"""Provider interface, the rich product record, and an offline dummy."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ProductInfo:
    """What a provider knows about a part. Every field beyond ``query`` is
    optional so providers can fill in only what they have."""

    query: str
    datasheet_url: str | None = None
    product_url: str | None = None        # where to buy / view the part
    unit_price: float | None = None
    currency: str | None = None
    mpn: str | None = None                # manufacturer part number
    manufacturer: str | None = None
    description: str | None = None
    stock: int | None = None
    supplier: str | None = None
    supplier_part_number: str | None = None
    package: str | None = None            # package / case, e.g. "0805 (2012 Metric)"
    parameters: dict | None = None        # parametric specs, e.g. {"Resistance": "10 kOhms"}
    approximate: bool = False             # a generic/representative match, not the exact part


class DatasheetProvider(ABC):
    """Resolves a part search key (usually an MPN) to product information."""

    name = "base"

    @abstractmethod
    def find_product(self, query: str) -> ProductInfo | None:
        """Return a :class:`ProductInfo` for *query*, or None if not found."""
        raise NotImplementedError

    def find_datasheet(self, query: str) -> str | None:
        """Convenience: just the datasheet URL (used by the linker)."""
        info = self.find_product(query)
        return info.datasheet_url if info else None


class CachingProvider(DatasheetProvider):
    """Mixin that memoises lookups so repeated parts cost one API call."""

    def __init__(self) -> None:
        self._cache: dict[str, ProductInfo | None] = {}

    def find_product(self, query: str) -> ProductInfo | None:
        key = query.strip()
        if not key:
            return None
        if key in self._cache:
            return self._cache[key]
        result = self._lookup_product(key)
        self._cache[key] = result
        return result

    @abstractmethod
    def _lookup_product(self, query: str) -> ProductInfo | None:
        raise NotImplementedError


class DummyProvider(DatasheetProvider):
    """Returns products from a static mapping. Useful for tests / dry runs.

    Mapping values may be a datasheet URL string, a dict of ``ProductInfo``
    fields, or a ``ProductInfo`` instance.
    """

    name = "dummy"

    def __init__(self, mapping: dict):
        self._mapping = mapping

    def find_product(self, query: str) -> ProductInfo | None:
        value = self._mapping.get(query.strip())
        if value is None:
            return None
        if isinstance(value, ProductInfo):
            return value
        if isinstance(value, str):
            return ProductInfo(query=query, datasheet_url=value)
        if isinstance(value, dict):
            return ProductInfo(query=query, **value)
        raise TypeError(f"unsupported dummy mapping value: {value!r}")
