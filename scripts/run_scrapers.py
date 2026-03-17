#!/usr/bin/env python3
"""Pipeline entry point — scrape, normalize, compare, and output grocery prices."""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

# Allow imports from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.compare import best_prices_by_category, category_summary
from src.models import FlyerProduct
from src.normalize import load_categories, load_stores
from src.scrapers.aldi import collect_all as aldi_collect_all
from src.scrapers.flipp import FlippScraper

logger = logging.getLogger(__name__)

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"


async def _run_playwright_scrapers(categories) -> list:
    """Run all enabled Playwright scrapers with a shared browser."""
    from src.scrapers.playwright import PlaywrightManager, SCRAPERS

    all_products = []
    try:
        async with PlaywrightManager() as manager:
            for scraper_cls in SCRAPERS:
                scraper = scraper_cls(manager=manager, categories=categories)
                try:
                    products = await scraper.collect_all()
                    all_products.extend(products)
                    logger.info(
                        "Playwright %s: %d products",
                        scraper.store_name,
                        len(products),
                    )
                except Exception:
                    logger.exception("Playwright scraper failed: %s", scraper.store_name)
    except Exception:
        logger.exception("Playwright browser failed to start — skipping headless scrapers")

    return all_products


def run_pipeline(postal_code: str = "03301") -> dict:
    """Run the full scraping pipeline and return structured results.

    Returns:
        {
            "timestamp": ISO timestamp,
            "postal_code": str,
            "stores_scraped": [store names],
            "all_products": [FlyerProduct as dicts],
            "categorized_products": [FlyerProduct as dicts],
            "best_by_category": {cat_id: [FlyerProduct dicts]},
            "summaries": [CategorySummary dicts],
            "category_names": {cat_id: display_name},
        }
    """
    scraper = FlippScraper(postal_code=postal_code)
    categories = scraper.categories
    category_names = {c.id: c.name for c in categories}

    logger.info("Starting pipeline for postal code %s", postal_code)

    # Flipp flyer data (sale prices)
    all_products = scraper.collect_all()
    logger.info("Flipp: %d products", len(all_products))

    # Aldi catalog data (everyday prices)
    aldi_products = aldi_collect_all()
    all_products.extend(aldi_products)
    logger.info("Aldi catalog: %d products, total: %d", len(aldi_products), len(all_products))

    # Playwright scrapers (everyday prices from stores behind anti-bot protection)
    playwright_products = asyncio.run(_run_playwright_scrapers(categories))
    all_products.extend(playwright_products)
    logger.info("Playwright: %d products, total: %d", len(playwright_products), len(all_products))

    categorized = [p for p in all_products if p.category]

    best = best_prices_by_category(categorized)
    summaries = category_summary(categorized, category_names)

    stores_scraped = sorted(set(p.store for p in all_products))

    return {
        "timestamp": datetime.now().isoformat(),
        "postal_code": postal_code,
        "stores_scraped": stores_scraped,
        "all_products": [asdict(p) for p in all_products],
        "categorized_products": [asdict(p) for p in categorized],
        "best_by_category": {
            cat_id: [asdict(p) for p in items] for cat_id, items in best.items()
        },
        "summaries": [asdict(s) for s in summaries],
        "category_names": category_names,
    }


def print_summary(result: dict) -> None:
    """Print a human-readable summary to the console."""
    print("\n" + "=" * 60)
    print("NH GROCERY PRICE COMPARISON")
    print(f"Generated: {result['timestamp']}")
    print(f"Zip code: {result['postal_code']}")
    print(f"Stores scraped: {', '.join(result['stores_scraped'])}")
    print("=" * 60)

    total_categorized = len(result["categorized_products"])
    total_all = len(result["all_products"])
    print(f"\nTotal items: {total_all} | Categorized: {total_categorized}")

    for summary in result["summaries"]:
        print(f"\n--- {summary['category_name']} ---")
        if summary["deal_count"] == 0:
            print("  No deals found this week")
            continue
        print(
            f"  {summary['deal_count']} deals | "
            f"Cheapest: ${summary['cheapest_price']:.2f} at {summary['cheapest_store']}"
        )
        print(f"  Best deal: {summary['cheapest_product']}")

        # Show top deals from best_by_category
        cat_id = summary["category_id"]
        best_items = result["best_by_category"].get(cat_id, [])
        for item in best_items[:3]:
            print(f"    ${item['price']:.2f} — {item['name']} ({item['store']})")

    print("\n" + "=" * 60)


def save_csv(result: dict) -> Path:
    """Save categorized products to CSV."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    csv_path = OUTPUT_DIR / f"products_{date_str}.csv"

    products = result["categorized_products"]
    if not products:
        logger.warning("No categorized products to save")
        return csv_path

    fieldnames = ["category", "store", "name", "price", "price_type", "brand", "size", "unit", "unit_price", "valid_from", "valid_to"]
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for p in sorted(products, key=lambda x: (x["category"], x["price"])):
            writer.writerow(p)

    logger.info("Saved %d products to %s", len(products), csv_path)
    return csv_path


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    result = run_pipeline()
    print_summary(result)
    csv_path = save_csv(result)
    print(f"\nCSV saved to: {csv_path}")


if __name__ == "__main__":
    main()
