"""Firecrawl-based store scrapers.

Uses Firecrawl's hosted scrape API with AI-powered JSON schema extraction.
One scraper class, parameterized per store via (store_id, store_name, urls).

Why this exists: Walmart/Aldi/Sam's Club all migrated to JS-rendered pages or
anti-bot setups that broke our local scrapers. Firecrawl handles both rendering
and anti-bot at the network edge, so we get clean structured products back
without per-store HTML parsing.

Rate limiting: Firecrawl free tier is 10 RPM; we space requests by ~6.5s.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

import requests

from ..models import CategoryDef, FlyerProduct
from ..normalize import categorize_item
from .playwright.base import CACHE_DIR, DEFAULT_STALENESS_DAYS

logger = logging.getLogger(__name__)

FIRECRAWL_ENDPOINT = "https://api.firecrawl.dev/v1/scrape"
FIRECRAWL_TIMEOUT = 90  # Firecrawl can take ~30s for JS-heavy pages

# Schema we ask Firecrawl to extract from every store page.
PRODUCT_SCHEMA = {
    "type": "object",
    "properties": {
        "products": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "brand": {"type": "string"},
                    "size": {"type": "string"},
                    "price": {"type": "number"},
                    "price_per_unit": {"type": "string"},
                },
                "required": ["name", "price"],
            },
        },
    },
}

SIZE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(oz|lb|fl\.?\s*oz|ct|count|pk|rolls?|ea)",
    re.IGNORECASE,
)


def _parse_size(text: str | None) -> tuple[float | None, str | None]:
    if not text:
        return None, None
    m = SIZE_RE.search(text)
    if not m:
        return None, None
    amount = float(m.group(1))
    unit = m.group(2).lower().replace(".", "").replace(" ", "")
    return amount, unit


class FirecrawlStoreScraper:
    """Scrapes a store's category pages via Firecrawl with JSON extraction.

    Each instance is configured with a `urls` dict mapping our internal
    category IDs to the store URL that serves them. Multiple category IDs
    may share a URL (e.g. Aldi's canned-foods page covers both canned_vegetables
    and canned_soups); we dedupe and bin products by `categorize_item()`.
    """

    def __init__(
        self,
        store_id: str,
        store_name: str,
        urls: dict[str, str],
        categories: list[CategoryDef],
        categories_to_scrape: list[str] | None = None,
        staleness_days: int = DEFAULT_STALENESS_DAYS,
        api_key: str | None = None,
        min_delay: float = 6.5,
    ) -> None:
        self.store_id = store_id
        self.store_name = store_name
        self.urls = urls
        self.categories = categories
        self.categories_to_scrape = categories_to_scrape
        self.staleness_days = staleness_days
        self.api_key = api_key or os.environ.get("FIRECRAWL_API_KEY")
        self.min_delay = min_delay

    def collect_all(self) -> list[FlyerProduct]:
        if not self.api_key:
            logger.info("Firecrawl %s: no API key set, skipping", self.store_name)
            return self._load_cache()

        stale_cats = self._stale_categories(list(self.urls.keys()))
        if not stale_cats:
            return self._load_cache()

        # Group stale categories by URL — Aldi has multi-category pages
        url_to_cats: dict[str, list[str]] = {}
        for cat in stale_cats:
            url = self.urls[cat]
            url_to_cats.setdefault(url, []).append(cat)

        new_products: list[FlyerProduct] = []
        successful: list[str] = []  # only categories whose URL actually returned
        for url, cats in url_to_cats.items():
            try:
                page_products = self._scrape_url(url, cats)
                new_products.extend(page_products)
                successful.extend(cats)
                logger.info(
                    "Firecrawl %s: %d products from %s (categories: %s)",
                    self.store_name, len(page_products), url, ", ".join(cats),
                )
            except Exception:
                # Transient failure: keep the existing cache for these cats
                logger.exception("Firecrawl %s: failed %s", self.store_name, url)
            time.sleep(self.min_delay)

        self._save_cache(new_products, refreshed_cats=successful)
        return self._load_cache()

    def _scrape_url(self, url: str, category_hints: list[str]) -> list[FlyerProduct]:
        payload = {
            "url": url,
            "formats": ["json"],
            "jsonOptions": {"schema": PRODUCT_SCHEMA},
            "onlyMainContent": True,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        resp = requests.post(
            FIRECRAWL_ENDPOINT,
            headers=headers,
            json=payload,
            timeout=FIRECRAWL_TIMEOUT,
        )
        if resp.status_code == 429:
            logger.warning("Firecrawl %s: rate limited on %s", self.store_name, url)
            return []
        resp.raise_for_status()
        body = resp.json()
        if not body.get("success"):
            logger.warning(
                "Firecrawl %s: unsuccessful response: %s",
                self.store_name, body.get("error") or body.get("warning"),
            )
            return []

        extracted = body.get("data", {}).get("json") or {}
        raw_products = extracted.get("products", []) if isinstance(extracted, dict) else []

        results: list[FlyerProduct] = []
        seen_names: set[str] = set()
        for raw in raw_products:
            name = (raw.get("name") or "").strip()
            if not name or name in seen_names:
                continue
            seen_names.add(name)

            try:
                price = float(raw.get("price"))
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue

            brand = (raw.get("brand") or "").strip() or None
            size_text = raw.get("size") or ""
            size, unit = _parse_size(size_text)

            # Prefer the store's price_per_unit when provided; fall back to computed
            ppu_raw = raw.get("price_per_unit") or ""
            ppu_match = re.search(r"(\d+(?:\.\d+)?)", ppu_raw)
            unit_price = float(ppu_match.group(1)) / 100 if ppu_match and "¢" in ppu_raw else None
            if unit_price is None and size and size > 0:
                unit_price = round(price / size, 3)

            category = categorize_item(name, self.categories)
            if category is None and brand:
                category = categorize_item(f"{brand} {name}", self.categories)
            if category is None and len(category_hints) == 1:
                # Only use a hint when there's no ambiguity (one cat per URL)
                category = category_hints[0]
            if category is None:
                continue  # uncategorizable — drop

            results.append(
                FlyerProduct(
                    name=name,
                    store=self.store_name,
                    price=price,
                    brand=brand,
                    category=category,
                    price_type="everyday",
                    size=size,
                    unit=unit,
                    unit_price=unit_price,
                )
            )
        return results

    # --- Per-category cache (same on-disk format as walmart_stealth.py) ---

    def _cache_path(self) -> Path:
        return CACHE_DIR / f"{self.store_id}.json"

    def _load_cache_data(self) -> dict:
        path = self._cache_path()
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text())
            if "products" in data and "categories" not in data:
                return self._migrate_cache(data)
            return data
        except Exception:
            return {}

    @staticmethod
    def _migrate_cache(old_data: dict) -> dict:
        timestamp = old_data.get("timestamp", datetime.now().isoformat())
        by_cat: dict[str, list[dict]] = {}
        for p in old_data.get("products", []):
            cat = p.get("category") or "uncategorized"
            by_cat.setdefault(cat, []).append(p)
        cats = {
            cat_id: {"scraped_at": timestamp, "products": prods}
            for cat_id, prods in by_cat.items()
        }
        return {"store_id": old_data.get("store_id", ""), "categories": cats}

    def _save_cache(
        self, products: list[FlyerProduct], refreshed_cats: list[str]
    ) -> None:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        now = datetime.now().isoformat()
        data = self._load_cache_data()
        data.setdefault("store_id", self.store_id)
        data.setdefault("categories", {})

        by_cat: dict[str, list[dict]] = {}
        for p in products:
            cat = p.category or "uncategorized"
            by_cat.setdefault(cat, []).append(asdict(p))

        # Replace every category we attempted to refresh — even with 0 products,
        # so we don't keep stale data labeled as fresh.
        for cat_id in refreshed_cats:
            data["categories"][cat_id] = {
                "scraped_at": now,
                "products": by_cat.get(cat_id, []),
            }

        self._cache_path().write_text(json.dumps(data, indent=2))
        total = sum(len(c["products"]) for c in data["categories"].values())
        logger.info(
            "Firecrawl %s: cache updated — %d new products, %d total across %d cats",
            self.store_name, len(products), total, len(data["categories"]),
        )

    def _load_cache(self) -> list[FlyerProduct]:
        data = self._load_cache_data()
        cats = data.get("categories", {})
        products: list[FlyerProduct] = []
        for cat_data in cats.values():
            for p in cat_data.get("products", []):
                try:
                    product = FlyerProduct(**p)
                except Exception:
                    continue
                # Re-categorize against the current categories.json so a category
                # schema change takes effect without re-scraping. Drop products
                # whose names no longer match any current category.
                new_cat = categorize_item(product.name, self.categories)
                if new_cat is None and product.brand:
                    new_cat = categorize_item(
                        f"{product.brand} {product.name}", self.categories
                    )
                if new_cat is None:
                    continue
                product.category = new_cat
                products.append(product)
        if cats:
            oldest = min((c["scraped_at"] for c in cats.values()), default="?")
            newest = max((c["scraped_at"] for c in cats.values()), default="?")
            logger.info(
                "Firecrawl %s: loaded %d cached products (oldest: %s, newest: %s)",
                self.store_name, len(products), oldest[:10], newest[:10],
            )
        return products

    def _stale_categories(self, all_category_ids: list[str]) -> list[str]:
        data = self._load_cache_data()
        cats = data.get("categories", {})
        threshold = datetime.now() - timedelta(days=self.staleness_days)
        stale: list[tuple[str, datetime]] = []
        for cat_id in all_category_ids:
            if self.categories_to_scrape and cat_id not in self.categories_to_scrape:
                continue
            cat_data = cats.get(cat_id)
            if not cat_data:
                stale.append((cat_id, datetime.min))
                continue
            try:
                scraped_at = datetime.fromisoformat(cat_data["scraped_at"])
            except (KeyError, ValueError):
                stale.append((cat_id, datetime.min))
                continue
            if scraped_at < threshold:
                stale.append((cat_id, scraped_at))
        stale.sort(key=lambda x: x[1])
        result = [c for c, _ in stale]
        if result:
            logger.info(
                "Firecrawl %s: %d/%d categories stale",
                self.store_name, len(result), len(all_category_ids),
            )
        else:
            logger.info("Firecrawl %s: all categories fresh", self.store_name)
        return result


# --- Per-store configs --------------------------------------------------------

ALDI_URLS: dict[str, str] = {
    "canned_vegetables": "https://www.aldi.us/products/pantry-essentials/canned-foods/k/102",
    "canned_soups": "https://www.aldi.us/products/pantry-essentials/soups-broth/k/105",
    "pasta": "https://www.aldi.us/products/pantry-essentials/pasta-rice-grains/k/108",
    "rice": "https://www.aldi.us/products/pantry-essentials/pasta-rice-grains/k/108",
    "oats": "https://www.aldi.us/products/breakfast-cereals/cereal-oatmeal/k/162",
    "cereal": "https://www.aldi.us/products/breakfast-cereals/cereal-oatmeal/k/162",
    "spices": "https://www.aldi.us/products/pantry-essentials/spices/k/106",
    "frozen_vegetables": "https://www.aldi.us/products/frozen-foods/frozen-vegetables/k/163",
    "frozen_meals": "https://www.aldi.us/products/frozen-foods/frozen-meals-sides/k/137",
    "toilet_paper": "https://www.aldi.us/products/household-essentials/paper-plastic-products/k/164",
    "paper_towels": "https://www.aldi.us/products/household-essentials/paper-plastic-products/k/164",
    "diapers": "https://www.aldi.us/products/baby-items/diapers-wipes-wash/k/53",
}

# Walmart & Sam's Club use search URLs. Concord NH = store 2055 (Walmart), 6604 (Sam's)
_WALMART_QUERIES: dict[str, str] = {
    "canned_vegetables": "canned+vegetables",
    "canned_soups": "canned+soup+broth",
    "pasta": "pasta+spaghetti",
    "rice": "rice+grains",
    "oats": "oatmeal+oats",
    "cereal": "breakfast+cereal",
    "spices": "spices+seasoning",
    "frozen_vegetables": "frozen+vegetables",
    "frozen_meals": "frozen+dinners+pot+pies",
    "toilet_paper": "toilet+paper+bath+tissue",
    "paper_towels": "paper+towels",
    "diapers": "diapers+baby+wipes",
}

WALMART_URLS: dict[str, str] = {
    cat: f"https://www.walmart.com/search?q={q}&store_id=2055"
    for cat, q in _WALMART_QUERIES.items()
}

SAMS_URLS: dict[str, str] = {
    cat: f"https://www.samsclub.com/s/{q.replace('+', '%20')}?clubId=6604"
    for cat, q in _WALMART_QUERIES.items()
}


def build_scrapers(
    categories: list[CategoryDef],
    categories_to_scrape: list[str] | None = None,
    staleness_days: int = DEFAULT_STALENESS_DAYS,
) -> list[FirecrawlStoreScraper]:
    """Construct the three Firecrawl-backed store scrapers."""
    return [
        FirecrawlStoreScraper(
            store_id="aldi",
            store_name="Aldi",
            urls=ALDI_URLS,
            categories=categories,
            categories_to_scrape=categories_to_scrape,
            staleness_days=staleness_days,
        ),
        FirecrawlStoreScraper(
            store_id="walmart",
            store_name="Walmart",
            urls=WALMART_URLS,
            categories=categories,
            categories_to_scrape=categories_to_scrape,
            staleness_days=staleness_days,
        ),
        FirecrawlStoreScraper(
            store_id="sams_club",
            store_name="Sam's Club",
            urls=SAMS_URLS,
            categories=categories,
            categories_to_scrape=categories_to_scrape,
            staleness_days=staleness_days,
        ),
    ]
