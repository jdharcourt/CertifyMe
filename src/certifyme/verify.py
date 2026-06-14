"""Verify BOM parts against DigiKey's data.

Linking a datasheet is only useful if it's the *right* datasheet. This module
cross-checks what the schematic / PCB say about a part against what the provider
(DigiKey) reports for the looked-up product, so a wrong MPN or a value that
doesn't match the linked part gets flagged.

Four independent checks per BOM line:

* **mpn**       — the part's MPN matches the provider's manufacturer P/N,
* **value**     — the component value (e.g. ``10k``, ``100nF``) matches the
                  provider's parametric spec (Resistance / Capacitance / …),
* **package**   — the footprint's package (e.g. ``0805``) matches the provider's
                  Package / Case,
* **datasheet** — the provider actually returned a datasheet URL.

Everything here is pure Python (no KiCad, no network) so it unit-tests cleanly;
the provider lookup happens upstream in :mod:`certifyme.bom`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Check statuses.
OK = "ok"               # confirmed to match
MISMATCH = "mismatch"   # the board contradicts DigiKey -> something is wrong
UNKNOWN = "unknown"     # not enough info on one side to decide
MISSING = "missing"     # expected datum (e.g. a datasheet) absent

# Line verdicts.
V_OK = "ok"
V_WARN = "warn"
V_FAIL = "fail"
V_NO_MATCH = "no-match"   # the part wasn't found at the provider at all


@dataclass
class Check:
    name: str        # "mpn" | "value" | "package" | "datasheet"
    status: str
    detail: str = ""


@dataclass
class LineVerdict:
    references: list[str]
    value: str
    mpn: str
    footprint: str
    status: str
    checks: list[Check] = field(default_factory=list)

    @property
    def refs_text(self) -> str:
        return ", ".join(self.references)

    @property
    def problems(self) -> list[str]:
        """Human-readable lines for every check that didn't pass cleanly."""
        out = []
        for c in self.checks:
            if c.status in (MISMATCH, MISSING):
                out.append(f"{c.name}: {c.detail}")
        return out


# --------------------------------------------------------------------------
# Value parsing (engineering notation, both "4.7k" and "4k7" forms).
# --------------------------------------------------------------------------

_SI = {
    "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6,
    "m": 1e-3, "R": 1.0, "r": 1.0, "k": 1e3, "K": 1e3,
    "M": 1e6, "G": 1e9, "g": 1e9,
}

# A value we can compare numerically: must carry an SI prefix, an ohm/farad/henry
# unit, or the R-as-decimal-point convention. Bare integers stay ambiguous and
# fall back to a textual check (so IC values like "LM358" aren't misread).
_CAP_RE = re.compile(r"(?i)^\s*\d*\.?\d+\s*[pnuµμm]?\s*f\s*$|^\s*\d+[pnuµμm]\d+\s*f?\s*$")
_IND_RE = re.compile(r"(?i)^\s*\d*\.?\d+\s*[pnuµμm]?\s*h\s*$|^\s*\d+[pnuµμm]\d+\s*h?\s*$")
_RES_RE = re.compile(r"(?i)^\s*\d*\.?\d+\s*[kmgr]\s*\d*\s*$|^\s*\d*\.?\d+\s*(?:ohms?|Ω)\s*$")

_EMBED_RE = re.compile(r"^\s*(\d+)([pnuµμmkKMGRr])(\d+)\s*$")
_TRAIL_RE = re.compile(r"(?i)^\s*([0-9]*\.?[0-9]+)\s*([pnuµμmkMGRr])?\s*(?:ohms?|farads?|henr(?:y|ies)|[fhΩ])?\s*$")


def value_kind(value: str) -> str | None:
    """Classify a component value as 'resistance' / 'capacitance' /
    'inductance', or None when it isn't a comparable passive value."""
    if not value:
        return None
    v = value.strip()
    if _CAP_RE.match(v):
        return "capacitance"
    if _IND_RE.match(v):
        return "inductance"
    if _RES_RE.match(v):
        return "resistance"
    return None


