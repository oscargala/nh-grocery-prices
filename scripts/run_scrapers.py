#!/usr/bin/env python3
"""Pipeline entry point — scrape, normalize, compare, and output grocery prices."""

from __future__ import annotations

import argparse
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


async def _run_walmart_stealth(
    categories,
    categories_to_scrape: list[str] | None = None,
    staleness_days: int = 7,
) -> list | None:
    """Try Walmart stealth scraper (Camoufox + Xvfb). Returns products or None on failure."""
    try:
        from src.scrapers.walmart_stealth import WalmartStealthScraper
    except ImportError:
        logger.info("WalmartStealth: camoufox not available, skipping")
        return None

    try:
        scraper = WalmartStealthScraper(
            categories=categories,
            categories_to_scrape=categories_to_scrape,
            staleness_days=staleness_days,
        )
        products = await scraper.collect_all()
        logger.info("WalmartStealth: %d products", len(products))
        return products
    except Exception:
        logger.exception("WalmartStealth scraper failed")
        return None


async def _run_playwright_scrapers(
    categories,
    categories_to_scrape: list[str] | None = None,
    staleness_days: int = 7,
) -> list:
    """Run Walmart scrapers in priority order, then remaining Playwright scrapers.

    Priority for Walmart:
    1. Stealth scraper (Camoufox + Xvfb) — best anti-bot bypass
    2. GraphQL API replay — fast but tokens expire quickly
    3. Standard Playwright — fallback, often blocked
    """
    from src.scrapers.playwright import PlaywrightManager, SCRAPERS
    from src.scrapers.walmart_api import WalmartAPIScraper

    all_products = []
    walmart_handled = False

    # 1. Try stealth scraper first (Camoufox + Xvfb)
    stealth_products = await _run_walmart_stealth(
        categories, categories_to_scrape, staleness_days
    )
    if stealth_products is not None:
        all_products.extend(stealth_products)
        walmart_handled = True

    try:
        async with PlaywrightManager() as manager:
            # 2. Try GraphQL API replay if stealth didn't handle Walmart
            if not walmart_handled:
                try:
                    api_scraper = WalmartAPIScraper(
                        manager=manager,
                        categories=categories,
                        categories_to_scrape=categories_to_scrape,
                        staleness_days=staleness_days,
                    )
                    products = await api_scraper.collect_all()
                    all_products.extend(products)
                    walmart_handled = True
                    logger.info("WalmartAPI: %d products", len(products))
                except Exception:
                    logger.exception("WalmartAPI scraper failed, will try Playwright fallback")

            # 3. Run remaining Playwright scrapers (skip Walmart if already handled)
            for scraper_cls in SCRAPERS:
                if walmart_handled and scraper_cls.store_id == "walmart":
                    continue
                scraper = scraper_cls(
                    manager=manager,
                    categories=categories,
                    categories_to_scrape=categories_to_scrape,
                    staleness_days=staleness_days,
                )
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


def run_pipeline(
    postal_code: str = "03301",
    categories_to_scrape: list[str] | None = None,
    staleness_days: int = 7,
) -> dict:
    """Run the full scraping pipeline and return structured results.

    Args:
        postal_code: ZIP code for Flipp flyer lookups.
        categories_to_scrape: If set, only scrape these category IDs
            in Playwright scrapers (e.g., for cron drip scheduling).
        staleness_days: Skip categories scraped within this many days.

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
    playwright_products = asyncio.run(
        _run_playwright_scrapers(categories, categories_to_scrape, staleness_days)
    )
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scrape grocery prices and generate comparison reports.",
    )
    parser.add_argument(
        "--categories",
        nargs="+",
        metavar="CAT",
        help=(
            "Only scrape these category IDs in Playwright scrapers "
            "(e.g., --categories canned_vegetables pasta rice). "
            "Useful for cron drip scheduling where each job handles a subset."
        ),
    )
    parser.add_argument(
        "--staleness-days",
        type=int,
        default=7,
        metavar="DAYS",
        help="Skip Playwright categories scraped within this many days (default: 7).",
    )
    parser.add_argument(
        "--zip",
        default="03301",
        help="ZIP code for Flipp flyer lookups (default: 03301).",
    )
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    args = parse_args()
    result = run_pipeline(
        postal_code=args.zip,
        categories_to_scrape=args.categories,
        staleness_days=args.staleness_days,
    )
    print_summary(result)
    csv_path = save_csv(result)
    print(f"\nCSV saved to: {csv_path}")


if __name__ == "__main__":
    main()
