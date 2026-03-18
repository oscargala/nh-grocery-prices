"""Abstract base class for Playwright-based store scrapers."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from abc import ABC, abstractmethod
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

from playwright.async_api import BrowserContext, Page, Response

from ...models import CategoryDef, FlyerProduct
from .browser import PlaywrightManager

CACHE_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data" / "cache"

logger = logging.getLogger(__name__)

# Default staleness threshold — skip categories scraped within this window
DEFAULT_STALENESS_DAYS = 7

# Common challenge page indicators
CHALLENGE_INDICATORS = [
    "checking your browser",
    "just a moment",
    "verify you are human",
    "please wait",
    "access denied",
    "attention required",
    "performing verification",
]


class PlaywrightStoreScraper(ABC):
    """Base class for Playwright-based store scrapers.

    Subclasses implement _scrape() with store-specific logic.
    The base handles browser context lifecycle, retries, and error handling.
    """

    store_id: str = ""
    store_name: str = ""
    base_url: str = ""
    min_delay: float = 2.0
    max_delay: float = 5.0
    max_retries: int = 3

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
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    async def collect_all(self) -> list[FlyerProduct]:
        """Main entry point. Sets up context, scrapes, tears down.

        On success, merges new products into the per-category cache.
        Always returns the full cache (all categories) so the dashboard
        shows data even for categories not scraped this run.
        On failure, falls back to the last successful cache.
        """
        for attempt in range(1, self.max_retries + 1):
            try:
                await self._setup()
                products = await self._scrape()
                await self._teardown()

                categorized = [p for p in products if p.category]
                logger.info(
                    "%s: %d new products, %d categorized (attempt %d)",
                    self.store_name,
                    len(products),
                    len(categorized),
                    attempt,
                )

                if products:
                    self._save_cache(products)
                # Return ALL cached products (including previously-scraped categories)
                return self._load_cache()

            except Exception:
                logger.exception(
                    "%s: scrape failed (attempt %d/%d)",
                    self.store_name,
                    attempt,
                    self.max_retries,
                )
                await self._teardown(save_cookies=False)

                # On challenge failure, clear cookies and retry with fresh session
                if attempt < self.max_retries:
                    cookie_path = self.manager._cookie_path(self.store_id)
                    if cookie_path.exists():
                        cookie_path.unlink()
                        logger.info("%s: cleared stale cookies for retry", self.store_name)
                    await asyncio.sleep(2)

        logger.error("%s: all %d attempts failed, loading cache", self.store_name, self.max_retries)
        return self._load_cache()

    def _cache_path(self) -> Path:
        return CACHE_DIR / f"{self.store_id}.json"

    def _load_cache_data(self) -> dict:
        """Load the raw cache dict from disk, handling both old and new formats."""
        path = self._cache_path()
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text())
            # Migrate old flat format to per-category format
            if "products" in data and "categories" not in data:
                return self._migrate_cache(data)
            return data
        except Exception:
            logger.exception("%s: failed to load cache", self.store_name)
            return {}

    @staticmethod
    def _migrate_cache(old_data: dict) -> dict:
        """Convert old flat cache format to per-category structure."""
        timestamp = old_data.get("timestamp", datetime.now().isoformat())
        by_cat: dict[str, list[dict]] = {}
        for p in old_data.get("products", []):
            cat = p.get("category") or "uncategorized"
            by_cat.setdefault(cat, []).append(p)

        categories = {}
        for cat_id, prods in by_cat.items():
            categories[cat_id] = {
                "scraped_at": timestamp,
                "products": prods,
            }
        return {"store_id": old_data.get("store_id", ""), "categories": categories}

    def _save_cache(self, products: list[FlyerProduct]) -> None:
        """Merge new products into per-category cache with timestamps."""
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        now = datetime.now().isoformat()

        # Load existing cache
        data = self._load_cache_data()
        data.setdefault("store_id", self.store_id)
        data.setdefault("categories", {})

        # Group new products by category
        by_cat: dict[str, list[dict]] = {}
        for p in products:
            cat = p.category or "uncategorized"
            by_cat.setdefault(cat, []).append(asdict(p))

        # Merge: for each category in new results, replace that category's data
        for cat_id, new_prods in by_cat.items():
            data["categories"][cat_id] = {
                "scraped_at": now,
                "products": new_prods,
            }

        self._cache_path().write_text(json.dumps(data, indent=2))

        total = sum(len(c["products"]) for c in data["categories"].values())
        logger.info(
            "%s: merged %d new products into cache (%d total across %d categories)",
            self.store_name,
            len(products),
            total,
            len(data["categories"]),
        )

    def _load_cache(self) -> list[FlyerProduct]:
        """Load all products from the per-category cache."""
        data = self._load_cache_data()
        cats = data.get("categories", {})
        if not cats:
            logger.info("%s: no cache file found", self.store_name)
            return []

        products = []
        for cat_id, cat_data in cats.items():
            for p in cat_data.get("products", []):
                try:
                    products.append(FlyerProduct(**p))
                except Exception:
                    pass

        oldest = min((c["scraped_at"] for c in cats.values()), default="unknown")
        newest = max((c["scraped_at"] for c in cats.values()), default="unknown")
        logger.info(
            "%s: loaded %d cached products across %d categories (oldest: %s, newest: %s)",
            self.store_name,
            len(products),
            len(cats),
            oldest[:10],
            newest[:10],
        )
        return products

    def _stale_categories(self, all_category_ids: list[str]) -> list[str]:
        """Return category IDs that are stale or missing from cache, stalest first."""
        data = self._load_cache_data()
        cats = data.get("categories", {})
        threshold = datetime.now() - timedelta(days=self.staleness_days)

        stale = []
        for cat_id in all_category_ids:
            # If caller specified a subset, only consider those
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

        # Sort stalest first
        stale.sort(key=lambda x: x[1])
        result = [cat_id for cat_id, _ in stale]

        if result:
            logger.info(
                "%s: %d/%d categories are stale: %s",
                self.store_name,
                len(result),
                len(all_category_ids),
                ", ".join(result),
            )
        else:
            logger.info("%s: all categories are fresh (within %d days)", self.store_name, self.staleness_days)

        return result

    @abstractmethod
    async def _scrape(self) -> list[FlyerProduct]:
        """Store-specific scraping logic. Implemented by subclasses."""
        ...

    async def _setup(self) -> None:
        """Create browser context and page for this store."""
        self._context = await self.manager.new_context(self.store_id)
        self._page = await self._context.new_page()

    async def _teardown(self, save_cookies: bool = True) -> None:
        """Save cookies and close context."""
        if self._context:
            if save_cookies:
                try:
                    await self.manager.save_cookies(self._context, self.store_id)
                except Exception:
                    logger.warning("%s: failed to save cookies", self.store_name)
            await self._context.close()
            self._context = None
            self._page = None

    async def _wait_for_challenge(self, page: Page, timeout: float = 30.0) -> bool:
        """Wait for anti-bot challenge pages to resolve.

        Polls page content for challenge indicators. Returns True if the page
        loaded real content, False if still stuck on a challenge after timeout.
        """
        deadline = asyncio.get_event_loop().time() + timeout
        checks = 0

        while asyncio.get_event_loop().time() < deadline:
            checks += 1
            try:
                # Check if we're on a challenge page
                title = (await page.title()).lower()
                content = (await page.content())[:3000].lower()

                is_challenge = any(
                    indicator in title or indicator in content
                    for indicator in CHALLENGE_INDICATORS
                )

                if not is_challenge:
                    if checks > 1:
                        logger.info("%s: challenge resolved after %d checks", self.store_name, checks)
                    return True

            except Exception:
                pass  # Page might be navigating

            await asyncio.sleep(1.0)

        logger.warning("%s: challenge not resolved after %.0fs", self.store_name, timeout)
        return False

    async def _intercept_api(
        self,
        page: Page,
        url_pattern: str,
        trigger,
        timeout: float = 20.0,
    ) -> dict | list | None:
        """Trigger a page action and capture a matching API response.

        Args:
            page: The Playwright page.
            url_pattern: Regex pattern to match against response URLs.
            trigger: An async callable that triggers the navigation/action.
            timeout: Max seconds to wait for the matching response.

        Returns parsed JSON from the first matching response, or None.
        """
        pattern = re.compile(url_pattern)
        captured: dict | list | None = None
        event = asyncio.Event()

        async def on_response(response: Response) -> None:
            nonlocal captured
            if captured is not None:
                return
            if pattern.search(response.url):
                try:
                    body = await response.json()
                    captured = body
                    event.set()
                    logger.debug(
                        "%s: intercepted API response from %s",
                        self.store_name,
                        response.url,
                    )
                except Exception:
                    pass  # Not JSON or failed to parse

        page.on("response", on_response)
        try:
            await trigger()
            try:
                await asyncio.wait_for(event.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning(
                    "%s: API intercept timed out for pattern %s",
                    self.store_name,
                    url_pattern,
                )
        finally:
            page.remove_listener("response", on_response)

        return captured

    async def _random_delay(self) -> None:
        """Sleep for a random duration between min_delay and max_delay."""
        delay = random.uniform(self.min_delay, self.max_delay)
        await asyncio.sleep(delay)
