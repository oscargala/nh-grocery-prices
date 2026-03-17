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

    def get_target_flyers(self) -> dict[str, dict]:
        """Fetch flyers and return the best one per target store.

        Returns {store_id: flyer_dict} for each matched store.
        Picks the flyer with the latest valid_to date per store.
        """
        data = self.client.get_flyers()
        if data is None:
            logger.error("Failed to fetch flyers")
            return {}

        flyers = data.get("flyers", []) if isinstance(data, dict) else data

        alias_map = self.stores_config["alias_map"]
        # Group flyers by canonical store ID
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

        # Pick best flyer per store (latest valid_to, then most items as tiebreaker)
        best: dict[str, dict] = {}
        for store_id, store_flyers in by_store.items():
            store_flyers.sort(
                key=lambda f: (f.get("valid_to", ""), f.get("item_count", 0)),
                reverse=True,
            )
            best[store_id] = store_flyers[0]
            if len(store_flyers) > 1:
                logger.info(
                    "Store %s has %d flyers, picking flyer %d",
                    store_id,
                    len(store_flyers),
                    store_flyers[0].get("id"),
                )

        logger.info("Target stores matched: %s", list(best.keys()))
        return best

    def collect_all(self) -> list[FlyerProduct]:
        """Run the full collection pipeline: fetch flyers, get items, normalize.

        Returns all normalized FlyerProduct objects (both categorized and uncategorized).
        """
        target_flyers = self.get_target_flyers()
        if not target_flyers:
            logger.warning("No target store flyers found")
            return []

        all_products: list[FlyerProduct] = []
        stores_list = self.stores_config["stores"]

        for store_id, flyer in target_flyers.items():
            flyer_id = flyer.get("id")
            if flyer_id is None:
                continue

            store_name = get_store_display_name(store_id, stores_list)
            raw_items = self.client.get_flyer_items(flyer_id)
            logger.info(
                "Fetched %d raw items from %s (flyer %d)",
                len(raw_items),
                store_name,
                flyer_id,
            )

            products = normalize_flyer_items(
                raw_items, store_id, store_name, flyer_id, self.categories
            )
            all_products.extend(products)

        categorized = [p for p in all_products if p.category]
        logger.info(
            "Collection complete: %d total items, %d categorized across %d stores",
            len(all_products),
            len(categorized),
            len(target_flyers),
        )
        return all_products
