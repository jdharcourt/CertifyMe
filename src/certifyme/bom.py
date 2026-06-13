"""Build a priced Bill of Materials from a KiCad project.

Components are read from the schematic (the canonical source of what's actually
on the board). Each placed symbol instance is one component; multi-unit parts
that share a reference (e.g. an op-amp's A/B units) count once. Power and
no-connect pseudo-symbols (references starting with ``#``) are excluded.

Identical components are grouped (by Value + Footprint + MPN), counted, and
priced via the datasheet/price provider, then written to Excel and/or CSV.
"""

from __future__ import annotations

import csv
import datetime as _dt
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import xlsx
from .kicad import MPN_FIELDS, _GENERIC_VALUES
from .providers.base import DatasheetProvider, ProductInfo
from .sexpr import Atom, SExp, parse


@dataclass
class Component:
    reference: str
    value: str
    footprint: str
    mpn: str
    dnp: bool
    file: Path


@dataclass
class BomLine:
    references: list[str]
    value: str
    footprint: str
    mpn: str
    dnp: bool
    product: ProductInfo | None = None

    @property
    def quantity(self) -> int:
        return len(self.references)

    @property
    def refs_text(self) -> str:
        return ", ".join(self.references)

    @property
    def unit_price(self) -> float | None:
        return self.product.unit_price if self.product else None

    @property
    def ext_price(self) -> float | None:
        up = self.unit_price
        return None if up is None else round(up * self.quantity, 4)

    def search_key(self) -> str | None:
        if self.mpn:
            return self.mpn
        if self.value and self.value.lower() not in _GENERIC_VALUES:
            return self.value
        return None


@dataclass
class Bom:
    lines: list[BomLine] = field(default_factory=list)
    project_name: str = ""
    currency: str = "USD"
    source: str = "schematic"

    @property
    def total_quantity(self) -> int:
        return sum(l.quantity for l in self.lines if not l.dnp)

    @property
    def total_cost(self) -> float:
        return round(sum(l.ext_price for l in self.lines if not l.dnp and l.ext_price), 4)

    @property
    def priced_lines(self) -> int:
        return sum(1 for l in self.lines if l.unit_price is not None)


# -- component collection ---------------------------------------------------

def _prop_map(node: SExp) -> dict[str, str]:
    props: dict[str, str] = {}
    for child in node.lists():
        if child.head == "property" and len(child.children) >= 3:
            key, val = child.children[1], child.children[2]
            if isinstance(key, Atom) and isinstance(val, Atom):
                props[key.value] = val.value
    return props


def _has_child(node: SExp, head: str, value: str | None = None) -> bool:
    for child in node.lists():
        if child.head == head:
            if value is None:
                return True
            if len(child.children) >= 2 and isinstance(child.children[1], Atom):
                return child.children[1].value == value
    return False


def _mpn_of(props: dict[str, str]) -> str:
    for fld in MPN_FIELDS:
        if props.get(fld, "").strip():
            return props[fld].strip()
    return ""


def _footprint_name(props: dict[str, str]) -> str:
    fp = props.get("Footprint", "").strip()
    return fp.split(":")[-1] if ":" in fp else fp


def _components_from_schematic(file: Path) -> list[Component]:
    root = parse(_read(file))
    out: list[Component] = []
    seen_refs: set[str] = set()
    for node in root.walk():
        if node.head != "symbol":
            continue
        # Placed instances have a lib_id child; cached lib_symbols do not.
        if not _has_child(node, "lib_id"):
            continue
        props = _prop_map(node)
        ref = props.get("Reference", "").strip()
        if not ref or ref.startswith("#"):  # skip power/flag pseudo-symbols
            continue
        if ref in seen_refs:  # multi-unit part already counted
            continue
        seen_refs.add(ref)
        dnp = _has_child(node, "dnp", "yes") or props.get("dnp", "").lower() == "yes"
        out.append(
            Component(
                reference=ref,
                value=props.get("Value", "").strip(),
                footprint=_footprint_name(props),
                mpn=_mpn_of(props),
                dnp=dnp,
                file=file,
            )
        )
    return out


def _components_from_board(file: Path) -> list[Component]:
    root = parse(_read(file))
    out: list[Component] = []
    for node in root.walk():
        if node.head != "footprint":
            continue
        props = _prop_map(node)
        ref = props.get("Reference", "").strip()
        if not ref or ref.startswith("#"):
            continue
        out.append(
            Component(
                reference=ref,
                value=props.get("Value", "").strip(),
                footprint=(node.name_atom().value.split(":")[-1] if node.name_atom() else ""),
                mpn=_mpn_of(props),
                dnp=_has_child(node, "attr", "dnp"),
                file=file,
            )
        )
    return out


def _read(file: Path) -> str:
    with file.open(encoding="utf-8", newline="") as fh:
        return fh.read()


def collect_components(project: Path) -> tuple[list[Component], str]:
    """Gather components, preferring the schematic; fall back to the board."""
    project = Path(project)
    if project.is_file():
        files = [project]
    else:
        files = sorted(project.rglob("*.kicad_sch"))
    if files:
        comps: list[Component] = []
        for f in files:
            comps.extend(_components_from_schematic(f))
        if comps:
            return comps, "schematic"
    # Fall back to the PCB if there is no schematic with components.
    boards = [project] if project.is_file() else sorted(project.rglob("*.kicad_pcb"))
    comps = []
    for b in boards:
        if b.suffix == ".kicad_pcb":
            comps.extend(_components_from_board(b))
    return comps, "board"


# -- grouping & pricing -----------------------------------------------------

_NAT = re.compile(r"(\d+)")


def _natural_key(ref: str):
    return [int(t) if t.isdigit() else t.lower() for t in _NAT.split(ref)]


