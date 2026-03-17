"""Data models for the NH Grocery Prices pipeline."""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class CategoryDef:
    """A category definition with compiled regex patterns."""

    id: str
    name: str
    keywords: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    _kw_patterns: list[re.Pattern] = field(default_factory=list, repr=False)
    _ex_patterns: list[re.Pattern] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        self._kw_patterns = [re.compile(p, re.IGNORECASE) for p in self.keywords]
        self._ex_patterns = [re.compile(p, re.IGNORECASE) for p in self.exclude]

    def matches(self, product_name: str) -> bool:
        """Return True if product_name matches this category."""
        if any(p.search(product_name) for p in self._ex_patterns):
            return False
        return any(p.search(product_name) for p in self._kw_patterns)


@dataclass
class FlyerProduct:
    """A normalized product from a flyer."""

    name: str
    store: str
    price: float
    brand: str | None = None
    category: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    flyer_id: int | None = None
    item_id: int | None = None
    price_type: str = "sale"
    size: float | None = None
    unit: str | None = None
    unit_price: float | None = None
