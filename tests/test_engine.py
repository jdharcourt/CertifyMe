import shutil
from pathlib import Path

import pytest

from certifyme.kicad import scan_file, write_datasheets
from certifyme.linker import link_project
from certifyme.providers.base import DummyProvider
from certifyme.sexpr import Editor, parse, quote

FIXTURES = Path(__file__).parent / "fixtures"


# -- sexpr ------------------------------------------------------------------

def test_parse_roundtrip_spans():
    text = '(symbol "A" (property "Value" "x"))'
    root = parse(text)
    assert root.head == "symbol"
    assert root.name_atom().value == "A"
    # the value atom's recorded span maps back to the source
    prop = list(root.lists())[0]
    val = prop.children[2]
    assert text[val.start:val.end] == '"x"'


def test_editor_applies_edits_right_to_left():
    text = "0123456789"
    ed = Editor(text)
    ed.replace(0, 1, "A")
    ed.replace(5, 6, "BBB")
    assert ed.render() == "A1234BBB6789"


def test_quote_escapes():
    assert quote('a"b\\c') == '"a\\"b\\\\c"'


# -- scanning ---------------------------------------------------------------

def test_scan_symbol_lib_finds_parts_not_units():
    parts = scan_file(FIXTURES / "sym.kicad_sym")
    names = {p.name for p in parts}
    assert names == {"STM32F103C8T6", "R"}  # units excluded
    by_name = {p.name: p for p in parts}
    assert by_name["STM32F103C8T6"].search_key() == "STM32F103C8T6"  # MPN field
    assert by_name["R"].current_datasheet == "https://example.com/existing.pdf"


def test_scan_footprint_without_datasheet():
    parts = scan_file(FIXTURES / "R_0805.kicad_mod")
    assert len(parts) == 1
    fp = parts[0]
    assert fp.kind == "footprint"
    assert fp.current_datasheet is None


# -- writing ----------------------------------------------------------------

def test_write_updates_existing_datasheet(tmp_path):
    src = tmp_path / "sym.kicad_sym"
    shutil.copy(FIXTURES / "sym.kicad_sym", src)
    parts = scan_file(src)
    mcu = next(p for p in parts if p.name == "STM32F103C8T6")
    assert write_datasheets(src, [(mcu, "https://datasheets.example/stm32.pdf")])
    text = src.read_text(encoding="utf-8")
    assert '"https://datasheets.example/stm32.pdf"' in text
    # untouched part keeps its original datasheet, file still parses
    assert "https://example.com/existing.pdf" in text
    assert {p.name for p in scan_file(src)} == {"STM32F103C8T6", "R"}


def test_write_preserves_crlf_line_endings(tmp_path):
    src = tmp_path / "crlf.kicad_mod"
    src.write_bytes(
        b'(footprint "T1"\r\n'
        b'  (property "Value" "T1" (at 0 0 0)(layer "F.Fab")(effects (font (size 1 1))))\r\n'
        b'  (pad "1" smd rect (at 0 0)(size 1 1)(layers "F.Cu")))\r\n'
    )
    fp = scan_file(src)[0]
    write_datasheets(src, [(fp, "https://ds/t1.pdf")])
    data = src.read_bytes()
    assert data.count(b"\r\n") > 0
    assert data.count(b"\n") == data.count(b"\r\n")  # no lone LFs introduced
    assert b'"https://ds/t1.pdf"' in data


def test_write_clones_datasheet_into_footprint(tmp_path):
    src = tmp_path / "R_0805.kicad_mod"
    shutil.copy(FIXTURES / "R_0805.kicad_mod", src)
    fp = scan_file(src)[0]
    assert write_datasheets(src, [(fp, "https://datasheets.example/r0805.pdf")])
    reparsed = scan_file(src)[0]
    assert reparsed.current_datasheet == "https://datasheets.example/r0805.pdf"


# -- end to end -------------------------------------------------------------

def test_link_project_dry_run_writes_nothing(tmp_path):
    shutil.copytree(FIXTURES, tmp_path / "proj")
    provider = DummyProvider({"STM32F103C8T6": "https://ds/stm32.pdf",
                              "R_0805_2012Metric": "https://ds/r0805.pdf"})
    before = (tmp_path / "proj" / "sym.kicad_sym").read_text(encoding="utf-8")
    report = link_project(tmp_path / "proj", provider, dry_run=True)
    after = (tmp_path / "proj" / "sym.kicad_sym").read_text(encoding="utf-8")
    assert before == after
    assert report.count("linked") >= 1


def test_link_project_links_and_skips_existing(tmp_path):
    shutil.copytree(FIXTURES, tmp_path / "proj")
    provider = DummyProvider({
        "STM32F103C8T6": "https://ds/stm32.pdf",
        "R_0805_2012Metric": "https://ds/r0805.pdf",
    })
    report = link_project(tmp_path / "proj", provider)
    statuses = {(r.part.name, r.status) for r in report.results}
    assert ("STM32F103C8T6", "linked") in statuses
    assert ("R", "already") in statuses          # had a datasheet, left alone
    assert ("R_0805_2012Metric", "linked") in statuses
    assert report.files_changed


def test_link_project_generic_fallback(tmp_path):
    shutil.copytree(FIXTURES, tmp_path / "proj")
    # The provider only knows *generic* same-type parts, not the exact values.
    provider = DummyProvider({
        "10k resistor": "https://ds/generic-r.pdf",
        "100nF capacitor 0603": "https://ds/generic-c0603.pdf",
    })
    report = link_project(
        tmp_path / "proj", provider, overwrite=True, guess_datasheets=True
    )
    statuses = {(r.part.name, r.status) for r in report.results}
    assert any(s == "linked-generic" for _, s in statuses)
    # An IC with no exact match is left unfound, never guessed.
    assert ("STM32F103C8T6", "not-found") in statuses
    # The generic URL was actually written into the schematic.
    sch = (tmp_path / "proj" / "board.kicad_sch").read_text(encoding="utf-8")
    assert "generic-c0603.pdf" in sch


def test_link_project_no_generic_when_disabled(tmp_path):
    shutil.copytree(FIXTURES, tmp_path / "proj")
    provider = DummyProvider({"100nF capacitor 0603": "https://ds/generic-c0603.pdf"})
    report = link_project(tmp_path / "proj", provider, overwrite=True)  # guessing off
    assert all(r.status != "linked-generic" for r in report.results)
