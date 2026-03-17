"""Category matching and data normalization for the grocery price pipeline."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from .models import CategoryDef, FlyerProduct

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def load_categories(path: Path | None = None) -> list[CategoryDef]:
    """Load category definitions from JSON and compile regex patterns."""
    path = path or DATA_DIR / "categories.json"
    with open(path) as f:
        data = json.load(f)
    categories = []
    for cat in data["categories"]:
        categories.append(
            CategoryDef(
                id=cat["id"],
                name=cat["name"],
                keywords=cat.get("keywords", []),
                exclude=cat.get("exclude", []),
            )
        )
    logger.info("Loaded %d categories", len(categories))
    return categories


def load_stores(path: Path | None = None) -> dict:
    """Load store config. Returns dict with 'alias_map' and 'stores' list."""
    path = path or DATA_DIR / "stores.json"
    with open(path) as f:
        data = json.load(f)

    alias_map: dict[str, str] = {}
    for store in data["stores"]:
        canonical = store["id"]
        for alias in store.get("merchant_aliases", []):
            alias_map[alias.strip().lower()] = canonical
    return {"stores": data["stores"], "alias_map": alias_map}


def resolve_store_name(merchant_name: str, alias_map: dict[str, str]) -> str | None:
    """Resolve a Flipp merchant name to a canonical store ID, or None."""
    return alias_map.get(merchant_name.strip().lower())


def get_store_display_name(store_id: str, stores: list[dict]) -> str:
    """Get display name for a store ID."""
    for s in stores:
        if s["id"] == store_id:
            return s["name"]
    return store_id


def parse_price(price_str: str | None) -> float | None:
    """Parse a price string into a float.

    Handles: "7.99", "2/$5" (returns per-item), "", None.
    """
    if not price_str:
        return None
    price_str = str(price_str).strip()
    if not price_str:
        return None

    # Handle "N/$X" or "N for $X" patterns
    multi_match = re.match(r"(\d+)\s*/\s*\$?([\d.]+)", price_str)
    if multi_match:
        count = int(multi_match.group(1))
        total = float(multi_match.group(2))
        if count > 0:
            return round(total / count, 2)
        return None

    multi_match2 = re.match(r"(\d+)\s+for\s+\$?([\d.]+)", price_str, re.IGNORECASE)
    if multi_match2:
        count = int(multi_match2.group(1))
        total = float(multi_match2.group(2))
        if count > 0:
            return round(total / count, 2)
        return None

    # Strip $ and try direct float
    cleaned = price_str.replace("$", "").replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def categorize_item(name: str, categories: list[CategoryDef]) -> str | None:
    """Return the first matching category ID, or None."""
    for cat in categories:
        if cat.matches(name):
            return cat.id
    return None


def normalize_flyer_items(
    raw_items: list[dict],
    store_id: str,
    store_name: str,
    flyer_id: int,
    categories: list[CategoryDef],
) -> list[FlyerProduct]:
    """Normalize raw Flipp items into FlyerProduct objects.

    Filters out non-products (display_type != 1) and items without prices.
    """
    products = []
    for item in raw_items:
        # Filter non-product items
        if item.get("display_type") != 1:
            continue

        price = parse_price(item.get("price"))
        if price is None:
            continue

        name = item.get("name", "").strip()
        if not name:
            continue

        category = categorize_item(name, categories)

        products.append(
            FlyerProduct(
                name=name,
                store=store_name,
                price=price,
                brand=item.get("brand") or None,
                category=category,
                valid_from=item.get("valid_from"),
                valid_to=item.get("valid_to"),
                flyer_id=flyer_id,
                item_id=item.get("id"),
            )
        )

    categorized = [p for p in products if p.category is not None]
    logger.info(
        "Store %s: %d items → %d with price → %d categorized",
        store_name,
        len(raw_items),
        len(products),
        len(categorized),
    )
    return products
