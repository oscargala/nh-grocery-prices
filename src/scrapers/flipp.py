"""Production Flipp scraper — fetches and normalizes flyer data for target stores."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..models import CategoryDef, FlyerProduct
from ..normalize import (
    load_categories,
    load_stores,
    normalize_flyer_items,
    resolve_store_name,
    get_store_display_name,
)
from .flipp_client import FlippClient

logger = logging.getLogger(__name__)


@dataclass
class FlippScraper:
    """Wraps FlippClient with store filtering and normalization logic."""

    postal_code: str = "03301"
    stores_config: dict = field(default_factory=dict)
    categories: list[CategoryDef] = field(default_factory=list)
    client: FlippClient = field(default=None)
    skip_stores: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        if not self.stores_config:
            self.stores_config = load_stores()
        if not self.categories:
            self.categories = load_categories()
        if self.client is None:
            self.client = FlippClient(postal_code=self.postal_code)

    def get_target_flyers(self) -> dict[str, list[dict]]:
        """Fetch flyers and return ALL active flyers per target store.

        Returns {store_id: [flyer_dict, ...]}. Each store may have multiple
        parallel flyers (e.g., Aldi runs a "Weekly Ad" grocery flyer alongside
        an "In Store Ad" of seasonal non-grocery features; both are valid the
        same week and only one of them contains pantry items).
        """
        data = self.client.get_flyers()
        if data is None:
            logger.error("Failed to fetch flyers")
            return {}

        flyers = data.get("flyers", []) if isinstance(data, dict) else data

        alias_map = self.stores_config["alias_map"]
        by_store: dict[str, list[dict]] = {}
        for flyer in flyers:
            merchant = flyer.get("merchant", "")
            if isinstance(merchant, dict):
                merchant = merchant.get("name", "")
            merchant = str(merchant).strip()

            store_id = resolve_store_name(merchant, alias_map)
            if store_id is None:
                continue
            if store_id in self.skip_stores:
                continue

            by_store.setdefault(store_id, []).append(flyer)

        # Sort each store's flyers by valid_to descending so logs show newest first
        for store_id, store_flyers in by_store.items():
            store_flyers.sort(key=lambda f: f.get("valid_to", ""), reverse=True)
            if len(store_flyers) > 1:
                logger.info(
                    "Store %s: %d flyers — %s",
                    store_id, len(store_flyers),
                    ", ".join(
                        f"{f.get('id')} ({f.get('name', '')[:25]})"
                        for f in store_flyers
                    ),
                )

        logger.info("Target stores matched: %s", list(by_store.keys()))
        return by_store

    def collect_all(self) -> list[FlyerProduct]:
        """Run the full collection pipeline: fetch all flyers per store,
        get items, normalize, deduplicate.

        Returns all normalized FlyerProduct objects (both categorized and uncategorized).
        """
        target_flyers = self.get_target_flyers()
        if not target_flyers:
            logger.warning("No target store flyers found")
            return []

        all_products: list[FlyerProduct] = []
        stores_list = self.stores_config["stores"]

        for store_id, flyers in target_flyers.items():
            store_name = get_store_display_name(store_id, stores_list)
            store_products: list[FlyerProduct] = []
            for flyer in flyers:
                flyer_id = flyer.get("id")
                if flyer_id is None:
                    continue
                raw_items = self.client.get_flyer_items(flyer_id)
                logger.info(
                    "Fetched %d raw items from %s (flyer %d: %s)",
                    len(raw_items), store_name, flyer_id,
                    flyer.get("name", "")[:30],
                )
                products = normalize_flyer_items(
                    raw_items, store_id, store_name, flyer_id, self.categories
                )
                store_products.extend(products)

            # Dedupe within a store across flyers — same (name, price) is one product.
            # Different prices for the same name (e.g., coupon vs base) are kept.
            seen: set[tuple[str, float]] = set()
            deduped: list[FlyerProduct] = []
            for p in store_products:
                key = (p.name.lower().strip(), round(p.price, 2))
                if key in seen:
                    continue
                seen.add(key)
                deduped.append(p)
            if len(deduped) < len(store_products):
                logger.info(
                    "%s: deduped %d → %d products across %d flyers",
                    store_name, len(store_products), len(deduped), len(flyers),
                )
            all_products.extend(deduped)

        categorized = [p for p in all_products if p.category]
        logger.info(
            "Collection complete: %d total items, %d categorized across %d stores",
            len(all_products), len(categorized), len(target_flyers),
        )
        return all_products
