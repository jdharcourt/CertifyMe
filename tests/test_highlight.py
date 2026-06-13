import json

from certifyme import highlight, kicad_theme
from certifyme.highlight import FLAG_DATASHEET, FLAG_PRICE
from certifyme.providers.base import ProductInfo


# -- classification ---------------------------------------------------------

def test_classify_both_missing_when_no_product():
    flags = highlight.classify(existing_datasheet=None, product=None)
    assert flags == {FLAG_DATASHEET, FLAG_PRICE}


def test_classify_datasheet_present_via_existing_field():
    flags = highlight.classify(existing_datasheet="https://x/y.pdf", product=None)
    assert flags == {FLAG_PRICE}  # has datasheet, still no price


def test_classify_price_present_datasheet_missing():
    prod = ProductInfo(query="q", unit_price=1.23)  # price but no datasheet_url
    flags = highlight.classify(existing_datasheet="~", product=prod)
    assert flags == {FLAG_DATASHEET}


def test_classify_nothing_missing():
    prod = ProductInfo(query="q", unit_price=1.23, datasheet_url="https://d")
    assert highlight.classify(existing_datasheet=None, product=prod) == set()


# -- search key -------------------------------------------------------------

def test_search_key_prefers_mpn_then_value():
    assert highlight.search_key({"MPN": "LM358DR", "Value": "LM358"}) == "LM358DR"
    assert highlight.search_key({"Value": "100nF"}) == "100nF"
    assert highlight.search_key({"Value": "~"}) is None
    assert highlight.search_key({}) is None


# -- geometry & styles ------------------------------------------------------

def test_inflate():
    assert highlight.inflate(10, 20, 30, 40, 5) == (5, 15, 35, 45)


def test_default_styles_are_white_and_cyan():
    ds = highlight.DEFAULT_STYLES[FLAG_DATASHEET]
    pr = highlight.DEFAULT_STYLES[FLAG_PRICE]
    assert "255, 255, 255" in ds.rgba and "0.30" in ds.rgba   # white @30%
    assert "0, 255, 255" in pr.rgba and "0.30" in pr.rgba     # cyan  @30%
    assert ds.layer == "Eco1_User" and pr.layer == "Eco2_User"


# -- colour theme editing ---------------------------------------------------

def test_find_theme_follows_active_user_theme(tmp_path, monkeypatch):
    monkeypatch.setenv("KICAD_CONFIG_HOME", str(tmp_path))
    ver = tmp_path / "8.0"
    (ver / "colors").mkdir(parents=True)
    (ver / "pcbnew.json").write_text(json.dumps({"appearance": {"color_theme": "mytheme"}}))
    theme = ver / "colors" / "mytheme.json"
    theme.write_text(json.dumps({"board": {}}))
    assert kicad_theme.find_color_theme() == theme


def test_builtin_theme_falls_back_to_user(tmp_path, monkeypatch):
    monkeypatch.setenv("KICAD_CONFIG_HOME", str(tmp_path))
    ver = tmp_path / "8.0"
    (ver / "colors").mkdir(parents=True)
    (ver / "pcbnew.json").write_text(json.dumps({"appearance": {"color_theme": "_builtin_default"}}))
    user = ver / "colors" / "user.json"
    user.write_text(json.dumps({"board": {}}))
    assert kicad_theme.find_color_theme() == user


def test_apply_and_restore_colors(tmp_path):
    theme = tmp_path / "theme.json"
    theme.write_text(json.dumps({"board": {"eco1_user": "rgb(0, 132, 0)"}}))
    mapping = {"eco1_user": "rgba(255, 255, 255, 0.30)", "eco2_user": "rgba(0, 255, 255, 0.30)"}
    previous = kicad_theme.apply_highlight_colors(theme, mapping)

    after = json.loads(theme.read_text())["board"]
    assert after["eco1_user"] == "rgba(255, 255, 255, 0.30)"
    assert after["eco2_user"] == "rgba(0, 255, 255, 0.30)"
    assert previous == {"eco1_user": "rgb(0, 132, 0)", "eco2_user": None}

    kicad_theme.restore_colors(theme, previous)
    board = json.loads(theme.read_text())["board"]
    assert board["eco1_user"] == "rgb(0, 132, 0)"   # original restored
    assert "eco2_user" not in board                 # absent key removed
