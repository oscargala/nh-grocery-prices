"""Sam's Club stealth scraper — Camoufox with headless='virtual' (auto-managed Xvfb).

Same approach as the Walmart stealth scraper (same parent company, same Next.js
SSR architecture, same PerimeterX anti-bot). Direct URL navigation to search
pages extracts product data from __NEXT_DATA__.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..models import CategoryDef, FlyerProduct
from ..normalize import categorize_item, parse_price
from .playwright.base import CACHE_DIR, DEFAULT_STALENESS_DAYS

logger = logging.getLogger(__name__)

CATEGORY_SEARCHES: dict[str, list[str]] = {
    "canned_vegetables": ["canned vegetables"],
    "canned_soups": ["canned soup broth"],
    "pasta": ["pasta spaghetti"],
    "rice": ["rice grains"],
    "oats": ["oatmeal oats"],
    "cereal": ["breakfast cereal"],
    "spices": ["spices seasoning"],
    "frozen_vegetables": ["frozen vegetables"],
    "frozen_meals": ["frozen dinners pot pies"],
    "toilet_paper": ["toilet paper bath tissue"],
    "paper_towels": ["paper towels"],
    "diapers": ["diapers baby wipes"],
}

SIZE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(oz|lb|fl\.?\s*oz|ct|count|pk|rolls?|ea)",
    re.IGNORECASE,
)

CHALLENGE_INDICATORS = [
    "robot",
    "human",
    "blocked",
    "px-captcha",
    "press & hold",
    "verify you are",
    "access denied",
]


def _parse_size_from_text(text: str) -> tuple[float | None, str | None]:
    match = SIZE_RE.search(text)
    if match:
        return float(match.group(1)), match.group(2).lower().replace(".", "").replace(" ", "")
    return None, None


class SamsClubStealthScraper:
    """Scrapes Sam's Club prices using Camoufox with virtual headed mode.

    Uses the same __NEXT_DATA__ SSR extraction as the Walmart stealth scraper.
    Sam's Club doesn't have third-party marketplace sellers so no seller
    filtering is needed.
    """

    store_id = "sams_club"
    store_name = "Sam's Club"
    base_url = "https://www.samsclub.com"
    max_retries = 2

    def __init__(
        self,
        categories: list[CategoryDef],
        categories_to_scrape: list[str] | None = None,
        staleness_days: int = DEFAULT_STALENESS_DAYS,
    ) -> None:
        self.categories = categories
        self.categories_to_scrape = categories_to_scrape
        self.staleness_days = staleness_days

    async def collect_all(self) -> list[FlyerProduct]:
        """Main entry point."""
        all_cat_ids = list(CATEGORY_SEARCHES.keys())
        stale_ids = self._stale_categories(all_cat_ids)

        if not stale_ids:
            logger.info("SamsClub: all categories fresh, nothing to scrape")
            return self._load_cache()

        for attempt in range(1, self.max_retries + 1):
            try:
                products = await self._scrape(stale_ids)
                if products:
                    self._save_cache(products)
                    logger.info(
                        "SamsClub: scraped %d products on attempt %d",
                        len(products), attempt,
                    )
                return self._load_cache()
            except Exception:
                logger.exception(
                    "SamsClub: scrape failed (attempt %d/%d)",
                    attempt, self.max_retries,
                )
                if attempt < self.max_retries:
                    await asyncio.sleep(3)

        logger.error("SamsClub: all attempts failed, returning cache")
        return self._load_cache()

    async def _scrape(self, stale_ids: list[str]) -> list[FlyerProduct]:
        """Launch a fresh Camoufox browser per category to avoid session-based blocking.

        Sam's Club (same PerimeterX as Walmart) aggressively flags sessions
        after the first search. A fresh browser per category gives a clean
        fingerprint each time.
        """
        from camoufox.async_api import AsyncCamoufox

        proxy_url = os.environ.get("PROXY_URL")
        proxy_config = {"server": proxy_url} if proxy_url else None

        camoufox_kwargs: dict[str, Any] = {
            "headless": "virtual",
            "geoip": True,
            "proxy": proxy_config,
            "humanize": True,
            "os": "windows",
        }
        if not proxy_config:
            camoufox_kwargs["locale"] = "en-US"

        all_products: list[FlyerProduct] = []
        seen_ids: set[str] = set()

        for cat_id in stale_ids:
            search_terms = CATEGORY_SEARCHES.get(cat_id, [])
            for term in search_terms:
                try:
                    async with AsyncCamoufox(**camoufox_kwargs) as browser:
                        page = await browser.new_page()
                        products = await self._search_via_url(page, term, cat_id, seen_ids)
                    if products:
                        all_products.extend(products)
                        self._save_cache(all_products)
                    else:
                        logger.warning("SamsClub: no products for '%s', continuing", term)
                except Exception:
                    logger.exception("SamsClub: failed search for '%s'", term)

                await asyncio.sleep(random.uniform(2.0, 5.0))

        return all_products

    async def _search_via_url(
        self,
        page,
        query: str,
        category_hint: str,
        seen_ids: set[str],
    ) -> list[FlyerProduct]:
        """Navigate directly to search URL to get SSR data in __NEXT_DATA__."""
        search_url = f"{self.base_url}/s/{query.replace(' ', '%20')}"
        logger.info("SamsClub: navigating to %s", search_url)

        await page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(random.uniform(2.0, 4.0))

        if await self._is_blocked(page):
            logger.warning("SamsClub: blocked after search for '%s'", query)
            return []

        return await self._extract_products(page, category_hint, seen_ids)

    async def _extract_products(self, page, category_hint: str, seen_ids: set[str]) -> list[FlyerProduct]:
        """Extract products from __NEXT_DATA__."""
        next_data_str = await page.evaluate(
            "() => document.getElementById('__NEXT_DATA__')?.textContent"
        )
        if not next_data_str:
            logger.warning("SamsClub: no __NEXT_DATA__ found")
            return []

        try:
            data = json.loads(next_data_str)
        except json.JSONDecodeError:
            logger.warning("SamsClub: invalid JSON in __NEXT_DATA__")
            return []

        return self._parse_next_data(data, category_hint, seen_ids)

    async def _is_blocked(self, page) -> bool:
        """Check if the page shows a bot detection challenge."""
        try:
            title = (await page.title()).lower()
            content = (await page.content())[:3000].lower()
            return any(
                indicator in title or indicator in content
                for indicator in CHALLENGE_INDICATORS
            )
        except Exception:
            return False

    def _parse_next_data(
        self,
        data: dict,
        category_hint: str,
        seen_ids: set[str],
    ) -> list[FlyerProduct]:
        """Extract products from Sam's Club __NEXT_DATA__ structure."""
        products = []
        try:
            stacks = (
                data.get("props", {})
                .get("pageProps", {})
                .get("initialData", {})
                .get("searchResult", {})
                .get("itemStacks", [])
            )
        except AttributeError:
            logger.warning("SamsClub: unexpected __NEXT_DATA__ structure")
            return []

        for stack in stacks:
            items = stack.get("items", [])
            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("__typename") not in ("Product", None):
                    if item.get("__typename") != "Product":
                        continue

                prod_id = str(item.get("usItemId", item.get("id", "")))
                if not prod_id or prod_id in seen_ids:
                    continue
                seen_ids.add(prod_id)

                name = (item.get("name") or "").strip()
                if not name:
                    continue

                price_val = item.get("price")
                if price_val is None:
                    pi = item.get("priceInfo") or {}
                    if isinstance(pi, dict):
                        cp = pi.get("currentPrice")
                        if isinstance(cp, dict):
                            price_val = cp.get("price")
                        elif cp is not None:
                            price_val = cp

                price = parse_price(str(price_val)) if price_val is not None else None
                if price is None or price <= 0:
                    continue

                brand = (item.get("brand") or "").strip() or None
                size, unit = _parse_size_from_text(name)
                unit_price = None
                if size and size > 0:
                    unit_price = round(price / size, 3)

                category = categorize_item(name, self.categories)
                if category is None and brand:
                    category = categorize_item(f"{brand} {name}", self.categories)
                if category is None:
                    category = category_hint

                products.append(
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

        logger.info("SamsClub: parsed %d products from __NEXT_DATA__", len(products))
        return products

    # --- Cache management ---

    def _cache_path(self) -> Path:
        return CACHE_DIR / f"{self.store_id}.json"

    def _load_cache_data(self) -> dict:
        path = self._cache_path()
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text())
        except Exception:
            return {}

    def _save_cache(self, products: list[FlyerProduct]) -> None:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        now = datetime.now().isoformat()
        data = self._load_cache_data()
        data.setdefault("store_id", self.store_id)
        data.setdefault("categories", {})

        by_cat: dict[str, list[dict]] = {}
        for p in products:
            cat = p.category or "uncategorized"
            by_cat.setdefault(cat, []).append(asdict(p))

        for cat_id, new_prods in by_cat.items():
            data["categories"][cat_id] = {"scraped_at": now, "products": new_prods}

        self._cache_path().write_text(json.dumps(data, indent=2))
        total = sum(len(c["products"]) for c in data["categories"].values())
        logger.info("SamsClub: merged %d products into cache (%d total)", len(products), total)

    def _load_cache(self) -> list[FlyerProduct]:
        data = self._load_cache_data()
        cats = data.get("categories", {})
        if not cats:
            return []
        products = []
        for cat_data in cats.values():
            for p in cat_data.get("products", []):
                try:
                    products.append(FlyerProduct(**p))
                except Exception:
                    pass
        logger.info("SamsClub: loaded %d products from cache (%d categories)", len(products), len(cats))
        return products

    def _stale_categories(self, all_category_ids: list[str]) -> list[str]:
        data = self._load_cache_data()
        cats = data.get("categories", {})
        threshold = datetime.now() - timedelta(days=self.staleness_days)

        stale = []
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
        result = [cat_id for cat_id, _ in stale]
        if result:
            logger.info("SamsClub: %d/%d categories stale: %s", len(result), len(all_category_ids), ", ".join(result))
        return result
