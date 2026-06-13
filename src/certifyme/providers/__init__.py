"""Datasheet lookup providers."""

from __future__ import annotations

from .base import DatasheetProvider, DummyProvider
from .digikey import DigiKeyProvider

__all__ = ["DatasheetProvider", "DummyProvider", "DigiKeyProvider", "build_provider"]


def build_provider(name: str, **kwargs) -> DatasheetProvider:
    name = name.lower()
    if name == "digikey":
        return DigiKeyProvider.from_env(**kwargs)
    if name == "dummy":
        return DummyProvider(kwargs.get("mapping") or {})
    raise ValueError(f"unknown provider: {name!r}")