def to_magnitude(text: str) -> float | None:
    """Parse an engineering-notation magnitude to a plain float.

    Handles ``10k``, ``4.7k``, ``4k7``, ``0R``, ``100nF``, ``0.1 µF``,
    ``10 kOhms``, ``4.7 µH`` and similar. Returns None if nothing parseable.
    """
    if not text:
        return None
    s = str(text).strip()
    # Drop a spelled-out unit so "10 kOhms" / "0.1 microfarad" reduce to magnitude.
    s = re.sub(r"(?i)\s*(ohms?|farads?|henr(?:y|ies))\s*$", "", s).strip()

    m = _EMBED_RE.match(s)            # 4k7 / 4R7 / 1n5
    if m:
        scale = _SI.get(m.group(2), 1.0)
        return float(f"{m.group(1)}.{m.group(3)}") * scale

    m = _TRAIL_RE.match(s)            # 10k / 4.7uF / 0.1 µF / 100
    if m:
        scale = _SI.get(m.group(2), 1.0) if m.group(2) else 1.0
        return float(m.group(1)) * scale

    # Last resort: first number plus an adjacent prefix anywhere in the text.
    m = re.search(r"([0-9]*\.?[0-9]+)\s*([pnuµμmkKMGR])?", s)
    if m:
        return float(m.group(1)) * (_SI.get(m.group(2), 1.0) if m.group(2) else 1.0)
    return None


def _close(a: float, b: float, rel: float = 0.02) -> bool:
    if a == b:
        return True
    hi = max(abs(a), abs(b))
    return hi > 0 and abs(a - b) <= rel * hi


def _norm(text: str) -> str:
    """Uppercase alphanumerics only, for forgiving identity comparisons."""
    return re.sub(r"[^0-9A-Z]", "", (text or "").upper())


# Footprint / package size codes shared between KiCad footprints and DigiKey.
_SIZE_RE = re.compile(r"(?<!\d)(0075|0100|0201|0402|0603|0805|1008|1206|1210|1806|1812|2010|2512|2920)(?!\d)")
# Named through-hole / SMD package families.
_FAMILY_RE = re.compile(
    r"(?i)\b(SOT-?23|SOT-?223|SOT-?89|SOIC|TSSOP|MSOP|SSOP|TSOP|QFN|DFN|TQFP|LQFP|QFP|BGA|TO-?220|TO-?92|TO-?252|TO-?263|DIP|SOD-?123|SOD-?323|SOD-?523|SMA|SMB|SMC|MELF)\b"
)


def _package_tokens(text: str) -> set[str]:
    if not text:
        return set()
    tokens = set(_SIZE_RE.findall(text))
    for fam in _FAMILY_RE.findall(text):
        tokens.add(re.sub(r"[\s-]", "", fam).upper())
    return tokens


# --------------------------------------------------------------------------
# The individual checks.
# --------------------------------------------------------------------------

def check_mpn(board_mpn: str, product) -> Check:
    prod_mpn = getattr(product, "mpn", None) if product else None
    if not board_mpn:
        return Check("mpn", UNKNOWN, "no MPN on the part to verify against")
    if not prod_mpn:
        return Check("mpn", UNKNOWN, f"provider returned no MPN for '{board_mpn}'")
    a, b = _norm(board_mpn), _norm(prod_mpn)
    if a == b:
        return Check("mpn", OK, f"{board_mpn} = {prod_mpn}")
    if a and b and (a in b or b in a):
        return Check("mpn", OK, f"{board_mpn} ~ {prod_mpn} (one contains the other)")
    return Check("mpn", MISMATCH, f"board '{board_mpn}' != DigiKey '{prod_mpn}'")


_KIND_PARAM = {
    "resistance": ("Resistance",),
    "capacitance": ("Capacitance",),
    "inductance": ("Inductance",),
}


