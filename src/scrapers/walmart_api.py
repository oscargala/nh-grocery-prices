"""Walmart GraphQL API scraper — captures auth tokens via Playwright, then
replays search queries directly via httpx for all categories without browser
overhead.

Strategy:
1. Open one Playwright session, do a single search to capture the GraphQL
   request (URL, headers, query shape, auth tokens).
2. Close the browser.
3. Replay the same GraphQL endpoint with httpx for all remaining categories,
   reusing the captured headers/cookies.

This avoids per-search browser overhead and can get all 12 categories in a
single session's token lifetime.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus

import httpx
from playwright.async_api import Page, Response

from ..models import CategoryDef, FlyerProduct
from ..normalize import categorize_item, parse_price
from .playwright.base import CACHE_DIR, DEFAULT_STALENESS_DAYS
from .playwright.browser import PlaywrightManager

logger = logging.getLogger(__name__)

# Search terms per category (same as the Playwright walmart scraper)
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

# Directory to persist captured API credentials between runs
TOKEN_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "tokens"


def _parse_size_from_text(text: str) -> tuple[float | None, str | None]:
    match = SIZE_RE.search(text)
    if match:
        return float(match.group(1)), match.group(2).lower().replace(".", "").replace(" ", "")
    return None, None


class WalmartAPISession:
    """Captures and stores the headers/cookies needed to replay Walmart's
    internal GraphQL Search API directly via httpx."""

    def __init__(self) -> None:
        self.endpoint: str | None = None
        self.headers: dict[str, str] = {}
        self.cookies: dict[str, str] = {}
        self.query_template: dict | None = None

    def is_valid(self) -> bool:
        return bool(self.endpoint and self.headers)

    def save(self) -> None:
        """Persist captured session to disk for reuse."""
        TOKEN_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "endpoint": self.endpoint,
            "headers": self.headers,
            "cookies": self.cookies,
            "captured_at": datetime.now().isoformat(),
        }
        (TOKEN_DIR / "walmart_graphql.json").write_text(json.dumps(data, indent=2))
        logger.info("Saved Walmart GraphQL session to disk")

    @classmethod
    def load(cls) -> WalmartAPISession | None:
        """Load a previously captured session from disk."""
        path = TOKEN_DIR / "walmart_graphql.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            # Check if the session is less than 2 hours old
            captured = datetime.fromisoformat(data["captured_at"])
            if datetime.now() - captured > timedelta(hours=2):
                logger.info("Walmart GraphQL session expired (>2h old)")
                return None
            session = cls()
            session.endpoint = data["endpoint"]
            session.headers = data["headers"]
            session.cookies = data["cookies"]
            logger.info("Loaded Walmart GraphQL session from disk (age: %s)", datetime.now() - captured)
            return session
        except Exception:
            logger.warning("Failed to load Walmart GraphQL session")
            return None


class WalmartAPIScraper:
    """Scrapes Walmart prices by capturing GraphQL tokens via Playwright,
    then replaying API calls directly via httpx.

    Falls back to the standard Playwright scraper's cache if token capture fails.
    """

    store_id = "walmart"
    store_name = "Walmart"

    def __init__(
        self,
        manager: PlaywrightManager,
        categories: list[CategoryDef],
        categories_to_scrape: list[str] | None = None,
        staleness_days: int = DEFAULT_STALENESS_DAYS,
    ) -> None:
        self.manager = manager
        self.categories = categories
        self.categories_to_scrape = categories_to_scrape
        self.staleness_days = staleness_days

    async def collect_all(self) -> list[FlyerProduct]:
        """Main entry point: capture tokens, then replay API for all stale categories."""
        # Determine which categories need scraping
        all_cat_ids = list(CATEGORY_SEARCHES.keys())
        stale_ids = self._stale_categories(all_cat_ids)

        if not stale_ids:
            logger.info("WalmartAPI: all categories fresh, nothing to scrape")
            return self._load_cache()

        # Try to reuse a saved session first
        session = WalmartAPISession.load()

        if not session or not session.is_valid():
            # Capture fresh tokens via Playwright
            session = await self._capture_session()
            if not session or not session.is_valid():
                logger.warning("WalmartAPI: failed to capture GraphQL session, falling back to cache")
                return self._load_cache()
            session.save()

        # Replay API calls for all stale categories
        products = await self._replay_searches(session, stale_ids)

        if products:
            self._save_cache(products)

        return self._load_cache()

    async def _capture_session(self) -> WalmartAPISession | None:
        """Open a Playwright browser, do one search, capture the GraphQL
        request details (endpoint, headers, cookies)."""
        session = WalmartAPISession()
        captured_event = asyncio.Event()

        context = await self.manager.new_context(self.store_id)
        page = await context.new_page()

        async def on_response(response: Response) -> None:
            url = response.url
            # Look for Walmart's search GraphQL endpoint
            if "/orchestra/graphql" not in url and "/api/graphql" not in url:
                return
            # Must be a search-related call
            request = response.request
            if request.method != "POST":
                return

            try:
                req_body = request.post_data
                if not req_body:
                    return
                body_json = json.loads(req_body)
                # Look for search-related queries
                query_name = body_json.get("query", "")
                variables = body_json.get("variables", {})
                # Walmart uses query names like "Search" or operation names
                if not (
                    "search" in query_name.lower()
                    or "search" in str(variables).lower()
                    or "Search" in body_json.get("operationName", "")
                ):
                    return

                # Capture the endpoint and headers
                session.endpoint = url.split("?")[0]  # Strip query params
                all_headers = await request.all_headers()
                # Keep headers that are needed for auth
                for key in [
                    "accept", "content-type", "user-agent", "x-o-correlation-id",
                    "x-o-gql-query", "x-o-segment", "x-o-bu", "x-o-ccm",
                    "x-o-mart", "x-o-platform", "x-o-platform-version",
                    "wm_mp", "wm_page_url", "wm_qos.correlation_id",
                    "x-latency-trace", "x-apollo-operation-name",
                    "device_profile_ref_id",
                ]:
                    val = all_headers.get(key)
                    if val:
                        session.headers[key] = val
                # Also capture any custom headers starting with wm_ or x-o-
                for key, val in all_headers.items():
                    if key.startswith(("wm_", "x-o-", "x-apollo")):
                        session.headers[key] = val

                # Save query template for replay
                session.query_template = body_json

                logger.info(
                    "WalmartAPI: captured GraphQL endpoint %s with %d headers",
                    session.endpoint,
                    len(session.headers),
                )
                captured_event.set()

            except Exception:
                logger.debug("WalmartAPI: failed to parse GraphQL request", exc_info=True)

        page.on("response", on_response)

        try:
            # Warm session
            await page.goto("https://www.walmart.com", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(random.randint(2000, 4000))
            await page.evaluate("window.scrollBy(0, Math.random() * 400 + 200)")
            await page.wait_for_timeout(random.randint(1000, 2000))

            # Do a search to trigger the GraphQL call
            search_term = random.choice(["canned vegetables", "pasta", "cereal", "rice"])
            search_url = f"https://www.walmart.com/search?q={quote_plus(search_term)}"
            await page.goto(search_url, wait_until="domcontentloaded", timeout=45000)

            # Wait for the GraphQL intercept
            try:
                await asyncio.wait_for(captured_event.wait(), timeout=15.0)
            except asyncio.TimeoutError:
                logger.warning("WalmartAPI: GraphQL intercept timed out")

            # Capture cookies from the browser context
            browser_cookies = await context.cookies()
            for cookie in browser_cookies:
                session.cookies[cookie["name"]] = cookie["value"]

        except Exception:
            logger.exception("WalmartAPI: session capture failed")
        finally:
            page.remove_listener("response", on_response)
            try:
                await self.manager.save_cookies(context, self.store_id)
            except Exception:
                pass
            await context.close()

        return session if session.is_valid() else None

    async def _replay_searches(
        self,
        session: WalmartAPISession,
        category_ids: list[str],
    ) -> list[FlyerProduct]:
        """Replay GraphQL search calls via httpx for each stale category."""
        all_products: list[FlyerProduct] = []
        seen_ids: set[str] = set()

        async with httpx.AsyncClient(
            timeout=30.0,
            cookies=session.cookies,
            follow_redirects=True,
        ) as client:
            for cat_id in category_ids:
                search_terms = CATEGORY_SEARCHES.get(cat_id, [])
                for term in search_terms:
                    products = await self._api_search(
                        client, session, term, cat_id, seen_ids
                    )
                    if products is None:
                        # Token expired or blocked — stop
                        logger.warning(
                            "WalmartAPI: API call failed for '%s', stopping (%d products so far)",
                            term, len(all_products),
                        )
                        return all_products
                    all_products.extend(products)
                    logger.info(
                        "WalmartAPI: '%s' -> %d products (%d total)",
                        term, len(products), len(all_products),
                    )
                    # Brief delay between API calls
                    await asyncio.sleep(random.uniform(1.0, 3.0))

        return all_products

    async def _api_search(
        self,
        client: httpx.AsyncClient,
        session: WalmartAPISession,
        search_term: str,
        category_hint: str,
        seen_ids: set[str],
    ) -> list[FlyerProduct] | None:
        """Execute a single GraphQL search and parse the response.

        Returns None if the request failed (token expired / blocked).
        Returns empty list if the search returned no results.
        """
        if not session.query_template:
            # If we didn't capture a query template, try __NEXT_DATA__ via HTTP
            return await self._fallback_http_search(client, session, search_term, category_hint, seen_ids)

        # Build the request body from the captured template
        body = json.loads(json.dumps(session.query_template))  # deep copy
        # Update the search query in the variables
        if "variables" in body:
            variables = body["variables"]
            # Walmart GraphQL uses various variable names for search
            for key in ["query", "searchQuery", "q"]:
                if key in variables:
                    variables[key] = search_term
                    break
            else:
                # Try to find and update any string value that looks like a search term
                for key, val in variables.items():
                    if isinstance(val, str) and len(val) > 2 and len(val) < 100:
                        variables[key] = search_term
                        break

        try:
            resp = await client.post(
                session.endpoint,
                json=body,
                headers=session.headers,
            )
        except httpx.HTTPError:
            logger.warning("WalmartAPI: HTTP error for '%s'", search_term)
            return None

        if resp.status_code == 403:
            logger.warning("WalmartAPI: 403 Forbidden — token likely expired")
            # Clear saved session
            token_path = TOKEN_DIR / "walmart_graphql.json"
            if token_path.exists():
                token_path.unlink()
            return None

        if resp.status_code != 200:
            logger.warning("WalmartAPI: HTTP %d for '%s'", resp.status_code, search_term)
            return None

        try:
            data = resp.json()
        except Exception:
            logger.warning("WalmartAPI: invalid JSON response for '%s'", search_term)
            return None

        return self._parse_graphql_response(data, category_hint, seen_ids)

    async def _fallback_http_search(
        self,
        client: httpx.AsyncClient,
        session: WalmartAPISession,
        search_term: str,
        category_hint: str,
        seen_ids: set[str],
    ) -> list[FlyerProduct] | None:
        """Fallback: fetch the search page via HTTP and parse __NEXT_DATA__."""
        url = f"https://www.walmart.com/search?q={quote_plus(search_term)}"
        try:
            resp = await client.get(
                url,
                headers={
                    "User-Agent": session.headers.get("user-agent", ""),
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
        except httpx.HTTPError:
            return None

        if resp.status_code != 200:
            return None

        # Extract __NEXT_DATA__ from HTML
        match = re.search(
            r'<script\s+id="__NEXT_DATA__"\s+type="application/json">(.*?)</script>',
            resp.text,
            re.DOTALL,
        )
        if not match:
            return None

        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            return None

        return self._parse_next_data(data, category_hint, seen_ids)

    def _parse_graphql_response(
        self,
        data: dict,
        category_hint: str,
        seen_ids: set[str],
    ) -> list[FlyerProduct]:
        """Parse Walmart's GraphQL search response into FlyerProducts."""
        products = []

        # GraphQL responses wrap data in {"data": {"search": {...}}}
        # Try several known structures
        search_result = None
        if "data" in data:
            gql_data = data["data"]
            for key in ["search", "searchResult", "productSearch"]:
                if key in gql_data:
                    search_result = gql_data[key]
                    break
            if search_result is None:
                # Try nested: data -> search -> searchResult
                for key in gql_data:
                    if isinstance(gql_data[key], dict):
                        for subkey in ["itemStacks", "items", "products"]:
                            if subkey in gql_data[key]:
                                search_result = gql_data[key]
                                break
                        if search_result:
                            break

        if not search_result:
            # Maybe the response IS the search result (no wrapper)
            if "itemStacks" in data:
                search_result = data
            else:
                logger.warning("WalmartAPI: could not find search results in GraphQL response")
                return []

        # Extract items from itemStacks (same structure as __NEXT_DATA__)
        stacks = search_result.get("itemStacks", [])
        if not stacks and "items" in search_result:
            stacks = [{"items": search_result["items"]}]

        for stack in stacks:
            items = stack.get("items", [])
            for item in items:
                product = self._parse_item(item, category_hint, seen_ids)
                if product:
                    products.append(product)

        return products

    def _parse_next_data(
        self,
        data: dict,
        category_hint: str,
        seen_ids: set[str],
    ) -> list[FlyerProduct]:
        """Parse __NEXT_DATA__ structure (fallback path, same as Playwright scraper)."""
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
            return []

        for stack in stacks:
            for item in stack.get("items", []):
                product = self._parse_item(item, category_hint, seen_ids)
                if product:
                    products.append(product)

        return products

    def _parse_item(
        self,
        item: dict,
        category_hint: str,
        seen_ids: set[str],
    ) -> FlyerProduct | None:
        """Parse a single product item dict into a FlyerProduct."""
        if not isinstance(item, dict):
            return None
        if item.get("__typename") not in ("Product", None):
            if item.get("__typename") != "Product":
                return None

        prod_id = str(item.get("usItemId", item.get("id", "")))
        if not prod_id or prod_id in seen_ids:
            return None
        seen_ids.add(prod_id)

        name = (item.get("name") or "").strip()
        if not name:
            return None

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
            return None

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

        return FlyerProduct(
            name=name,
            store="Walmart",
            price=price,
            brand=brand,
            category=category,
            price_type="everyday",
            size=size,
            unit=unit,
            unit_price=unit_price,
        )

    # --- Cache management (reuses the same cache file as Playwright scraper) ---

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
        logger.info("WalmartAPI: merged %d products into cache (%d total)", len(products), total)

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
        logger.info("WalmartAPI: loaded %d products from cache (%d categories)", len(products), len(cats))
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
            logger.info("WalmartAPI: %d/%d categories stale: %s", len(result), len(all_category_ids), ", ".join(result))
        return result
