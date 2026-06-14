"""Infer a part's class so *unfound* parts can still get a generic datasheet.

When DigiKey has no hit for a part's MPN or value, we can often still find a
*representative* part of the same class — e.g. ``10k resistor 0805`` — and borrow
its datasheet. That only makes sense for parts whose value + package essentially
define them (passives and simple discretes); an IC, connector, or module with no
MPN can't be guessed this way, so those are deliberately left unfound.

The package parsing is shared with :mod:`certifyme.verify` so the two features
recognise exactly the same footprint/package codes.
"""

from __future__ import annotations

import re

from .kicad import _GENERIC_VALUES
from .verify import _package_tokens

# Reference-designator prefix -> (search word, eligible-for-generic-guess).
# Eligible types are those a value + package genuinely identifies. ICs (U),
# connectors (J/P), modules, etc. are intentionally absent: a generic datasheet
# for "some op-amp in SOIC-8" would be misleading.
_REFDES_TYPE = {
    "R": "resistor",
    "RN": "resistor network",
    "RV": "varistor",
    "C": "capacitor",
    "L": "inductor",
    "FB": "ferrite bead",
    "D": "diode",
    "LED": "LED",
    "ZD": "zener diode",
    "Q": "transistor",
    "F": "fuse",
    "Y": "crystal",
    "X": "crystal",
    "XTAL": "crystal",
}

_PREFIX_RE = re.compile(r"^([A-Za-z]+)")


def refdes_prefix(ref: str) -> str:
    """Leading letters of a reference designator (``R12`` -> ``R``, ``LED3`` ->
    ``LED``)."""
    m = _PREFIX_RE.match((ref or "").strip())
    return m.group(1).upper() if m else ""


def part_type(ref: str) -> str | None:
    """The generic-search word for a reference designator, or None if the part
    isn't a type we can responsibly guess a datasheet for."""
    return _REFDES_TYPE.get(refdes_prefix(ref))


def package_label(footprint: str) -> str:
    """A single package token usable in a keyword search (e.g. ``0805``,
    ``SOT23``), or ``""`` when none is recognisable. Prefers a numeric size code."""
    tokens = _package_tokens(footprint)
    if not tokens:
        return ""
    sizes = sorted(t for t in tokens if t.isdigit())
    if sizes:
        return sizes[0]
    return sorted(tokens)[0]


def generic_query_from(ref: str, value: str, footprint: str) -> str | None:
    """Build a generic DigiKey keyword query (e.g. ``"10k resistor 0805"``) from a
    reference designator, value and footprint. Returns None when the part isn't a
    guessable type or has no concrete value to anchor on."""
    type_word = part_type(ref)
    if not type_word:
        return None
    value = (value or "").strip()
    if not value or value.lower() in _GENERIC_VALUES:
        return None  # without a real value the result would be an arbitrary part
    pkg = package_label(footprint or "")
    return " ".join(p for p in (value, type_word, pkg) if p)


def generic_query(line) -> str | None:
    """Generic query for an unfound BOM *line* (see :func:`generic_query_from`)."""
    refs = getattr(line, "references", None) or []
    if not refs:
        return None
    return generic_query_from(
        refs[0], getattr(line, "value", "") or "", getattr(line, "footprint", "") or ""
    )
