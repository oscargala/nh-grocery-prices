"""Walmart scraper — everyday prices extracted from __NEXT_DATA__ SSR payloads."""

from __future__ import annotations

import json
import logging
import random
import re

from ...models import FlyerProduct
from ...normalize import categorize_item, parse_price
from .base import PlaywrightStoreScraper

logger = logging.getLogger(__name__)

# Fewer, broader search terms to minimize requests and avoid detection
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


def _parse_size_from_text(text: str) -> tuple[float | None, str | None]:
    match = SIZE_RE.search(text)
    if match:
        return float(match.group(1)), match.group(2).lower().replace(".", "").replace(" ", "")
    return None, None


class WalmartScraper(PlaywrightStoreScraper):
    """Scrapes Walmart everyday prices from __NEXT_DATA__ in search result pages."""

    store_id = "walmart"
    store_name = "Walmart"
    base_url = "https://www.walmart.com"
    # Aggressive delays to avoid Akamai detection
    min_delay = 4.0
    max_delay = 8.0
    max_retries = 2

    async def _scrape(self) -> list[FlyerProduct]:
        """Load Walmart search pages and extract products from __NEXT_DATA__.

        Stops after first bot detection to avoid burning the session.
        Prioritizes stalest categories first so incremental runs fill in
        coverage across the full category set.
        """
        page = self._page
        all_products: list[FlyerProduct] = []
        seen_ids: set[str] = set()

        # Warm the session with organic-looking browsing before searching
        await self._warm_session(page)

        # Determine which categories to scrape — stalest first
        all_cat_ids = list(CATEGORY_SEARCHES.keys())
        stale_ids = self._stale_categories(all_cat_ids)

        if not stale_ids:
            logger.info("Walmart: all categories fresh, nothing to scrape")
            return self._load_cache()

        for cat_id in stale_ids:
            search_terms = CATEGORY_SEARCHES.get(cat_id, [])
            for term in search_terms:
                try:
                    products = await self._search_products(page, term, cat_id, seen_ids)
                    if products:
                        all_products.extend(products)
                    else:
                        # Bot detection or empty — stop immediately to preserve session
                        logger.info("Walmart: stopping after %d products (blocked or empty)", len(all_products))
                        return all_products
                except Exception:
                    logger.exception("Walmart: failed search for '%s'", term)
                    return all_products
                await self._random_delay()

                # Occasionally browse a non-search page between searches to look organic
                if len(all_products) > 0 and random.random() < 0.3:
                    await self._browse_organic(page)

        return all_products

    async def _warm_session(self, page) -> None:
        """Browse Walmart organically before searching to establish a human-like session."""
        try:
            logger.info("Walmart: warming session with organic browsing")

            # Load homepage
            await page.goto(self.base_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(random.randint(2000, 4000))

            # Scroll down a bit like a real user
            await page.evaluate("window.scrollBy(0, Math.random() * 600 + 200)")
            await page.wait_for_timeout(random.randint(1000, 3000))

            # Click a department link if available
            dept_links = [
                "/cp/food/976759",
                "/cp/household-essentials/1115193",
                "/cp/pantry-snacks/1735450",
            ]
            dept = random.choice(dept_links)
            try:
                await page.goto(
                    f"{self.base_url}{dept}",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                await page.wait_for_timeout(random.randint(2000, 5000))
                await page.evaluate("window.scrollBy(0, Math.random() * 800 + 300)")
                await page.wait_for_timeout(random.randint(1000, 2000))
            except Exception:
                logger.debug("Walmart: department page browse failed, continuing")

            logger.info("Walmart: session warming complete")
        except Exception:
            logger.warning("Walmart: session warming failed, proceeding to search")

    async def _browse_organic(self, page) -> None:
        """Briefly visit a non-search page between searches to look organic."""
        try:
            filler_pages = [
                "/cp/food/976759",
                "/cp/household-essentials/1115193",
                "/",
            ]
            url = f"{self.base_url}{random.choice(filler_pages)}"
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(random.randint(2000, 4000))
            await page.evaluate("window.scrollBy(0, Math.random() * 500 + 100)")
            await page.wait_for_timeout(random.randint(1000, 2000))
        except Exception:
            pass  # Non-critical

    async def _search_products(
        self,
        page,
        search_term: str,
        category_hint: str,
        seen_ids: set[str],
    ) -> list[FlyerProduct]:
        """Load a Walmart search page and extract products from __NEXT_DATA__."""
        search_url = f"{self.base_url}/search?q={search_term}"

        await page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
        # Wait for SSR content to be in DOM
        await page.wait_for_timeout(3000)

        # Check for bot detection
        title = (await page.title()).lower()
        if "robot" in title or "human" in title or "blocked" in title:
            logger.warning("Walmart: bot detection triggered on '%s'", search_term)
            return []

        # Extract __NEXT_DATA__
        next_data_str = await page.evaluate(
            "() => document.getElementById('__NEXT_DATA__')?.textContent"
        )
        if not next_data_str:
            logger.warning("Walmart: no __NEXT_DATA__ for '%s'", search_term)
            return []

        try:
            data = json.loads(next_data_str)
        except json.JSONDecodeError:
            logger.warning("Walmart: invalid JSON in __NEXT_DATA__")
            return []

        return self._parse_next_data(data, category_hint, seen_ids)

    def _parse_next_data(
        self,
        data: dict,
        category_hint: str,
        seen_ids: set[str],
    ) -> list[FlyerProduct]:
        """Extract products from Walmart's __NEXT_DATA__ structure."""
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
            logger.warning("Walmart: unexpected __NEXT_DATA__ structure")
            return []

        for stack in stacks:
            items = stack.get("items", [])
            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("__typename") != "Product":
                    continue

                prod_id = str(item.get("usItemId", item.get("id", "")))
                if not prod_id or prod_id in seen_ids:
                    continue
                seen_ids.add(prod_id)

                name = (item.get("name") or "").strip()
                if not name:
                    continue

                # Price is a top-level numeric field in __NEXT_DATA__
                price_val = item.get("price")
                if price_val is None:
                    # Fallback to priceInfo structure
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

        logger.info("Walmart: parsed %d products from __NEXT_DATA__", len(products))
        return products
