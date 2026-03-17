"""Reusable Flipp API client for fetching flyer and item data."""

import logging
import random
import time
from dataclasses import dataclass, field

import requests

logger = logging.getLogger(__name__)

FLYERS_BASE = "https://flyers-ng.flippback.com/api/flipp"
SEARCH_BASE = "https://backflipp.wishabi.com/flipp/items/search"


def _generate_sid() -> str:
    """Generate a random 16-digit numeric session ID."""
    return str(random.randint(10**15, 10**16 - 1))


@dataclass
class FlippClient:
    """Client for the Flipp flyer/search API."""

    postal_code: str = "03301"
    delay: float = 1.0
    user_agent: str = "NHGroceryPrices/0.1 (community research)"
    sid: str = field(default_factory=_generate_sid)

    def _get(self, url: str, params: dict | None = None) -> dict | list | None:
        """Make a GET request with delay, logging, and error handling."""
        time.sleep(self.delay)
        headers = {"User-Agent": self.user_agent}
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException:
            logger.exception("Request failed: %s", url)
            return None

    def get_flyers(self) -> dict | None:
        """Fetch available flyers for the configured postal code."""
        url = f"{FLYERS_BASE}/data"
        params = {
            "locale": "en",
            "postal_code": self.postal_code,
            "sid": self.sid,
        }
        logger.info("Fetching flyers for postal code %s", self.postal_code)
        return self._get(url, params)

    def get_flyer_items(self, flyer_id: int) -> list[dict]:
        """Fetch all items from a specific flyer."""
        url = f"{FLYERS_BASE}/flyers/{flyer_id}/flyer_items"
        params = {"locale": "en", "sid": self.sid}
        logger.info("Fetching items for flyer %d", flyer_id)
        result = self._get(url, params)
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return result.get("items", [])
        return []

    def search_items(self, query: str) -> dict | None:
        """Search for items via the Flipp search endpoint."""
        params = {"query": query, "postal_code": self.postal_code}
        logger.info("Searching for '%s' near %s", query, self.postal_code)
        return self._get(SEARCH_BASE, params)
