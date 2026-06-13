"""Discover parts inside KiCad files and update their Datasheet property.

A *part* here is any ``(symbol ...)`` or ``(footprint ...)`` list that carries
its own ``(property ...)`` children. That rule naturally:

* includes the top-level symbol of a ``.kicad_sym`` library,
* includes footprints in ``.kicad_mod`` / ``.pretty`` libraries,
* includes both the cached ``lib_symbols`` and the placed instances in a
  ``.kicad_sch`` schematic,
* and excludes symbol *units* (e.g. ``(symbol "R_1_1" ...)``) which hold only
  graphics and no properties.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .sexpr import Atom, Editor, SExp, parse, quote

# Field names that, if present, are a far better datasheet search key than the
# generic Value field. Checked in order.
MPN_FIELDS = (
    "MPN",
    "mpn",
    "Manufacturer Part Number",
    "Manufacturer_Part_Number",
    "Mfr Part #",
    "Mfr. Part #",
    "Part Number",
    "PartNumber",
    "Part",
)

# Values that are too generic to ever resolve to a real datasheet.
_GENERIC_VALUES = {"~", "", "unknown", "dnp", "dnf"}

KICAD_GLOBS = ("*.kicad_sym", "*.kicad_mod", "*.kicad_sch")


@dataclass
class Part:
    """A part found in a file, plus everything needed to update it in place."""

    file: Path
    scope: SExp                       # the (symbol ...) / (footprint ...) node
    properties: dict[str, str]        # property name -> value
    _prop_nodes: dict[str, SExp]      # property name -> its (property ...) node

    @property
    def kind(self) -> str:
        return self.scope.head or "?"

    @property
    def name(self) -> str:
        """Human-readable identity: the lib id, stripped of any library prefix."""
        name_atom = self.scope.name_atom()
        raw = name_atom.value if name_atom else (self.properties.get("Value") or "?")
        return raw.split(":")[-1]

    def search_key(self, prefer_field: str | None = None) -> str | None:
        """The best string to search a datasheet for: an explicit MPN field, an
        operator-chosen field, else the Value, else the part name."""
        if prefer_field and self.properties.get(prefer_field):
            return self.properties[prefer_field].strip()
        for fld in MPN_FIELDS:
            val = self.properties.get(fld)
            if val and val.strip():
                return val.strip()
        value = (self.properties.get("Value") or "").strip()
        if value and value.lower() not in _GENERIC_VALUES:
            return value
        name = self.name
        return name if name and name != "?" else None

    @property
    def current_datasheet(self) -> str | None:
        ds = self.properties.get("Datasheet")
        if ds is None:
            return None
        return ds if ds not in ("~", "") else None


def _property_value_atom(prop: SExp) -> Atom | None:
    """The 3rd element of ``(property "Name" "Value" ...)``."""
    if len(prop.children) >= 3 and isinstance(prop.children[2], Atom):
        return prop.children[2]
    return None


def _collect_parts(file: Path, root: SExp) -> list[Part]:
    parts: list[Part] = []
    for node in root.walk():
        if node.head not in ("symbol", "footprint"):
            continue
        prop_nodes: dict[str, SExp] = {}
        props: dict[str, str] = {}
        for child in node.lists():
            if child.head != "property":
                continue
            if len(child.children) < 3:
                continue
            key_atom, val_atom = child.children[1], child.children[2]
            if not (isinstance(key_atom, Atom) and isinstance(val_atom, Atom)):
                continue
            prop_nodes[key_atom.value] = child
            props[key_atom.value] = val_atom.value
        if prop_nodes:  # only real parts have properties; units don't
            parts.append(Part(file, node, props, prop_nodes))
    return parts


def _read(file: Path) -> str:
    # newline="" disables newline translation so original EOLs are preserved.
    with file.open(encoding="utf-8", newline="") as fh:
        return fh.read()


def _write(file: Path, text: str) -> None:
    with file.open("w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def scan_file(file: Path) -> list[Part]:
    root = parse(_read(file))
    return _collect_parts(file, root)


def discover_files(project: Path) -> list[Path]:
    """All KiCad part-bearing files under *project* (recursively)."""
    if project.is_file():
        return [project]
    found: list[Path] = []
    for pattern in KICAD_GLOBS:
        found.extend(project.rglob(pattern))
    return sorted(set(found))


def write_datasheets(file: Path, updates: list[tuple[Part, str]]) -> bool:
    """Apply ``(part -> url)`` updates to *file*. Returns True if it changed.

    If a part already has a Datasheet property, only its value token is
    rewritten. If it has none (common for footprints), a Datasheet property is
    cloned from the part's Value property so the geometry stays valid.
    """
    if not updates:
        return False
    text = _read(file)
    editor = Editor(text)
    eol = "\r\n" if "\r\n" in text else "\n"

    for part, url in updates:
        ds_node = part._prop_nodes.get("Datasheet")
        if ds_node is not None:
            val_atom = _property_value_atom(ds_node)
            if val_atom is not None:
                editor.replace(val_atom.start, val_atom.end, quote(url))
                continue
        _insert_cloned_property(editor, text, part, url, eol)

    if not editor.dirty:
        return False
    _write(file, editor.render())
    return True


def _insert_cloned_property(editor: Editor, text: str, part: Part, url: str, eol: str = "\n") -> None:
    """Create a Datasheet property by cloning the part's Value property node."""
    template = part._prop_nodes.get("Value")
    if template is None:
        # Nothing safe to clone from; skip rather than emit invalid geometry.
        return
    name_atom = template.children[1]
    val_atom = template.children[2]
    raw = text[template.start:template.end]
    base = template.start
    # Splice within the cloned substring, later span first to keep offsets valid.
    n0, n1 = name_atom.start - base, name_atom.end - base
    v0, v1 = val_atom.start - base, val_atom.end - base
    cloned = raw[:v0] + quote(url) + raw[v1:]
    cloned = cloned[:n0] + quote("Datasheet") + cloned[n1:]

    indent = _line_indent(text, template.start)
    editor.insert(template.end, f"{eol}{indent}{cloned}")


def _line_indent(text: str, pos: int) -> str:
    line_start = text.rfind("\n", 0, pos) + 1
    indent = []
    for ch in text[line_start:pos]:
        if ch in " \t":
            indent.append(ch)
        else:
            break
    return "".join(indent)
