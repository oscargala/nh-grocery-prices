"""Walmart stealth scraper — Camoufox with headless='virtual' (auto-managed Xvfb)
to bypass Akamai/PerimeterX bot detection.

Strategy:
- Camoufox (C++-patched Firefox) with headless='virtual' (auto-managed Xvfb)
- Camoufox humanize=True handles cursor/typing at C++ level
- Search via the search bar instead of direct URL navigation
- Reuses the same per-category cache as other Walmart scrapers
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

# Search terms per category (same as other Walmart scrapers)
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

# Challenge page indicators
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


class WalmartStealthScraper:
    """Scrapes Walmart prices using Camoufox with virtual headed mode.

    This bypasses Akamai/PerimeterX by:
    - Using Camoufox (C++-level Firefox patches) with headless='virtual'
    - Auto-managed Xvfb virtual framebuffer (indistinguishable from real display)
    - Camoufox humanize=True for natural mouse/typing at C++ level
    - Navigating via search bar instead of direct URL
    """

    store_id = "walmart"
    store_name = "Walmart"
    base_url = "https://www.walmart.com"
    walmart_store_id = "2055"  # Concord NH Supercenter
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
        """Main entry point. Launch Camoufox, scrape stale categories, return all cached products."""
        all_cat_ids = list(CATEGORY_SEARCHES.keys())
        stale_ids = self._stale_categories(all_cat_ids)

        if not stale_ids:
            logger.info("WalmartStealth: all categories fresh, nothing to scrape")
            return self._load_cache()

        for attempt in range(1, self.max_retries + 1):
            try:
                products = await self._scrape(stale_ids)
                if products:
                    self._save_cache(products)
                    logger.info(
                        "WalmartStealth: scraped %d products on attempt %d",
                        len(products), attempt,
                    )
                return self._load_cache()
            except Exception:
                logger.exception(
                    "WalmartStealth: scrape failed (attempt %d/%d)",
                    attempt, self.max_retries,
                )
                if attempt < self.max_retries:
                    await asyncio.sleep(3)

        logger.error("WalmartStealth: all attempts failed, returning cache")
        return self._load_cache()

    async def _scrape(self, stale_ids: list[str]) -> list[FlyerProduct]:
        """Launch Camoufox headed browser and scrape categories."""
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
        # Only set locale when no proxy — geoip derives locale from proxy IP
        if not proxy_config:
            camoufox_kwargs["locale"] = "en-US"

        all_products: list[FlyerProduct] = []
        seen_ids: set[str] = set()

        async with AsyncCamoufox(**camoufox_kwargs) as browser:
            page = await browser.new_page()

            for cat_id in stale_ids:
                search_terms = CATEGORY_SEARCHES.get(cat_id, [])
                for term in search_terms:
                    try:
                        products = await self._search_via_url(page, term, cat_id, seen_ids)
                        if products:
                            all_products.extend(products)
                            # Save incrementally after each successful search
                            self._save_cache(all_products)
                        else:
                            logger.warning(
                                "WalmartStealth: no products for '%s', continuing",
                                term,
                            )
                    except Exception:
                        logger.exception("WalmartStealth: failed search for '%s'", term)
                        return all_products

                    # Strategic pause between searches
                    await asyncio.sleep(random.uniform(3.0, 7.0))

        return all_products

    async def _search_via_url(
        self,
        page,
        query: str,
        category_hint: str,
        seen_ids: set[str],
    ) -> list[FlyerProduct]:
        """Navigate directly to search URL to get SSR data in __NEXT_DATA__.

        Direct page.goto() triggers a full server-side render, which includes
        search results in __NEXT_DATA__. The search bar triggers client-side
        routing (React/Next.js) which fetches data via GraphQL CSR instead,
        leaving __NEXT_DATA__ empty.
        """
        search_url = f"{self.base_url}/search?q={query.replace(' ', '+')}&store_id={self.walmart_store_id}"
        logger.info("WalmartStealth: navigating to %s", search_url)

        await page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(random.uniform(2.0, 4.0))

        if await self._is_blocked(page):
            logger.warning("WalmartStealth: blocked after search for '%s'", query)
            return []

        return await self._extract_products(page, category_hint, seen_ids)

    async def _extract_products(
        self,
        page,
        category_hint: str,
        seen_ids: set[str],
    ) -> list[FlyerProduct]:
        """Extract products from __NEXT_DATA__ on the current page.

        __NEXT_DATA__ is present in the DOM even when the PerimeterX captcha
        overlay is showing, because Walmart uses SSR (server-side rendering).
        """
        # Extract __NEXT_DATA__
        next_data_str = await page.evaluate(
            "() => document.getElementById('__NEXT_DATA__')?.textContent"
        )
        if not next_data_str:
            logger.warning("WalmartStealth: no __NEXT_DATA__ found")
            return []

        try:
            data = json.loads(next_data_str)
        except json.JSONDecodeError:
            logger.warning("WalmartStealth: invalid JSON in __NEXT_DATA__")
            return []

        return self._parse_next_data(data, category_hint, seen_ids)

    async def _is_blocked(self, page) -> bool:
        """Check if the page shows a bot detection challenge."""
        try:
            title = (await page.title()).lower()
            content = (await page.content())[:3000].lower()
            is_blocked = any(
                indicator in title or indicator in content
                for indicator in CHALLENGE_INDICATORS
            )
            if is_blocked:
                logger.warning("WalmartStealth: blocked — attempting captcha solve")
                # Try to solve press-and-hold captcha before giving up
                solved = await self._solve_press_and_hold(page)
                if solved:
                    return False
            return is_blocked
        except Exception:
            return False

    async def _solve_press_and_hold(self, page) -> bool:
        """Attempt to solve PerimeterX press-and-hold captcha.

        The px-captcha requires pressing and holding a button for several seconds.
        We simulate this with a mouse-down, hold, mouse-up sequence.
        """
        try:
            # Look for the press-and-hold button
            button = await page.query_selector('#px-captcha')
            if not button:
                # Try alternative selectors
                button = await page.query_selector('[id*="px-captcha"]')
            if not button:
                button = await page.query_selector('div[aria-label="Press and hold"]')
            if not button:
                return False

            logger.info("WalmartStealth: attempting press-and-hold captcha solve")

            box = await button.bounding_box()
            if not box:
                return False

            # Move to the button — Camoufox humanize makes this natural at C++ level
            target_x = box["x"] + box["width"] / 2 + random.uniform(-5, 5)
            target_y = box["y"] + box["height"] / 2 + random.uniform(-5, 5)
            await page.mouse.move(target_x, target_y)
            await asyncio.sleep(random.uniform(0.2, 0.4))

            # Press and hold for a variable duration (typically needs 8-15 seconds)
            hold_duration = random.uniform(10.0, 14.0)
            logger.info("WalmartStealth: holding captcha button for %.1fs", hold_duration)

            await page.mouse.down()
            await asyncio.sleep(hold_duration)
            await page.mouse.up()

            # Wait for the page to potentially reload/redirect
            await asyncio.sleep(3.0)

            # Check if we're past the captcha
            content = (await page.content())[:3000].lower()
            still_blocked = any(
                ind in content for ind in ["press & hold", "px-captcha", "robot or human"]
            )

            if not still_blocked:
                logger.info("WalmartStealth: captcha solved successfully")
                return True

            logger.warning("WalmartStealth: captcha solve attempt failed")
            return False

        except Exception:
            logger.debug("WalmartStealth: captcha solve error", exc_info=True)
            return False

    def _parse_next_data(
        self,
        data: dict,
        category_hint: str,
        seen_ids: set[str],
    ) -> list[FlyerProduct]:
        """Extract products from Walmart's __NEXT_DATA__ structure.

        Filters to Walmart-sold, in-store items only (no third-party
        marketplace sellers, no online-only fulfillment center items).
        """
        products = []
        skipped_seller = 0
        skipped_fulfillment = 0
        try:
            stacks = (
                data.get("props", {})
                .get("pageProps", {})
                .get("initialData", {})
                .get("searchResult", {})
                .get("itemStacks", [])
            )
        except AttributeError:
            logger.warning("WalmartStealth: unexpected __NEXT_DATA__ structure")
            return []

        for stack in stacks:
            items = stack.get("items", [])
            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("__typename") not in ("Product", None):
                    if item.get("__typename") != "Product":
                        continue

                # Skip third-party marketplace sellers
                seller = item.get("sellerName") or ""
                if seller and seller != "Walmart.com":
                    skipped_seller += 1
                    continue

                # Skip online-only items (FC = fulfillment center, MARKETPLACE)
                fulfillment = item.get("fulfillmentType") or ""
                if fulfillment in ("FC", "MARKETPLACE"):
                    skipped_fulfillment += 1
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

        if skipped_seller or skipped_fulfillment:
            logger.info(
                "WalmartStealth: skipped %d third-party, %d online-only items",
                skipped_seller, skipped_fulfillment,
            )
        logger.info("WalmartStealth: parsed %d in-store products from __NEXT_DATA__", len(products))
        return products

    # --- Cache management (shares cache file with other Walmart scrapers) ---

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
        categories = {}
        for cat_id, prods in by_cat.items():
            categories[cat_id] = {"scraped_at": timestamp, "products": prods}
        return {"store_id": old_data.get("store_id", ""), "categories": categories}

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
        logger.info(
            "WalmartStealth: merged %d products into cache (%d total)",
            len(products), total,
        )

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
        logger.info(
            "WalmartStealth: loaded %d products from cache (%d categories)",
            len(products), len(cats),
        )
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
            logger.info(
                "WalmartStealth: %d/%d categories stale: %s",
                len(result), len(all_category_ids), ", ".join(result),
            )
        else:
            logger.info(
                "WalmartStealth: all categories fresh (within %d days)",
                self.staleness_days,
            )
        return result
