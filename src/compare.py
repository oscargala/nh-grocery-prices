"""Cross-store comparison logic for categorized grocery products."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from .models import FlyerProduct


@dataclass
class CategorySummary:
    """Summary stats for a single category."""

    category_id: str
    category_name: str
    deal_count: int
    cheapest_price: float | None
    cheapest_store: str | None
    cheapest_product: str | None
    price_range: tuple[float, float] | None
    stores_with_deals: list[str]


def best_prices_by_category(
    products: list[FlyerProduct], top_n: int = 5
) -> dict[str, list[FlyerProduct]]:
    """Group categorized products by category, sorted by price (cheapest first).

    Returns {category_id: [top_n cheapest FlyerProducts]}.
    Only includes products that have a category assigned.
    """
    by_category: dict[str, list[FlyerProduct]] = defaultdict(list)
    for p in products:
        if p.category:
            by_category[p.category].append(p)

    result = {}
    for cat_id, items in by_category.items():
        items.sort(key=lambda p: p.price)
        result[cat_id] = items[:top_n]

    return result


def category_summary(
    products: list[FlyerProduct],
    category_names: dict[str, str],
) -> list[CategorySummary]:
    """Compute per-category summary stats.

    Args:
        products: All products (categorized and uncategorized).
        category_names: {category_id: display_name} mapping.

    Returns list of CategorySummary sorted by category name.
    """
    by_category: dict[str, list[FlyerProduct]] = defaultdict(list)
    for p in products:
        if p.category:
            by_category[p.category].append(p)

    summaries = []
    for cat_id, cat_name in sorted(category_names.items(), key=lambda x: x[1]):
        items = by_category.get(cat_id, [])
        if items:
            items.sort(key=lambda p: p.price)
            cheapest = items[0]
            prices = [p.price for p in items]
            stores = sorted(set(p.store for p in items))
            summaries.append(
                CategorySummary(
                    category_id=cat_id,
                    category_name=cat_name,
                    deal_count=len(items),
                    cheapest_price=cheapest.price,
                    cheapest_store=cheapest.store,
                    cheapest_product=cheapest.name,
                    price_range=(min(prices), max(prices)),
                    stores_with_deals=stores,
                )
            )
        else:
            summaries.append(
                CategorySummary(
                    category_id=cat_id,
                    category_name=cat_name,
                    deal_count=0,
                    cheapest_price=None,
                    cheapest_store=None,
                    cheapest_product=None,
                    price_range=None,
                    stores_with_deals=[],
                )
            )

    return summaries
