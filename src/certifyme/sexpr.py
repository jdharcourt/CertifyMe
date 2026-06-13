"""A minimal, span-preserving S-expression parser for KiCad files.

KiCad stores symbols (.kicad_sym), footprints (.kicad_mod) and schematics
(.kicad_sch) as S-expressions. We need to read those files, locate specific
nodes (the "Datasheet" property of each part), and rewrite *only* that value so
the resulting diff is as small as possible and the rest of the file is byte-for-
byte preserved.

To do that, every node remembers the byte/character offsets it occupies in the
original source text. Edits are expressed as (start, end, replacement) splices
against that original text, applied right-to-left so earlier offsets stay valid.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Atom:
    """A leaf token: a bare symbol/number, or a double-quoted string."""

    value: str          # the decoded value (quotes/escapes removed)
    start: int          # offset of the first char (the opening quote, if any)
    end: int            # offset one past the last char (the closing quote)
    quoted: bool        # whether the source token was a "quoted string"

    def is_symbol(self, name: str) -> bool:
        return not self.quoted and self.value == name


@dataclass
class SExp:
    """A parenthesised list of child nodes."""

    children: list = field(default_factory=list)
    start: int = 0      # offset of '('
    end: int = 0        # offset one past ')'

    @property
    def head(self) -> str | None:
        """The leading symbol of the list, e.g. ``symbol`` in ``(symbol ...)``."""
        if self.children and isinstance(self.children[0], Atom):
            return self.children[0].value
        return None

    def name_atom(self) -> Atom | None:
        """The quoted name following the head, e.g. ``"Device:R"`` in
        ``(symbol "Device:R" ...)``."""
        if len(self.children) >= 2 and isinstance(self.children[1], Atom):
            atom = self.children[1]
            if atom.quoted:
                return atom
        return None

    def lists(self):
        """Iterate over direct children that are lists."""
        return (c for c in self.children if isinstance(c, SExp))

    def walk(self):
        """Depth-first iteration over this node and all descendant lists."""
        yield self
        for c in self.children:
            if isinstance(c, SExp):
                yield from c.walk()


class SexprError(ValueError):
    pass


def parse(text: str) -> SExp:
    """Parse the first top-level S-expression in *text*."""
    pos = _skip_ws(text, 0)
    if pos >= len(text) or text[pos] != "(":
        raise SexprError("expected '(' at start of S-expression")
    node, _ = _parse_list(text, pos)
    return node


def _skip_ws(text: str, pos: int) -> int:
    n = len(text)
    while pos < n and text[pos] in " \t\r\n":
        pos += 1
    return pos


def _parse_list(text: str, pos: int) -> tuple[SExp, int]:
    assert text[pos] == "("
    node = SExp(start=pos)
    pos += 1
    n = len(text)
    while True:
        pos = _skip_ws(text, pos)
        if pos >= n:
            raise SexprError("unterminated list")
        ch = text[pos]
        if ch == ")":
            node.end = pos + 1
            return node, pos + 1
        if ch == "(":
            child, pos = _parse_list(text, pos)
            node.children.append(child)
        elif ch == '"':
            atom, pos = _parse_string(text, pos)
            node.children.append(atom)
        else:
            atom, pos = _parse_bare(text, pos)
            node.children.append(atom)


def _parse_string(text: str, pos: int) -> tuple[Atom, int]:
    start = pos
    pos += 1  # opening quote
    n = len(text)
    out = []
    while pos < n:
        ch = text[pos]
        if ch == "\\" and pos + 1 < n:
            nxt = text[pos + 1]
            out.append({"n": "\n", "t": "\t", "r": "\r"}.get(nxt, nxt))
            pos += 2
            continue
        if ch == '"':
            return Atom("".join(out), start, pos + 1, quoted=True), pos + 1
        out.append(ch)
        pos += 1
    raise SexprError("unterminated string")


def _parse_bare(text: str, pos: int) -> tuple[Atom, int]:
    start = pos
    n = len(text)
    while pos < n and text[pos] not in " \t\r\n()\"":
        pos += 1
    return Atom(text[start:pos], start, pos, quoted=False), pos


def quote(value: str) -> str:
    """Encode *value* as a KiCad double-quoted string token."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


class Editor:
    """Accumulates splice edits against an original source string."""

    def __init__(self, text: str):
        self.text = text
        self._edits: list[tuple[int, int, str]] = []

    def replace(self, start: int, end: int, replacement: str) -> None:
        self._edits.append((start, end, replacement))

    def insert(self, at: int, replacement: str) -> None:
        self._edits.append((at, at, replacement))

    @property
    def dirty(self) -> bool:
        return bool(self._edits)

    def render(self) -> str:
        # Apply right-to-left so earlier offsets remain valid.
        result = self.text
        for start, end, replacement in sorted(self._edits, key=lambda e: e[0], reverse=True):
            result = result[:start] + replacement + result[end:]
        return result