def group_components(components: list[Component]) -> list[BomLine]:
    groups: dict[tuple, BomLine] = {}
    order: list[tuple] = []
    for comp in components:
        key = (comp.value, comp.footprint, comp.mpn, comp.dnp)
        line = groups.get(key)
        if line is None:
            line = BomLine([], comp.value, comp.footprint, comp.mpn, comp.dnp)
            groups[key] = line
            order.append(key)
        line.references.append(comp.reference)
    lines = [groups[k] for k in order]
    for line in lines:
        line.references.sort(key=_natural_key)
    # Sort lines by first reference for a stable, readable BOM.
    lines.sort(key=lambda l: _natural_key(l.references[0]) if l.references else [])
    return lines


def build_bom(
    project: Path,
    provider: DatasheetProvider,
    *,
    currency: str = "USD",
    on_event=None,
) -> Bom:
    components, source = collect_components(project)
    lines = group_components(components)
    project = Path(project)
    bom = Bom(
        lines=lines,
        project_name=(project.stem if project.is_file() else project.name),
        currency=currency,
        source=source,
    )
    for line in lines:
        key = line.search_key()
        if key:
            try:
                line.product = provider.find_product(key)
            except Exception:
                line.product = None
        if on_event:
            on_event(line)
    return bom


# -- output -----------------------------------------------------------------

COLUMNS = [
    ("#", 5),
    ("References", 22),
    ("Qty", 6),
    ("Value", 16),
    ("Footprint", 22),
    ("MPN", 20),
    ("Manufacturer", 20),
    ("Description", 36),
    ("Unit Price", 12),
    ("Ext. Price", 12),
    ("Stock", 9),
    ("Supplier P/N", 18),
    ("Datasheet", 14),
    ("Buy Link", 14),
    ("DNP", 6),
]


def write_xlsx_bom(bom: Bom, path) -> None:
    rows: list[list[xlsx.Cell]] = []

    # Title / metadata block.
    today = _dt.date.today().isoformat()
    rows.append([xlsx.text(f"Bill of Materials - {bom.project_name}", xlsx.STYLE_BOLD)])
    rows.append([
        xlsx.text(f"Generated {today} by CertifyMe"),
        xlsx.blank(), xlsx.blank(),
        xlsx.text(f"Source: {bom.source}"),
        xlsx.blank(),
        xlsx.text(f"Currency: {bom.currency}"),
    ])
    rows.append([])

    rows.append([xlsx.text(name, xlsx.STYLE_BOLD) for name, _ in COLUMNS])

    for i, line in enumerate(bom.lines, start=1):
        p = line.product
        rows.append([
            xlsx.number(i),
            xlsx.text(line.refs_text),
            xlsx.number(line.quantity),
            xlsx.text(line.value),
            xlsx.text(line.footprint),
            xlsx.text(line.mpn or (p.mpn if p else "")),
            xlsx.text(p.manufacturer if p else ""),
            xlsx.text(p.description if p else ""),
            xlsx.number(line.unit_price, xlsx.STYLE_MONEY4) if line.unit_price is not None else xlsx.text(""),
            xlsx.number(line.ext_price, xlsx.STYLE_MONEY2) if line.ext_price is not None else xlsx.text(""),
            xlsx.number(p.stock) if (p and p.stock is not None) else xlsx.text(""),
            xlsx.text(p.supplier_part_number if p else ""),
            xlsx.hyperlink(p.datasheet_url, "Datasheet") if (p and p.datasheet_url) else xlsx.text(""),
            xlsx.hyperlink(p.product_url, "Buy") if (p and p.product_url) else xlsx.text(""),
            xlsx.text("DNP" if line.dnp else ""),
        ])

    # Totals row.
    rows.append([])
    rows.append([
        xlsx.blank(),
        xlsx.text("TOTAL", xlsx.STYLE_BOLD),
        xlsx.number(bom.total_quantity, xlsx.STYLE_BOLD),
        xlsx.blank(), xlsx.blank(), xlsx.blank(), xlsx.blank(), xlsx.blank(),
        xlsx.blank(),
        xlsx.number(bom.total_cost, xlsx.STYLE_TOTAL),
    ])

    # A title block sits above the column header, so the simple top-row freeze
    # doesn't apply here; the autofilter on the header row still aids navigation.
    xlsx.write_xlsx(
        path,
        rows,
        sheet_name="BOM",
        col_widths=[w for _, w in COLUMNS],
        freeze_header=False,
    )


def write_csv_bom(bom: Bom, path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow([name for name, _ in COLUMNS])
        for i, line in enumerate(bom.lines, start=1):
            p = line.product
            w.writerow([
                i,
                line.refs_text,
                line.quantity,
                line.value,
                line.footprint,
                line.mpn or (p.mpn if p else ""),
                p.manufacturer if p else "",
                p.description if p else "",
                f"{line.unit_price:.4f}" if line.unit_price is not None else "",
                f"{line.ext_price:.2f}" if line.ext_price is not None else "",
                p.stock if (p and p.stock is not None) else "",
                p.supplier_part_number if p else "",
                p.datasheet_url if p else "",
                p.product_url if p else "",
                "DNP" if line.dnp else "",
            ])
        w.writerow([])
        w.writerow(["", "TOTAL", bom.total_quantity, "", "", "", "", "", "",
                    f"{bom.total_cost:.2f}"])


def summarize(bom: Bom) -> str:
    lines = [
        f"Source        : {bom.source}",
        f"BOM lines     : {len(bom.lines)}",
        f"Total parts   : {bom.total_quantity}",
        f"Priced lines  : {bom.priced_lines}/{len(bom.lines)}",
        f"Total cost    : {bom.total_cost:.2f} {bom.currency}",
    ]
    return "\n".join(lines)
