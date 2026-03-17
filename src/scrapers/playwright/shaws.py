"""Shaw's scraper — everyday catalog prices via Albertsons xapi behind Imperva."""

from __future__ import annotations

import logging
import re

from ...models import FlyerProduct
from ...normalize import categorize_item, parse_price
from .base import PlaywrightStoreScraper

logger = logging.getLogger(__name__)

# Category browse URLs — more reliable than search for SPAs
CATEGORY_PAGES: dict[str, list[str]] = {
    "canned_vegetables": [
        "/shop/aisles/canned-goods-soups/canned-vegetables.2579.html",
    ],
    "canned_soups": [
        "/shop/aisles/canned-goods-soups/canned-soups.2577.html",
    ],
    "pasta": [
        "/shop/aisles/pasta-sauces-grain/pasta.2532.html",
    ],
    "rice": [
        "/shop/aisles/pasta-sauces-grain/rice-grains.2534.html",
    ],
    "oats": [
        "/shop/aisles/breakfast/hot-cereal-pancake-mix.2453.html",
    ],
    "cereal": [
        "/shop/aisles/breakfast/cereal.2449.html",
    ],
    "spices": [
        "/shop/aisles/condiment-sauces/spices-seasonings.2485.html",
    ],
    "frozen_vegetables": [
        "/shop/aisles/frozen/frozen-vegetables.2523.html",
    ],
    "frozen_meals": [
        "/shop/aisles/frozen/frozen-meals-sides.2517.html",
    ],
    "toilet_paper": [
        "/shop/aisles/household/paper-plastic/toilet-paper.3203.html",
    ],
    "paper_towels": [
        "/shop/aisles/household/paper-plastic/paper-towels.3201.html",
    ],
    "diapers": [
        "/shop/aisles/baby/diapers-wipes/diapers.3162.html",
    ],
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


class ShawsScraper(PlaywrightStoreScraper):
    """Scrapes Shaw's everyday prices via the Albertsons xapi."""

    store_id = "shaws"
    store_name = "Shaw's"
    base_url = "https://www.shaws.com"
    min_delay = 2.5
    max_delay = 5.0

    async def _scrape(self) -> list[FlyerProduct]:
        """Navigate to Shaw's category pages and intercept xapi product responses."""
        page = self._page
        all_products: list[FlyerProduct] = []
        seen_ids: set[str] = set()

        # Step 1: Load homepage to establish session
        logger.info("Shaw's: loading homepage...")
        await page.goto(self.base_url, wait_until="domcontentloaded", timeout=45000)
        challenge_ok = await self._wait_for_challenge(page, timeout=20)
        if not challenge_ok:
            raise RuntimeError("Shaw's: stuck on challenge page")

        logger.info("Shaw's: session established, browsing categories")
        await self._random_delay()

        # Step 2: Browse category pages and intercept product API calls
        for cat_id, pages in CATEGORY_PAGES.items():
            for page_path in pages:
                try:
                    products = await self._browse_category(page, page_path, cat_id, seen_ids)
                    all_products.extend(products)
                except Exception:
                    logger.exception("Shaw's: failed category %s", page_path)
                await self._random_delay()

        return all_products

    async def _browse_category(
        self,
        page,
        page_path: str,
        category_hint: str,
        seen_ids: set[str],
    ) -> list[FlyerProduct]:
        """Browse a Shaw's category page and intercept product API responses."""
        url = f"{self.base_url}{page_path}"

        # Intercept xapi product responses
        api_data = await self._intercept_api(
            page,
            url_pattern=r"xapi.*(?:product|pgmsearch|aisles)",
            trigger=lambda: page.goto(url, wait_until="domcontentloaded", timeout=30000),
            timeout=15.0,
        )

        if api_data is not None:
            products = self._parse_xapi_response(api_data, category_hint, seen_ids)
            if products:
                return products

        # Fallback: try scraping visible product data from the DOM
        logger.info("Shaw's: no API intercept for %s, trying DOM scrape", page_path)
        return await self._scrape_dom(page, category_hint, seen_ids)

    async def _scrape_dom(
        self,
        page,
        category_hint: str,
        seen_ids: set[str],
    ) -> list[FlyerProduct]:
        """Fallback: extract product data from the rendered DOM."""
        products = []
        try:
            # Wait for product tiles to render
            await page.wait_for_timeout(3000)

            items = await page.evaluate("""() => {
                const products = [];
                // Shaw's uses product-card or product-item-v2 components
                const cards = document.querySelectorAll(
                    '[class*="ProductCard"], [class*="product-card"], [data-qa="productTile"]'
                );
                cards.forEach(card => {
                    const name = card.querySelector(
                        '[class*="ProductName"], [class*="product-name"], [data-qa="product-name"]'
                    )?.textContent?.trim() || '';
                    const price = card.querySelector(
                        '[class*="ProductPrice"], [class*="product-price"], [data-qa="product-price"]'
                    )?.textContent?.trim() || '';
                    const brand = card.querySelector(
                        '[class*="ProductBrand"], [class*="product-brand"]'
                    )?.textContent?.trim() || '';
                    const size = card.querySelector(
                        '[class*="ProductSize"], [class*="product-size"]'
                    )?.textContent?.trim() || '';
                    if (name && price) {
                        products.push({name, price, brand, size});
                    }
                });
                return products;
            }""")

            for item in items:
                name = item.get("name", "").strip()
                if not name:
                    continue

                # Deduplicate by name (no product IDs from DOM)
                name_key = name.lower()
                if name_key in seen_ids:
                    continue
                seen_ids.add(name_key)

                price = parse_price(item.get("price"))
                if price is None or price <= 0:
                    continue

                brand = item.get("brand", "").strip() or None
                size_str = item.get("size", "") or name

                size, unit = _parse_size_from_text(size_str)
                if size is None:
                    size, unit = _parse_size_from_text(name)

                unit_price = round(price / size, 3) if size and size > 0 else None

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

            logger.info("Shaw's: scraped %d products from DOM", len(products))
        except Exception:
            logger.exception("Shaw's: DOM scrape failed")

        return products

    def _parse_xapi_response(
        self,
        data: dict | list,
        category_hint: str,
        seen_ids: set[str],
    ) -> list[FlyerProduct]:
        """Parse Albertsons xapi response into FlyerProduct objects."""
        products = []

        # Find product items in the response at any nesting level
        items = self._find_products(data)
        if not items:
            return []

        for item in items:
            if not isinstance(item, dict):
                continue

            prod_id = str(item.get("productId", item.get("pid", item.get("id", ""))))
            if not prod_id or prod_id in seen_ids:
                continue
            seen_ids.add(prod_id)

            name = (item.get("name") or item.get("description") or "").strip()
            if not name:
                continue

            price_val = (
                item.get("basePrice")
                or item.get("regularPrice")
                or item.get("price")
                or item.get("currentPrice")
            )
            price = parse_price(str(price_val)) if price_val else None
            if price is None or price <= 0:
                continue

            brand = (item.get("brand", "") or "").strip() or None
            size_str = item.get("unitSize", item.get("size", "")) or ""

            size, unit = _parse_size_from_text(size_str)
            if size is None:
                size, unit = _parse_size_from_text(name)

            unit_price_val = item.get("unitPrice", item.get("pricePerUnit"))
            unit_price = parse_price(str(unit_price_val)) if unit_price_val else None
            if unit_price is None and size and size > 0:
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

        logger.info("Shaw's: parsed %d products from xapi response", len(products))
        return products

    def _find_products(self, data, depth: int = 0) -> list[dict]:
        """Recursively find product arrays in an API response."""
        if depth > 4:
            return []
        if isinstance(data, dict):
            for key in ("products", "items", "primaryProducts", "results"):
                if key in data and isinstance(data[key], list) and data[key]:
                    first = data[key][0]
                    if isinstance(first, dict) and any(
                        k in first for k in ("name", "description", "productId", "price")
                    ):
                        return data[key]
            for v in data.values():
                if isinstance(v, (dict, list)):
                    result = self._find_products(v, depth + 1)
                    if result:
                        return result
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, (dict, list)):
                    result = self._find_products(item, depth + 1)
                    if result:
                        return result
        return []
