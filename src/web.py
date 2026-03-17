"""Flask web application for the NH Grocery Price Comparison dashboard."""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

# Allow imports when running as module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flask import Flask, jsonify, render_template

from src.normalize import load_categories

logger = logging.getLogger(__name__)

app = Flask(
    __name__,
    template_folder=str(Path(__file__).resolve().parent.parent / "templates"),
)

# In-memory cache of pipeline results
_cached_data: dict | None = None


def _run_and_cache() -> dict:
    """Run the pipeline and cache the result."""
    global _cached_data
    from scripts.run_scrapers import run_pipeline

    logger.info("Running pipeline...")
    _cached_data = run_pipeline()
    logger.info("Pipeline complete — %d categorized products", len(_cached_data["categorized_products"]))
    return _cached_data


def _get_data() -> dict:
    """Return cached data, or run the pipeline if no cache exists."""
    if _cached_data is None:
        return _run_and_cache()
    return _cached_data


@app.route("/")
def dashboard():
    """Main dashboard page."""
    data = _get_data()
    categories = load_categories()
    category_names = {c.id: c.name for c in categories}

    # Build template context
    category_sections = []
    for summary in data["summaries"]:
        cat_id = summary["category_id"]
        deals = data["best_by_category"].get(cat_id, [])
        # Get ALL deals for this category, not just top 5
        all_deals = [
            p for p in data["categorized_products"] if p["category"] == cat_id
        ]
        all_deals.sort(key=lambda p: p["price"])

        category_sections.append({
            "id": cat_id,
            "name": summary["category_name"],
            "deal_count": summary["deal_count"],
            "cheapest_price": summary["cheapest_price"],
            "cheapest_store": summary["cheapest_store"],
            "deals": all_deals,
        })

    return render_template(
        "dashboard.html",
        categories=category_sections,
        stores=data["stores_scraped"],
        total_deals=len(data["categorized_products"]),
        total_items=len(data["all_products"]),
        timestamp=data["timestamp"],
        postal_code=data["postal_code"],
    )


@app.route("/api/refresh")
def refresh():
    """Trigger a fresh scrape and return updated data as JSON."""
    data = _run_and_cache()
    return jsonify({
        "status": "ok",
        "timestamp": data["timestamp"],
        "stores_scraped": data["stores_scraped"],
        "total_items": len(data["all_products"]),
        "categorized": len(data["categorized_products"]),
        "summaries": data["summaries"],
    })


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    app.run(host="0.0.0.0", port=5000)
