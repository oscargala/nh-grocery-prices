"""Playwright-based scrapers for stores behind anti-bot protection."""

from .browser import PlaywrightManager
from .hannaford import HannafordScraper
from .shaws import ShawsScraper
from .walmart import WalmartScraper

# Active scrapers — only include stores that return data reliably
# Hannaford: blocked by Datadome captcha — needs captcha solving service
# Shaw's: SPA doesn't render product data on category pages — needs store selection flow
SCRAPERS = [
    WalmartScraper,
]

__all__ = [
    "PlaywrightManager",
    "HannafordScraper",
    "ShawsScraper",
    "WalmartScraper",
    "SCRAPERS",
]
