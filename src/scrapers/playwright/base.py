"""Abstract base class for Playwright-based store scrapers."""

from __future__ import annotations

import asyncio
import logging
import random
import re
from abc import ABC, abstractmethod

from playwright.async_api import BrowserContext, Page, Response

from ...models import CategoryDef, FlyerProduct
from .browser import PlaywrightManager

logger = logging.getLogger(__name__)

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
    ) -> None:
        self.manager = manager
        self.categories = categories
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    async def collect_all(self) -> list[FlyerProduct]:
        """Main entry point. Sets up context, scrapes, tears down.

        Returns [] on failure instead of crashing the pipeline.
        """
        for attempt in range(1, self.max_retries + 1):
            try:
                await self._setup()
                products = await self._scrape()
                await self._teardown()

                categorized = [p for p in products if p.category]
                logger.info(
                    "%s: %d products, %d categorized (attempt %d)",
                    self.store_name,
                    len(products),
                    len(categorized),
                    attempt,
                )
                return products

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

        logger.error("%s: all %d attempts failed", self.store_name, self.max_retries)
        return []

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
