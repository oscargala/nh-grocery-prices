"""Aldi.us catalog scraper — everyday (non-sale) prices from product pages."""

from __future__ import annotations

import logging
import re
import time

import requests
from bs4 import BeautifulSoup

from ..models import FlyerProduct
from ..normalize import categorize_item, load_categories

logger = logging.getLogger(__name__)

BASE_URL = "https://www.aldi.us"
USER_AGENT = "NHGroceryPrices/0.1 (community research)"

# Category pages to scrape — maps our category IDs to Aldi URL paths
ALDI_CATEGORIES = {
    "canned_vegetables": "/products/pantry-essentials/canned-foods/k/102",
    "canned_soups": "/products/pantry-essentials/soups-broth/k/105",
    "pasta": "/products/pantry-essentials/pasta-rice-grains/k/108",
    "rice": "/products/pantry-essentials/pasta-rice-grains/k/108",
    "oats": "/products/breakfast-cereals/cereal-oatmeal/k/162",
    "cereal": "/products/breakfast-cereals/cereal-oatmeal/k/162",
    "spices": "/products/pantry-essentials/spices/k/106",
    "frozen_vegetables": "/products/frozen-foods/frozen-vegetables/k/163",
    "frozen_meals": "/products/frozen-foods/frozen-meals-sides/k/137",
    "toilet_paper": "/products/household-essentials/paper-plastic-products/k/164",
    "paper_towels": "/products/household-essentials/paper-plastic-products/k/164",
    "diapers": "/products/baby-items/diapers-wipes-wash/k/53",
}

# Deduplicate URLs (several categories share the same page)
_UNIQUE_URLS = list(dict.fromkeys(ALDI_CATEGORIES.values()))

PRICE_RE = re.compile(r"\$(\d+\.\d{2})")
SIZE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(oz|lb|fl\.?\s*oz|ct|count|pk|rolls?|ea)",
    re.IGNORECASE,
)
MULTI_RE = re.compile(
    r"(\d+)\s*x\s*(\d+(?:\.\d+)?)\s*(oz|lb|fl\.?\s*oz)",
    re.IGNORECASE,
)


def _parse_size(text: str) -> tuple[float | None, str | None]:
    """Extract size and unit from text. Returns (amount, unit) or (None, None).

    Handles multi-packs like '6 x 4 oz' → (24.0, 'oz').
    """
    multi = MULTI_RE.search(text)
    if multi:
        count = int(multi.group(1))
        per = float(multi.group(2))
        unit = multi.group(3).lower().replace(".", "").replace(" ", "")
        return count * per, unit

    match = SIZE_RE.search(text)
    if match:
        amount = float(match.group(1))
        unit = match.group(2).lower().replace(".", "").replace(" ", "")
        return amount, unit

    return None, None


def _compute_unit_price(
    price: float, size: float | None, unit: str | None
) -> float | None:
    """Compute price per unit (oz, lb, ct, etc.)."""
    if size and size > 0:
        return round(price / size, 3)
    return None


def _fetch_page(url: str) -> BeautifulSoup | None:
    """Fetch a page and return parsed soup."""
    headers = {"User-Agent": USER_AGENT}
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "lxml")
    except requests.RequestException:
        logger.exception("Failed to fetch %s", url)
        return None


def _extract_products_from_page(soup: BeautifulSoup) -> list[dict]:
    """Extract product data from a parsed category page.

    Finds product links and extracts text nodes for brand, name, size, price.
    """
    products = []
    product_links = soup.find_all("a", href=re.compile(r"^/product/"))

    # Deduplicate by href (some pages repeat product links)
    seen_hrefs: set[str] = set()
    for link in product_links:
        href = link.get("href", "")
        if href in seen_hrefs:
            continue
        seen_hrefs.add(href)

        texts = [t.strip() for t in link.stripped_strings if t.strip()]
        if len(texts) < 2:
            continue

        # Find price (usually last text matching $X.XX)
        price = None
        price_idx = None
        for i in range(len(texts) - 1, -1, -1):
            m = PRICE_RE.search(texts[i])
            if m:
                price = float(m.group(1))
                price_idx = i
                break

        if price is None:
            continue

        # First text is typically brand, second is product name (may include size)
        brand = texts[0] if len(texts) > 2 else None
        name_parts = texts[1] if len(texts) > 2 else texts[0]

        # Try to find size in any of the text nodes
        full_text = " ".join(texts)
        size, unit = _parse_size(full_text)

        products.append({
            "name": name_parts,
            "brand": brand,
            "price": price,
            "size": size,
            "unit": unit,
            "unit_price": _compute_unit_price(price, size, unit),
            "url": BASE_URL + href,
        })

    return products


def _has_next_page(soup: BeautifulSoup, current_page: int) -> bool:
    """Check if there's a next page of results."""
    # Look for pagination links with the next page number
    next_page = str(current_page + 1)
    for a in soup.find_all("a", href=True):
        if f"page={next_page}" in a["href"]:
            return True
    # Also check for text content that looks like page numbers
    for el in soup.find_all(string=re.compile(rf"^\s*{next_page}\s*$")):
        return True
    return False


def scrape_category_pages(
    url_path: str, delay: float = 1.5, max_pages: int = 10
) -> list[dict]:
    """Scrape all pages of an Aldi category. Returns list of product dicts."""
    all_products = []
    page = 1

    while page <= max_pages:
        url = BASE_URL + url_path
        if page > 1:
            url += f"?page={page}"

        logger.info("Fetching Aldi page: %s", url)
        time.sleep(delay)
        soup = _fetch_page(url)
        if soup is None:
            break

        products = _extract_products_from_page(soup)
        if not products:
            break

        all_products.extend(products)
        logger.info("Page %d: %d products", page, len(products))

        if not _has_next_page(soup, page):
            break
        page += 1

    return all_products


def collect_all(delay: float = 1.5) -> list[FlyerProduct]:
    """Scrape all target Aldi categories and return normalized FlyerProducts.

    Products are tagged with price_type='everyday' to distinguish from
    Flipp sale prices.
    """
    categories = load_categories()
    seen_urls: set[str] = set()
    all_raw: list[dict] = []

    for url_path in _UNIQUE_URLS:
        raw = scrape_category_pages(url_path, delay=delay)
        for product in raw:
            if product["url"] not in seen_urls:
                seen_urls.add(product["url"])
                all_raw.append(product)

    logger.info("Aldi: %d unique products scraped", len(all_raw))

    products = []
    for raw in all_raw:
        name = raw["name"]
        category = categorize_item(name, categories)
        # Also try matching with brand prepended
        if category is None and raw.get("brand"):
            category = categorize_item(f"{raw['brand']} {name}", categories)

        products.append(
            FlyerProduct(
                name=name,
                store="Aldi",
                price=raw["price"],
                brand=raw.get("brand"),
                category=category,
                valid_from=None,
                valid_to=None,
                flyer_id=None,
                item_id=None,
                price_type="everyday",
                size=raw.get("size"),
                unit=raw.get("unit"),
                unit_price=raw.get("unit_price"),
            )
        )

    categorized = [p for p in products if p.category]
    logger.info(
        "Aldi catalog: %d products, %d categorized",
        len(products),
        len(categorized),
    )
    return products
