"""Playwright browser lifecycle manager with stealth, fingerprint rotation, and cookie persistence."""

from __future__ import annotations

import json
import logging
import os
import random
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

# ---------------------------------------------------------------------------
# Browser fingerprint profiles
# ---------------------------------------------------------------------------
# Each profile represents a realistic Firefox user with varying UA version,
# viewport size, and platform.  A random profile is selected per session so
# consecutive cron runs look like different users to Akamai / bot detectors.
# ---------------------------------------------------------------------------

BROWSER_PROFILES: list[dict] = [
    # Windows 10 — most common desktop OS
    {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0",
        "viewport": {"width": 1366, "height": 768},
        "platform": "Win32",
    },
    {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) Gecko/20100101 Firefox/130.0",
        "viewport": {"width": 1920, "height": 1080},
        "platform": "Win32",
    },
    {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:129.0) Gecko/20100101 Firefox/129.0",
        "viewport": {"width": 1536, "height": 864},
        "platform": "Win32",
    },
    {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
        "viewport": {"width": 1440, "height": 900},
        "platform": "Win32",
    },
    # Windows 11
    {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0",
        "viewport": {"width": 1920, "height": 1080},
        "platform": "Win32",
    },
    {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) Gecko/20100101 Firefox/130.0",
        "viewport": {"width": 2560, "height": 1440},
        "platform": "Win32",
    },
    # macOS
    {
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.6; rv:131.0) Gecko/20100101 Firefox/131.0",
        "viewport": {"width": 1440, "height": 900},
        "platform": "MacIntel",
    },
    {
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.6; rv:130.0) Gecko/20100101 Firefox/130.0",
        "viewport": {"width": 1680, "height": 1050},
        "platform": "MacIntel",
    },
    {
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.5; rv:129.0) Gecko/20100101 Firefox/129.0",
        "viewport": {"width": 1512, "height": 982},
        "platform": "MacIntel",
    },
    {
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.4; rv:128.0) Gecko/20100101 Firefox/128.0",
        "viewport": {"width": 1728, "height": 1117},
        "platform": "MacIntel",
    },
    # Linux desktop
    {
        "user_agent": "Mozilla/5.0 (X11; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0",
        "viewport": {"width": 1920, "height": 1080},
        "platform": "Linux x86_64",
    },
    {
        "user_agent": "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0",
        "viewport": {"width": 1366, "height": 768},
        "platform": "Linux x86_64",
    },
    {
        "user_agent": "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:129.0) Gecko/20100101 Firefox/129.0",
        "viewport": {"width": 1600, "height": 900},
        "platform": "Linux x86_64",
    },
    {
        "user_agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
        "viewport": {"width": 2560, "height": 1440},
        "platform": "Linux x86_64",
    },
    # Windows 10 — additional common resolutions
    {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
        "viewport": {"width": 1280, "height": 720},
        "platform": "Win32",
    },
]


class PlaywrightManager:
    """Manages a single Playwright browser instance across all store scrapers.

    Selects a random browser fingerprint profile per session so consecutive
    runs look like different users.

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
        profile: dict | None = None,
        proxy_url: str | None = None,
    ) -> None:
        self.headless = headless
        self.cookie_dir = cookie_dir or DEFAULT_COOKIE_DIR
        # Pick a random fingerprint profile for this session
        self.profile = profile or random.choice(BROWSER_PROFILES)
        # Proxy: explicit arg > PROXY_URL env var > no proxy
        self.proxy_url = proxy_url or os.environ.get("PROXY_URL")
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

    @property
    def user_agent(self) -> str:
        return self.profile["user_agent"]

    async def __aenter__(self) -> PlaywrightManager:
        await self.start()
        return self

    async def __aexit__(self, *args) -> None:
        await self.stop()

    async def start(self) -> None:
        """Launch Firefox with stealth-friendly configuration."""
        self._playwright = await async_playwright().start()

        launch_kwargs: dict = {
            "headless": self.headless,
            "firefox_user_prefs": {
                "privacy.resistFingerprinting": False,
                "media.peerconnection.enabled": False,
            },
        }
        if self.proxy_url:
            launch_kwargs["proxy"] = {"server": self.proxy_url}

        self._browser = await self._playwright.firefox.launch(**launch_kwargs)
        logger.info(
            "Browser started (headless=%s, proxy=%s, profile=%s %s)",
            self.headless,
            self.proxy_url or "none",
            self.profile["platform"],
            self.profile["viewport"],
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
        Uses the session's randomly-selected fingerprint profile.
        """
        if self._browser is None:
            raise RuntimeError("Browser not started — use 'async with' or call start()")

        context = await self._browser.new_context(
            viewport=self.profile["viewport"],
            user_agent=self.profile["user_agent"],
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

        # Override navigator.platform to match the selected profile
        platform = self.profile.get("platform", "Win32")
        await context.add_init_script(
            f"Object.defineProperty(navigator, 'platform', {{get: () => '{platform}'}})"
        )

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
