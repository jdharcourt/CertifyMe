"""Exercise the plugin's pcbnew highlighting glue against a fake ``pcbnew``.

The real drawing needs KiCad, but the logic (which parts get flagged, how many
outlines are drawn, grouping, and clearing) can be verified with lightweight
fakes that mimic the small slice of the pcbnew API the plugin touches.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

from certifyme.providers.base import DummyProvider, ProductInfo

PLUGIN = Path(__file__).resolve().parents[1] / "kicad_plugin" / "action_certifyme.py"


# -- fake pcbnew / wx -------------------------------------------------------

class _BBox:
    def __init__(self, x, y, w, h):
        self._x, self._y, self._w, self._h = x, y, w, h
    def GetX(self): return self._x
    def GetY(self): return self._y
    def GetWidth(self): return self._w
    def GetHeight(self): return self._h


class _Field:
    def __init__(self, name, text):
        self._n, self._t = name, text
    def GetName(self): return self._n
    def GetText(self): return self._t
    def SetText(self, t): self._t = t


class _Footprint:
    def __init__(self, ref, value, fields=None):
        self._ref, self._value = ref, value
        self._fields = [_Field(n, v) for n, v in (fields or {}).items()]
    def GetReference(self): return self._ref
    def GetValue(self): return self._value
    def GetFields(self): return self._fields
    def GetBoundingBox(self, *_a): return _BBox(0, 0, 1_000_000, 1_000_000)


class _Shape:
    def __init__(self, board): self.layer = None
    def SetShape(self, t): self.shape_t = t
    def SetStart(self, p): self.start = p
    def SetEnd(self, p): self.end = p
    def SetLayer(self, l): self.layer = l
    def SetWidth(self, w): self.width = w
    def SetFilled(self, f): self.filled = f


class _Group:
    def __init__(self, board): self._name = ""; self._items = []
    def SetName(self, n): self._name = n
    def GetName(self): return self._name
    def AddItem(self, it): self._items.append(it)
    def GetItems(self): return list(self._items)


class _Board:
    def __init__(self, fps): self._fps = fps; self.shapes = []; self.groups = []
    def GetFootprints(self): return self._fps
    def Groups(self): return self.groups
    def Add(self, item):
        (self.groups if isinstance(item, _Group) else self.shapes).append(item)
    def Remove(self, item):
        for bucket in (self.shapes, self.groups):
            if item in bucket:
                bucket.remove(item)


@pytest.fixture
def plugin(monkeypatch):
    fake_pcb = types.ModuleType("pcbnew")
    fake_pcb.ActionPlugin = type("ActionPlugin", (), {"register": lambda self: None})
    fake_pcb.FromMM = lambda mm: int(mm * 1_000_000)
    fake_pcb.VECTOR2I = lambda x, y: (x, y)
    fake_pcb.PCB_SHAPE = _Shape
    fake_pcb.PCB_GROUP = _Group
    fake_pcb.SHAPE_T_RECTANGLE = object()
    fake_pcb.Eco1_User = 60
    fake_pcb.Eco2_User = 61
    fake_pcb.Refresh = lambda: None
    fake_pcb.GetBoard = lambda: None
    monkeypatch.setitem(sys.modules, "pcbnew", fake_pcb)

    fake_wx = types.ModuleType("wx")
    fake_wx.Dialog = type("Dialog", (), {})
    monkeypatch.setitem(sys.modules, "wx", fake_wx)

    spec = importlib.util.spec_from_file_location("action_certifyme_test", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _board():
    return _Board([
        _Footprint("U1", "LM358", {"MPN": "LM358DR", "Datasheet": ""}),
        _Footprint("R1", "10k", {"Datasheet": ""}),       # price only -> datasheet missing
        _Footprint("C1", "100nF", {"Datasheet": ""}),     # datasheet only -> price missing
        _Footprint("J1", "CONN", {"Datasheet": ""}),      # nothing -> both missing
        _Footprint("#PWR01", "GND", {}),                  # excluded
    ])


def _provider():
    return DummyProvider({
        "LM358DR": ProductInfo("LM358DR", datasheet_url="https://d/lm358", unit_price=0.5),
        "10k": ProductInfo("10k", unit_price=0.01),                  # no datasheet
        "100nF": ProductInfo("100nF", datasheet_url="https://d/c"),  # no price
        # "CONN" -> not in map -> None (both missing)
    })


def test_scan_missing_flags_expected_parts(plugin):
    flagged = plugin.scan_missing(_board(), _provider())
    by_ref = {e["ref"]: e["flags"] for e in flagged}
    assert "U1" not in by_ref          # fully resolved
    assert "#PWR01" not in by_ref      # excluded
    assert by_ref["R1"] == {plugin.highlight.FLAG_DATASHEET}
    assert by_ref["C1"] == {plugin.highlight.FLAG_PRICE}
    assert by_ref["J1"] == {plugin.highlight.FLAG_DATASHEET, plugin.highlight.FLAG_PRICE}


def test_draw_and_clear_highlights(plugin):
    board = _board()
    flagged = plugin.scan_missing(board, _provider())
    drawn = plugin.draw_highlights(board, flagged)
    # R1(1) + C1(1) + J1(2) = 4 outlines, in 2 category groups.
    assert drawn == 4
    assert len(board.shapes) == 4
    names = sorted(g.GetName() for g in board.groups)
    assert names == ["CertifyMe-Highlight:datasheet", "CertifyMe-Highlight:price"]
    # white (Eco1) used for datasheet, cyan (Eco2) for price
    layers = {s.layer for s in board.shapes}
    assert layers == {60, 61}

    removed = plugin.clear_highlights(board)
    assert removed == 4
    assert board.shapes == [] and board.groups == []
