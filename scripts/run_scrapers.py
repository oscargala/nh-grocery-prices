#!/usr/bin/env python3
"""Pipeline entry point — scrape, normalize, compare, and output grocery prices."""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

# Allow imports from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Load .env if present so FIRECRAWL_API_KEY is available without exporting first
_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
if _ENV_PATH.exists():
    for _line in _ENV_PATH.read_text().splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip())

from src.compare import best_prices_by_category, category_summary
from src.normalize import load_categories
from src.scrapers.firecrawl_store import build_scrapers as build_firecrawl_scrapers
from src.scrapers.flipp import FlippScraper

logger = logging.getLogger(__name__)

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"


def _run_firecrawl_scrapers(
    categories,
    categories_to_scrape: list[str] | None = None,
    staleness_days: int = 7,
) -> list:
    """Run Firecrawl-backed scrapers (Aldi + Walmart + Sam's Club).

    Replaces the previous Camoufox/Playwright stack, which broke after Aldi's
    Instacart migration and Walmart's PerimeterX rollout.
    """
    all_products = []
    for scraper in build_firecrawl_scrapers(
        categories=categories,
        categories_to_scrape=categories_to_scrape,
        staleness_days=staleness_days,
    ):
        try:
            products = scraper.collect_all()
            all_products.extend(products)
            logger.info(
                "Firecrawl %s: %d products returned to pipeline",
                scraper.store_name, len(products),
            )
        except Exception:
            logger.exception("Firecrawl %s: failed", scraper.store_name)
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

    # Firecrawl-backed catalog scrapers (Aldi, Walmart, Sam's Club — everyday prices)
    firecrawl_products = _run_firecrawl_scrapers(
        categories, categories_to_scrape, staleness_days
    )
    all_products.extend(firecrawl_products)
    logger.info(
        "Firecrawl: %d products, pipeline total: %d",
        len(firecrawl_products), len(all_products),
    )

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
