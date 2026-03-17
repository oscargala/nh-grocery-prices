"""Playwright browser lifecycle manager with stealth and cookie persistence."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from playwright.async_api import (
    BrowserContext,
    async_playwright,
    Playwright,
    Browser,
)
from playwright_stealth import Stealth

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data"
DEFAULT_COOKIE_DIR = DATA_DIR / "cookies"

# Recent Firefox on Windows — common residential profile
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0"
)


class PlaywrightManager:
    """Manages a single Playwright browser instance across all store scrapers.

    Usage:
        async with PlaywrightManager() as manager:
            ctx = await manager.new_context("hannaford")
            page = await ctx.new_page()
            ...
            await manager.save_cookies(ctx, "hannaford")
            await ctx.close()
    """

    def __init__(
        self,
        headless: bool = True,
        cookie_dir: Path | None = None,
        user_agent: str | None = None,
    ) -> None:
        self.headless = headless
        self.cookie_dir = cookie_dir or DEFAULT_COOKIE_DIR
        self.user_agent = user_agent or DEFAULT_USER_AGENT
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

    async def __aenter__(self) -> PlaywrightManager:
        await self.start()
        return self

    async def __aexit__(self, *args) -> None:
        await self.stop()

    async def start(self) -> None:
        """Launch Firefox with stealth-friendly configuration."""
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.firefox.launch(
            headless=self.headless,
            firefox_user_prefs={
                # Reduce fingerprinting signals
                "privacy.resistFingerprinting": False,  # breaks some sites if True
                "media.peerconnection.enabled": False,  # disable WebRTC IP leak
            },
        )
        logger.info(
            "Browser started (headless=%s, pid=%s)",
            self.headless,
            self._browser.contexts,
        )

    async def stop(self) -> None:
        """Close browser and playwright instance."""
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        logger.info("Browser stopped")

    async def new_context(
        self,
        store_id: str,
        headless: bool | None = None,
    ) -> BrowserContext:
        """Create a new browser context with stored cookies for this store.

        Each store gets its own context (isolated cookies/storage).
        """
        if self._browser is None:
            raise RuntimeError("Browser not started — use 'async with' or call start()")

        context = await self._browser.new_context(
            viewport={"width": 1366, "height": 768},
            user_agent=self.user_agent,
            locale="en-US",
            timezone_id="America/New_York",
            # Accept common web content
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "DNT": "1",
            },
        )

        # Apply stealth patches
        stealth = Stealth()
        await stealth.apply_stealth_async(context)

        # Load persisted cookies if available
        cookie_file = self._cookie_path(store_id)
        if cookie_file.exists():
            try:
                cookies = json.loads(cookie_file.read_text())
                await context.add_cookies(cookies)
                logger.info("Loaded %d cookies for %s", len(cookies), store_id)
            except (json.JSONDecodeError, Exception):
                logger.warning("Failed to load cookies for %s, starting fresh", store_id)

        return context

    async def save_cookies(self, context: BrowserContext, store_id: str) -> None:
        """Persist context cookies to disk for reuse across runs."""
        self.cookie_dir.mkdir(parents=True, exist_ok=True)
        cookies = await context.cookies()
        cookie_file = self._cookie_path(store_id)
        cookie_file.write_text(json.dumps(cookies, indent=2))
        logger.info("Saved %d cookies for %s", len(cookies), store_id)

    def _cookie_path(self, store_id: str) -> Path:
        return self.cookie_dir / f"{store_id}.json"
