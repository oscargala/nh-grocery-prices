"""Hannaford scraper — everyday catalog prices via internal API behind Cloudflare."""

from __future__ import annotations

import logging
import re

from ...models import FlyerProduct
from ...normalize import categorize_item, parse_price
from .base import PlaywrightStoreScraper

logger = logging.getLogger(__name__)

# Anonymous user + Concord NH store
USER_ID = "2"
SERVICE_LOCATION_ID = "50002092"
API_PATH = f"/api/v5.0/products/{USER_ID}/{SERVICE_LOCATION_ID}"

# Map our categories to Hannaford search terms
CATEGORY_SEARCHES: dict[str, list[str]] = {
    "canned_vegetables": ["canned vegetables", "canned beans"],
    "canned_soups": ["soup", "broth", "chili"],
    "pasta": ["pasta", "spaghetti", "noodles"],
    "rice": ["rice"],
    "oats": ["oatmeal", "oats"],
    "cereal": ["cereal"],
    "spices": ["spices", "seasoning"],
    "frozen_vegetables": ["frozen vegetables"],
    "frozen_meals": ["frozen dinners", "pot pies"],
    "toilet_paper": ["toilet paper", "bath tissue"],
    "paper_towels": ["paper towels"],
    "diapers": ["diapers", "baby wipes"],
}

# Size/unit parsing from product descriptions
SIZE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(oz|lb|fl\.?\s*oz|ct|count|pk|rolls?|ea)",
    re.IGNORECASE,
)


def _parse_size_from_text(text: str) -> tuple[float | None, str | None]:
    """Extract size and unit from product name/description."""
    match = SIZE_RE.search(text)
    if match:
        return float(match.group(1)), match.group(2).lower().replace(".", "").replace(" ", "")
    return None, None


class HannafordScraper(PlaywrightStoreScraper):
    """Scrapes Hannaford everyday prices via their internal product API."""

    store_id = "hannaford"
    store_name = "Hannaford"
    base_url = "https://www.hannaford.com"
    min_delay = 2.0
    max_delay = 5.0

    async def _scrape(self) -> list[FlyerProduct]:
        """Navigate to Hannaford, bypass Cloudflare, intercept product API."""
        page = self._page
        all_products: list[FlyerProduct] = []
        seen_ids: set[str] = set()

        # Step 1: Load homepage to establish session and pass Cloudflare
        logger.info("Hannaford: loading homepage to pass Cloudflare...")
        await page.goto(self.base_url, wait_until="domcontentloaded", timeout=45000)
        challenge_ok = await self._wait_for_challenge(page, timeout=30)
        if not challenge_ok:
            raise RuntimeError("Hannaford: stuck on Cloudflare challenge")

        logger.info("Hannaford: Cloudflare passed, starting product searches")
        await self._random_delay()

        # Step 2: Search for each category and intercept API responses
        for cat_id, search_terms in CATEGORY_SEARCHES.items():
            for term in search_terms:
                try:
                    products = await self._search_products(page, term, cat_id, seen_ids)
                    all_products.extend(products)
                except Exception:
                    logger.exception("Hannaford: failed search for '%s'", term)
                await self._random_delay()

        return all_products

    async def _search_products(
        self,
        page,
        search_term: str,
        category_hint: str,
        seen_ids: set[str],
    ) -> list[FlyerProduct]:
        """Execute a search and intercept the product API response."""
        search_url = (
            f"{self.base_url}/shop/search?query={search_term}"
            f"&sort=bestMatch+asc&rows=40&start=0"
        )

        api_data = await self._intercept_api(
            page,
            url_pattern=r"/api/v[0-9.]+/products/",
            trigger=lambda: page.goto(search_url, wait_until="domcontentloaded", timeout=30000),
            timeout=20.0,
        )

        if api_data is None:
            logger.warning("Hannaford: no API response for '%s'", search_term)
            return []

        return self._parse_api_response(api_data, category_hint, seen_ids)

    def _parse_api_response(
        self,
        data: dict | list,
        category_hint: str,
        seen_ids: set[str],
    ) -> list[FlyerProduct]:
        """Parse Hannaford API response into FlyerProduct objects."""
        products = []

        # The API response structure may vary — try common shapes
        items = []
        if isinstance(data, dict):
            items = data.get("products", data.get("items", data.get("results", [])))
            # Sometimes nested under a 'data' key
            if not items and "data" in data:
                inner = data["data"]
                if isinstance(inner, dict):
                    items = inner.get("products", inner.get("items", []))
                elif isinstance(inner, list):
                    items = inner
        elif isinstance(data, list):
            items = data

        for item in items:
            if not isinstance(item, dict):
                continue

            prod_id = str(item.get("prodId", item.get("id", "")))
            if not prod_id or prod_id in seen_ids:
                continue
            seen_ids.add(prod_id)

            name = item.get("name", "").strip()
            if not name:
                continue

            # Get price — prefer regularPrice for everyday, fall back to price
            price_val = item.get("regularPrice") or item.get("price")
            price = parse_price(str(price_val)) if price_val else None
            if price is None or price <= 0:
                continue

            brand = item.get("brand", "").strip() or None
            description = item.get("description", "") or ""

            # Size/unit — check dedicated fields first, then parse from name/description
            size = item.get("size")
            unit = None
            unit_price_val = item.get("unitPrice")

            if isinstance(size, str):
                parsed_size, parsed_unit = _parse_size_from_text(size)
                size = parsed_size
                unit = parsed_unit

            if size is None:
                # Try parsing from name or description
                for text in [name, description]:
                    s, u = _parse_size_from_text(text)
                    if s:
                        size, unit = s, u
                        break

            # Unit price from API or computed
            unit_price = None
            if unit_price_val:
                unit_price = parse_price(str(unit_price_val))
            elif size and size > 0:
                unit_price = round(price / size, 3)

            # Categorize using our keyword engine
            category = categorize_item(name, self.categories)
            if category is None and brand:
                category = categorize_item(f"{brand} {name}", self.categories)
            # Use category hint if our engine doesn't match but the search was targeted
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

        logger.info(
            "Hannaford: parsed %d products from API response",
            len(products),
        )
        return products
