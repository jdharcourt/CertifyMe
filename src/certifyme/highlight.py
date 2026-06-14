"""Pure logic for the PCB "missing info" highlighter.

The KiCad-specific drawing lives in the plugin (it needs ``pcbnew``); everything
here is plain Python so it can be unit-tested without KiCad:

* decide which categories a part is missing (datasheet / price),
* derive the search key used to look the part up,
* compute the inflated bounding box for the outline,
* describe how each category should look (layer + colour).
"""

from __future__ import annotations

from dataclasses import dataclass

from .kicad import _GENERIC_VALUES, MPN_FIELDS

FLAG_DATASHEET = "datasheet"
FLAG_PRICE = "price"
FLAG_SPEC = "spec"          # the part's specs contradict DigiKey (from verify)


def search_key(props: dict, value: str | None = None) -> str | None:
    """Best lookup key for a part: an MPN-style field, else a meaningful Value."""
    for field in MPN_FIELDS:
        val = (props.get(field) or "").strip()
        if val:
            return val
    val = (value if value is not None else props.get("Value", "")).strip()
    if val and val.lower() not in _GENERIC_VALUES:
        return val
    return None


def _has_datasheet(existing: str | None, product) -> bool:
    cur = (existing or "").strip()
    if cur and cur not in ("~", ""):
        return True
    return bool(product and getattr(product, "datasheet_url", None))


def classify(*, existing_datasheet: str | None, product) -> set[str]:
    """Return the set of missing-info flags for a part.

    *product* is a :class:`~certifyme.providers.base.ProductInfo` or None.
    A datasheet counts as found if the part already has one *or* the provider
    returned one. A price counts as found only if the provider returned one.
    """
    flags: set[str] = set()
    if not _has_datasheet(existing_datasheet, product):
        flags.add(FLAG_DATASHEET)
    if not (product and getattr(product, "unit_price", None) is not None):
        flags.add(FLAG_PRICE)
    return flags


@dataclass(frozen=True)
class CategoryStyle:
    flag: str
    layer: str        # pcbnew layer attribute name, e.g. "Eco1_User"
    theme_key: str    # colour-theme JSON key, e.g. "eco1_user"
    rgba: str         # e.g. "rgba(255, 255, 255, 0.30)"
    label: str


# Datasheet-missing -> translucent white   on Eco1.User.
# Price-missing      -> translucent cyan    on Eco2.User.
DEFAULT_STYLES: dict[str, CategoryStyle] = {
    FLAG_DATASHEET: CategoryStyle(
        FLAG_DATASHEET, "Eco1_User", "eco1_user", "rgba(255, 255, 255, 0.30)", "datasheet missing"
    ),
    FLAG_PRICE: CategoryStyle(
        FLAG_PRICE, "Eco2_User", "eco2_user", "rgba(0, 255, 255, 0.30)", "price missing"
    ),
}

# Spec-mismatch -> translucent magenta on Dwgs.User. Kept out of DEFAULT_STYLES
# so the missing-info highlighter doesn't touch a third layer; the verify flow
# passes this explicitly.
SPEC_STYLES: dict[str, CategoryStyle] = {
    FLAG_SPEC: CategoryStyle(
        FLAG_SPEC, "Dwgs_User", "dwgs_user", "rgba(255, 0, 255, 0.30)", "spec mismatch"
    ),
}


def inflate(x0: int, y0: int, x1: int, y1: int, margin: int) -> tuple[int, int, int, int]:
    """Grow a bounding box by *margin* on every side."""
    return (x0 - margin, y0 - margin, x1 + margin, y1 + margin)