def check_value(value: str, product) -> Check:
    if not product:
        return Check("value", UNKNOWN, "no product to compare against")
    kind = value_kind(value)
    params = getattr(product, "parameters", None) or {}
    if kind:
        spec_text = None
        for key in _KIND_PARAM[kind]:
            if key in params:
                spec_text = params[key]
                break
        board = to_magnitude(value)
        spec = to_magnitude(spec_text) if spec_text else None
        if board is not None and spec is not None:
            if _close(board, spec):
                return Check("value", OK, f"{kind} {value} = {spec_text}")
            return Check("value", MISMATCH,
                         f"{kind}: board '{value}' != DigiKey '{spec_text}'")
        return Check("value", UNKNOWN,
                     f"could not read {kind} from DigiKey to compare '{value}'")
    # Non-passive value (e.g. an IC part value): confirm it shows up textually.
    needle = _norm(value)
    hay = _norm(getattr(product, "mpn", "")) + " " + _norm(getattr(product, "description", ""))
    if needle and needle in hay:
        return Check("value", OK, f"'{value}' matches the DigiKey part")
    return Check("value", UNKNOWN, f"can't parametrically verify value '{value}'")


def check_package(footprint: str, product) -> Check:
    if not product:
        return Check("package", UNKNOWN, "no product to compare against")
    prod_pkg = getattr(product, "package", None) or ""
    params = getattr(product, "parameters", None) or {}
    prod_text = " ".join(
        [prod_pkg, params.get("Package / Case", ""), params.get("Supplier Device Package", "")]
    )
    board_tokens = _package_tokens(footprint)
    prod_tokens = _package_tokens(prod_text)
    if not board_tokens or not prod_tokens:
        return Check("package", UNKNOWN, "package not determinable on one side")
    if board_tokens & prod_tokens:
        return Check("package", OK, f"package {sorted(board_tokens & prod_tokens)[0]} matches")
    return Check("package", MISMATCH,
                 f"footprint {sorted(board_tokens)} != DigiKey {sorted(prod_tokens)}")


def check_datasheet(existing: str, product) -> Check:
    cur = (existing or "").strip()
    if cur and cur not in ("~",):
        return Check("datasheet", OK, "datasheet already on the part")
    if product and getattr(product, "datasheet_url", None):
        return Check("datasheet", OK, getattr(product, "datasheet_url"))
    return Check("datasheet", MISSING, "no datasheet found")


# --------------------------------------------------------------------------
# Per-line and whole-BOM verdicts.
# --------------------------------------------------------------------------

def verify_line(line) -> LineVerdict:
    """Verify a single :class:`certifyme.bom.BomLine` against its product."""
    product = getattr(line, "product", None)
    checks = [
        check_mpn(line.mpn, product),
        check_value(line.value, product),
        check_package(line.footprint, product),
        check_datasheet("", product),
    ]
    if product is None:
        status = V_NO_MATCH
    elif any(c.status == MISMATCH for c in checks):
        status = V_FAIL
    else:
        confirmed = any(
            c.status == OK for c in checks if c.name in ("mpn", "value", "package")
        )
        ds_missing = any(c.name == "datasheet" and c.status == MISSING for c in checks)
        status = V_OK if (confirmed and not ds_missing) else V_WARN
    return LineVerdict(
        references=list(line.references),
        value=line.value,
        mpn=line.mpn,
        footprint=line.footprint,
        status=status,
        checks=checks,
    )


def verify_bom(bom, *, include_dnp: bool = False) -> list[LineVerdict]:
    return [
        verify_line(line)
        for line in bom.lines
        if include_dnp or not line.dnp
    ]


def counts(verdicts: list[LineVerdict]) -> dict:
    out = {V_OK: 0, V_WARN: 0, V_FAIL: 0, V_NO_MATCH: 0}
    for v in verdicts:
        out[v.status] = out.get(v.status, 0) + 1
    return out


def summarize(verdicts: list[LineVerdict]) -> str:
    c = counts(verdicts)
    lines = [
        f"Verified lines : {len(verdicts)}",
        f"  OK           : {c[V_OK]}",
        f"  Warnings     : {c[V_WARN]}  (couldn't fully confirm)",
        f"  Mismatches   : {c[V_FAIL]}  (board contradicts DigiKey)",
        f"  Not found    : {c[V_NO_MATCH]}",
    ]
    return "\n".join(lines)
