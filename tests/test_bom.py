import zipfile
from pathlib import Path
from xml.dom import minidom

from certifyme import bom as bom_mod
from certifyme.providers.base import DummyProvider, ProductInfo

FIXTURES = Path(__file__).parent / "fixtures"
SCH = FIXTURES / "board.kicad_sch"

PRICES = {
    "10k": {"unit_price": 0.01, "manufacturer": "Yageo", "product_url": "https://dk/10k",
            "datasheet_url": "https://dk/10k.pdf", "stock": 1000, "supplier_part_number": "311-10K"},
    "100nF": {"unit_price": 0.02, "manufacturer": "Murata", "product_url": "https://dk/100nf",
              "stock": 500, "supplier_part_number": "490-100NF"},
    "LM358DR": {"unit_price": 0.50, "manufacturer": "TI", "product_url": "https://dk/lm358",
                "datasheet_url": "https://dk/lm358.pdf", "stock": 42, "supplier_part_number": "296-LM358"},
    "1k": {"unit_price": 0.01, "manufacturer": "Yageo"},
}


def _provider():
    return DummyProvider(PRICES)


# -- collection & grouping --------------------------------------------------

def test_collect_excludes_power_and_dedups_multiunit():
    comps, source = bom_mod.collect_components(SCH)
    assert source == "schematic"
    refs = sorted(c.reference for c in comps)
    assert refs == ["C1", "R1", "R2", "R3", "U1"]  # #PWR01 excluded, U1 once


def test_grouping_counts_quantities():
    comps, _ = bom_mod.collect_components(SCH)
    lines = bom_mod.group_components(comps)
    by_value = {l.value: l for l in lines}
    assert by_value["10k"].quantity == 2
    assert by_value["10k"].references == ["R1", "R2"]
    assert by_value["100nF"].quantity == 1
    assert by_value["LM358"].mpn == "LM358DR"
    assert by_value["1k"].dnp is True


# -- pricing & totals -------------------------------------------------------

def test_build_bom_prices_and_excludes_dnp_from_total():
    bom = bom_mod.build_bom(SCH, _provider())
    assert bom.source == "schematic"
    assert bom.total_quantity == 4          # 2 + 1 + 1, DNP R3 excluded
    assert abs(bom.total_cost - 0.54) < 1e-9  # 2*0.01 + 0.02 + 0.50
    line_10k = next(l for l in bom.lines if l.value == "10k")
    assert line_10k.unit_price == 0.01
    assert abs(line_10k.ext_price - 0.02) < 1e-9
    assert line_10k.product.manufacturer == "Yageo"


def test_search_key_prefers_mpn():
    comps, _ = bom_mod.collect_components(SCH)
    lines = bom_mod.group_components(comps)
    u1 = next(l for l in lines if l.value == "LM358")
    assert u1.search_key() == "LM358DR"


# -- xlsx output ------------------------------------------------------------

def test_write_xlsx_is_valid_and_has_content(tmp_path):
    bom = bom_mod.build_bom(SCH, _provider())
    out = tmp_path / "bom.xlsx"
    bom_mod.write_xlsx_bom(bom, out)

    assert out.exists() and out.stat().st_size > 0
    with zipfile.ZipFile(out) as z:
        names = set(z.namelist())
        assert {"[Content_Types].xml", "xl/workbook.xml",
                "xl/worksheets/sheet1.xml", "xl/styles.xml"} <= names
        # every stored part must be well-formed XML
        for name in names:
            if name.endswith(".xml") or name.endswith(".rels"):
                minidom.parseString(z.read(name))
        sheet = z.read("xl/worksheets/sheet1.xml").decode("utf-8")
    assert "Bill of Materials" in sheet
    assert "LM358DR" in sheet
    assert "HYPERLINK(" in sheet          # datasheet / buy links present
    assert "TOTAL" in sheet


# -- generic datasheet fallback for unfound parts ---------------------------

from certifyme import parttype


def test_generic_query_for_passives():
    line = bom_mod.BomLine(["R1", "R2"], "10k", "R_0805_2012Metric", "", False)
    assert parttype.generic_query(line) == "10k resistor 0805"
    cap = bom_mod.BomLine(["C1"], "100nF", "C_0603_1608Metric", "", False)
    assert parttype.generic_query(cap) == "100nF capacitor 0603"


def test_generic_query_skips_ics_and_blank_values():
    ic = bom_mod.BomLine(["U1"], "LM358", "SOIC-8", "", False)
    assert parttype.generic_query(ic) is None        # ICs aren't guessable
    blank = bom_mod.BomLine(["R9"], "~", "R_0805_2012Metric", "", False)
    assert parttype.generic_query(blank) is None     # no real value to anchor on


def test_build_bom_guesses_generic_datasheet():
    # Provider knows nothing about the exact values, but does have a
    # representative part for the generic queries.
    generic = {
        "10k resistor 0805": {"datasheet_url": "https://dk/generic-10k.pdf",
                              "manufacturer": "Yageo"},
        "100nF capacitor 0603": {"datasheet_url": "https://dk/generic-100nf.pdf"},
    }
    bom = bom_mod.build_bom(SCH, DummyProvider(generic), guess_datasheets=True)
    r10k = next(l for l in bom.lines if l.value == "10k")
    assert r10k.product is not None
    assert r10k.product.approximate is True
    assert r10k.product.datasheet_url == "https://dk/generic-10k.pdf"
    assert r10k.unit_price is None                    # generic match adds no price
    assert bom.total_cost == 0.0                      # totals unaffected by guesses
    assert bom.generic_lines >= 2
    # ICs are left unfound even with guessing on.
    u1 = next(l for l in bom.lines if l.value == "LM358")
    assert u1.product is None


def test_generic_fill_keeps_real_price():
    # Exact part is found (with a price) but has no datasheet; the generic
    # fallback should fill the datasheet without discarding the price.
    mapping = {
        "10k": {"unit_price": 0.01, "manufacturer": "Yageo"},   # priced, no datasheet
        "10k resistor 0805": {"datasheet_url": "https://dk/generic-10k.pdf"},
    }
    bom = bom_mod.build_bom(SCH, DummyProvider(mapping), guess_datasheets=True)
    r10k = next(l for l in bom.lines if l.value == "10k")
    assert r10k.unit_price == 0.01                       # price preserved
    assert r10k.product.datasheet_url == "https://dk/generic-10k.pdf"
    assert r10k.product.approximate is True
    assert bom.total_cost > 0                            # real price still counts


def test_guessing_off_by_default():
    generic = {"10k resistor 0805": {"datasheet_url": "https://dk/generic-10k.pdf"}}
    bom = bom_mod.build_bom(SCH, DummyProvider(generic))   # guess_datasheets=False
    r10k = next(l for l in bom.lines if l.value == "10k")
    assert r10k.product is None


def test_write_csv(tmp_path):
    bom = bom_mod.build_bom(SCH, _provider())
    out = tmp_path / "bom.csv"
    bom_mod.write_csv_bom(bom, out)
    content = out.read_text(encoding="utf-8")
    assert "References" in content
    assert "R1, R2" in content
    assert "LM358DR" in content
    assert "TOTAL" in content
