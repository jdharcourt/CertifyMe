from certifyme import verify
from certifyme.bom import BomLine
from certifyme.providers.base import ProductInfo
from certifyme.verify import (
    MISMATCH, MISSING, OK, UNKNOWN,
    V_FAIL, V_NO_MATCH, V_OK, V_WARN,
)


# -- value parsing ----------------------------------------------------------

def test_to_magnitude_engineering_forms():
    assert verify.to_magnitude("10k") == 10_000
    assert verify.to_magnitude("4.7k") == 4_700
    assert abs(verify.to_magnitude("4k7") - 4_700) < 1e-6
    assert verify.to_magnitude("0R") == 0
    assert abs(verify.to_magnitude("100nF") - 100e-9) < 1e-18
    assert abs(verify.to_magnitude("0.1 µF") - 0.1e-6) < 1e-18
    assert abs(verify.to_magnitude("10 kOhms") - 10_000) < 1e-6
    assert abs(verify.to_magnitude("4.7 µH") - 4.7e-6) < 1e-12


def test_value_kind_classifies_passives_only():
    assert verify.value_kind("10k") == "resistance"
    assert verify.value_kind("100nF") == "capacitance"
    assert verify.value_kind("4.7uH") == "inductance"
    assert verify.value_kind("100Ω") == "resistance"
    assert verify.value_kind("LM358") is None      # IC, not a passive
    assert verify.value_kind("") is None


# -- individual checks ------------------------------------------------------

def test_check_mpn_match_partial_and_mismatch():
    assert verify.check_mpn("LM358DR", ProductInfo("q", mpn="LM358DR")).status == OK
    # base vs packaged suffix counts as a match
    assert verify.check_mpn("LM358", ProductInfo("q", mpn="LM358DR")).status == OK
    assert verify.check_mpn("LM358DR", ProductInfo("q", mpn="NE555P")).status == MISMATCH
    assert verify.check_mpn("", ProductInfo("q", mpn="X")).status == UNKNOWN


def test_check_value_parametric_match_and_mismatch():
    good = ProductInfo("q", parameters={"Resistance": "10 kOhms"})
    assert verify.check_value("10k", good).status == OK
    bad = ProductInfo("q", parameters={"Resistance": "1 kOhms"})
    assert verify.check_value("10k", bad).status == MISMATCH


def test_check_value_textual_for_ics():
    p = ProductInfo("q", mpn="LM358DR", description="Dual op-amp")
    assert verify.check_value("LM358", p).status == OK
    assert verify.check_value("NE555", p).status == UNKNOWN


def test_check_package_match_and_mismatch():
    p = ProductInfo("q", package="0805 (2012 Metric)")
    assert verify.check_package("R_0805_2012Metric", p).status == OK
    assert verify.check_package("R_0603_1608Metric", p).status == MISMATCH
    assert verify.check_package("R_0603_1608Metric", ProductInfo("q")).status == UNKNOWN


def test_check_datasheet_missing_vs_present():
    assert verify.check_datasheet("", ProductInfo("q", datasheet_url="http://d")).status == OK
    assert verify.check_datasheet("", ProductInfo("q")).status == MISSING


# -- whole-line verdicts ----------------------------------------------------

def _line(value, footprint, mpn, product):
    line = BomLine(references=["R1"], value=value, footprint=footprint, mpn=mpn, dnp=False)
    line.product = product
    return line


def test_verdict_ok_when_everything_lines_up():
    p = ProductInfo("10k", mpn="RC0805FR-0710KL", datasheet_url="http://d",
                    parameters={"Resistance": "10 kOhms"}, package="0805 (2012 Metric)")
    v = verify.verify_line(_line("10k", "R_0805_2012Metric", "RC0805FR-0710KL", p))
    assert v.status == V_OK


def test_verdict_fail_on_value_mismatch():
    p = ProductInfo("10k", mpn="X", datasheet_url="http://d",
                    parameters={"Resistance": "1 kOhms"}, package="0805 (2012 Metric)")
    v = verify.verify_line(_line("10k", "R_0805_2012Metric", "X", p))
    assert v.status == V_FAIL
    assert any("value" in m for m in v.problems)


def test_verdict_no_match_when_product_missing():
    v = verify.verify_line(_line("10k", "R_0805", "X", None))
    assert v.status == V_NO_MATCH


def test_verdict_warn_when_datasheet_missing_but_value_ok():
    p = ProductInfo("10k", parameters={"Resistance": "10 kOhms"})  # no datasheet
    v = verify.verify_line(_line("10k", "R_0805_2012Metric", "", p))
    assert v.status == V_WARN


def test_verify_bom_skips_dnp_by_default():
    p = ProductInfo("10k", datasheet_url="http://d", parameters={"Resistance": "10 kOhms"})
    keep = _line("10k", "R_0805_2012Metric", "", p)
    drop = BomLine(references=["R9"], value="10k", footprint="R_0805", mpn="", dnp=True)
    drop.product = p

    class _Bom:
        lines = [keep, drop]

    verdicts = verify.verify_bom(_Bom())
    assert len(verdicts) == 1
    assert verdicts[0].references == ["R1"]
