"""Provider interface and an offline dummy used by tests."""

from __future__ import annotations

from abc import ABC, abstractmethod


class DatasheetProvider(ABC):
    """Resolves a part search key (usually an MPN) to a datasheet URL."""

    name = "base"

    @abstractmethod
    def find_datasheet(self, query: str) -> str | None:
        """Return a datasheet URL for *query*, or None if nothing was found."""
        raise NotImplementedError


class CachingProvider(DatasheetProvider):
    """Mixin that memoises lookups so repeated parts cost one API call."""

    def __init__(self) -> None:
        self._cache: dict[str, str | None] = {}

    def find_datasheet(self, query: str) -> str | None:
        key = query.strip()
        if not key:
            return None
        if key in self._cache:
            return self._cache[key]
        result = self._lookup(key)
        self._cache[key] = result
        return result

    @abstractmethod
    def _lookup(self, query: str) -> str | None:
        raise NotImplementedError


class DummyProvider(DatasheetProvider):
    """Returns datasheets from a static mapping. Useful for tests / dry runs."""

    name = "dummy"

    def __init__(self, mapping: dict[str, str]):
        self._mapping = mapping

    def find_datasheet(self, query: str) -> str | None:
        return self._mapping.get(query.strip())
