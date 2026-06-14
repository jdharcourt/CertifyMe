"""CertifyMe KiCad Action Plugin.

Registers a toolbar button in the PCB Editor (pcbnew) that scans the open
project for parts and links each one to its datasheet via the DigiKey API.

It works on two surfaces:

1. The **live board** — footprints loaded in pcbnew get their ``Datasheet``
   field updated through the pcbnew Python API, so changes are visible
   immediately (just save the board afterwards).
2. The **project files** — symbol libraries (.kicad_sym), footprint libraries
   (.kicad_mod) and the schematic (.kicad_sch) are scanned and rewritten on
   disk by the bundled CertifyMe engine.

It can also **generate a priced Bill of Materials** (Excel + CSV) from the
project's schematic, with part counts, unit/extended prices, stock, and links
to each part and its datasheet.

Finally, it can **highlight missing info on the board**: footprints whose
datasheet couldn't be found get a translucent white box over them, those whose
price couldn't be found get a translucent cyan one, with a clickable list to
zoom to each flagged part.

The engine is imported as a bundled subpackage when installed, or from the
repo's ``src/`` directory when run from a checkout.
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

import pcbnew
import wx

# --- locate the CertifyMe engine ------------------------------------------
try:  # installed layout: certifyme/ sits next to this file
    from .certifyme import bom as bom_mod
    from .certifyme import config
    from .certifyme import highlight, kicad_theme, parttype
    from .certifyme import verify as verify_mod
    from .certifyme.linker import PartResult, link_project, summarize
    from .certifyme.open_file import open_file
    from .certifyme.providers import build_provider
except ImportError:  # dev checkout: engine lives in ../src
    _src = os.path.join(os.path.dirname(__file__), "..", "src")
    if _src not in sys.path:
        sys.path.insert(0, _src)
    from certifyme import bom as bom_mod  # type: ignore
    from certifyme import config  # type: ignore
    from certifyme import highlight, kicad_theme, parttype  # type: ignore
    from certifyme import verify as verify_mod  # type: ignore
    from certifyme.linker import PartResult, link_project, summarize  # type: ignore
    from certifyme.open_file import open_file  # type: ignore
    from certifyme.providers import build_provider  # type: ignore


class CertifyMeDatasheetPlugin(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = "CertifyMe: Link Datasheets"
        self.category = "Modify PCB"
        self.description = (
            "Find each part's datasheet online (DigiKey) and link it into the "
            "matching symbol, footprint and schematic part."
        )
        self.show_toolbar_button = True
        icon = os.path.join(os.path.dirname(__file__), "icon.png")
        self.icon_file_name = icon if os.path.exists(icon) else ""
        self.dark_icon_file_name = self.icon_file_name

    def Run(self):
        try:
            _run()
        except Exception:  # never let an exception escape into pcbnew
            wx.MessageBox(
                "CertifyMe failed:\n\n" + traceback.format_exc(),
                "CertifyMe error",
                wx.OK | wx.ICON_ERROR,
            )


def _project_dir() -> str:
    board = pcbnew.GetBoard()
    path = board.GetFileName() if board else ""
    return os.path.dirname(path) if path else os.getcwd()


def _run() -> None:
    project = _project_dir()
    dlg = CertifyMeDialog(project)
    try:
        dlg.ShowModal()
    finally:
        dlg.Destroy()


def _warning_bitmap(size: int = 16) -> "wx.Bitmap":
    """A small orange circle with a white exclamation mark, drawn at runtime so
    the plugin needs no icon asset shipped alongside it."""
    bmp = wx.Bitmap(size, size)
    dc = wx.MemoryDC(bmp)
    try:
        # Transparent-ish background: paint the panel's face colour then the circle.
        bg = wx.SystemSettings.GetColour(wx.SYS_COLOUR_BTNFACE)
        dc.SetBackground(wx.Brush(bg))
        dc.Clear()
        gc = wx.GraphicsContext.Create(dc)
        if gc:
            gc.SetBrush(wx.Brush(wx.Colour(0xF5, 0x9E, 0x0B)))  # amber/orange
            gc.SetPen(wx.Pen(wx.Colour(0xB4, 0x6F, 0x00)))
            gc.DrawEllipse(0.5, 0.5, size - 1.5, size - 1.5)
            gc.SetFont(
                wx.Font(
                    int(size * 0.7), wx.FONTFAMILY_DEFAULT,
                    wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD,
                ),
                wx.Colour(0xFF, 0xFF, 0xFF),
            )
            tw, th = gc.GetTextExtent("!")[:2]
            gc.DrawText("!", (size - tw) / 2.0, (size - th) / 2.0)
    finally:
        dc.SelectObject(wx.NullBitmap)
    bmp.SetMaskColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_BTNFACE))
    return bmp


# --------------------------------------------------------------------------
# Live-board footprint helpers (pcbnew API shapes vary across KiCad versions,
# so every access is guarded).
# --------------------------------------------------------------------------

def _fp_fields(fp) -> dict:
    if hasattr(fp, "GetFields"):
        try:
            return {f.GetName(): f for f in fp.GetFields()}
        except Exception:
            pass
    return {}


def _fp_footprint_name(fp) -> str:
    """The footprint's library item name (e.g. ``R_0805_2012Metric``), guarded
    against pcbnew API differences. Used for generic package inference."""
    try:
        fpid = fp.GetFPID()
        return str(fpid.GetLibItemName()) if fpid else ""
    except Exception:
        return ""


def _update_board(board, provider, *, overwrite, prefer_field, dry_run, guess_datasheets, log):
    """Best-effort live update of footprints on the open board."""
    linked = 0
    if board is None:
        return linked
    for fp in board.GetFootprints():
        fields = _fp_fields(fp)
        ds_field = fields.get("Datasheet")
        current = ds_field.GetText().strip() if ds_field else ""
        if current and current not in ("~", "") and not overwrite:
            continue

        # Build a search key: explicit field, then common MPN fields, then Value.
        key = None
        for name in ([prefer_field] if prefer_field else []) + [
            "MPN", "Manufacturer Part Number", "Part Number",
        ]:
            f = fields.get(name)
            if f and f.GetText().strip():
                key = f.GetText().strip()
                break
        if not key:
            try:
                value = fp.GetValue().strip()
            except Exception:
                value = ""
            if value and value not in ("~", ""):
                key = value
        if not key:
            continue

        url = provider.find_datasheet(key)
        generic = False
        if not url and guess_datasheets:
            try:
                value = fp.GetValue().strip()
            except Exception:
                value = ""
            gq = parttype.generic_query_from(fp.GetReference(), value, _fp_footprint_name(fp))
            if gq:
                url = provider.find_datasheet(gq)
                generic = bool(url)
        if not url:
            log(f"  [?] {fp.GetReference():6} {key}  (no datasheet found)")
            continue
        tag = "!" if generic else "+"
        suffix = "  (generic - verify)" if generic else ""
        log(f"  [{tag}] {fp.GetReference():6} {key} -> {url}{suffix}")
        linked += 1
        if not dry_run and ds_field is not None:
            try:
                ds_field.SetText(url)
            except Exception:
                pass
    if linked and not dry_run:
        try:
            pcbnew.Refresh()
        except Exception:
            pass
    return linked


# --------------------------------------------------------------------------
# Missing-info highlighter: cover footprints that are missing a datasheet
# (white) or a price (cyan) with translucent filled boxes on user layers.
# --------------------------------------------------------------------------

_HL_GROUP_PREFIX = "CertifyMe-Highlight:"
_HL_OUTLINE_MM = 0.2          # box border line width
_HL_MARGIN_MM = 0.3           # gap between the part and its box


def _mm(value_mm: float) -> int:
    """Millimetres -> KiCad internal units (nanometres)."""
    try:
        return pcbnew.FromMM(value_mm)
    except Exception:
        return int(value_mm * 1_000_000)


def _board_search_key(fp) -> str | None:
    fields = _fp_fields(fp)
    props = {name: f.GetText() for name, f in fields.items()}
    try:
        value = fp.GetValue()
    except Exception:
        value = ""
    return highlight.search_key(props, value)


def scan_missing(board, provider, *, log=lambda _m: None) -> list[dict]:
    """Return [{fp, ref, value, flags}] for footprints missing datasheet/price."""
    flagged: list[dict] = []
    if board is None:
        return flagged
    for fp in board.GetFootprints():
        try:
            ref = fp.GetReference()
        except Exception:
            ref = "?"
        if ref.startswith("#"):  # power/flag pseudo-parts
            continue
        fields = _fp_fields(fp)
        ds_field = fields.get("Datasheet")
        existing = ds_field.GetText() if ds_field else ""
        key = _board_search_key(fp)
        product = None
        if key:
            try:
                product = provider.find_product(key)
            except Exception as exc:
                log(f"  lookup failed for {ref} ({key}): {exc}")
        flags = highlight.classify(existing_datasheet=existing, product=product)
        if flags:
            try:
                value = fp.GetValue()
            except Exception:
                value = ""
            flagged.append({"fp": fp, "ref": ref, "value": value, "flags": flags})
    return flagged


def _footprint_bbox(fp):
    """(x0, y0, x1, y1) bounding box of *fp* in internal units, or None."""
    for getter in ("GetBoundingBox",):
        if not hasattr(fp, getter):
            continue
        try:
            bb = getattr(fp, getter)()
        except Exception:
            try:
                bb = fp.GetBoundingBox(False, False)
            except Exception:
                continue
        try:
            x0, y0 = bb.GetX(), bb.GetY()
            return x0, y0, x0 + bb.GetWidth(), y0 + bb.GetHeight()
        except Exception:
            continue
    return None


def _make_box(board, x0, y0, x1, y1, layer_id):
    """A filled rectangle covering the part. The fill renders translucent because
    the user layer it sits on is coloured at ~30% opacity (see _recolor_layers),
    so it reads as a tint over the footprint rather than a hollow outline."""
    shape = pcbnew.PCB_SHAPE(board)
    rect_t = getattr(pcbnew, "SHAPE_T_RECTANGLE", None) or getattr(pcbnew, "SHAPE_T_RECT", None)
    if rect_t is not None:
        shape.SetShape(rect_t)
    shape.SetStart(pcbnew.VECTOR2I(int(x0), int(y0)))
    shape.SetEnd(pcbnew.VECTOR2I(int(x1), int(y1)))
    shape.SetLayer(layer_id)
    try:
        shape.SetWidth(_mm(_HL_OUTLINE_MM))
    except Exception:
        pass
    # Fill the box so it tints the whole part; the layer's 30% opacity makes it
    # translucent. SetFillMode is the newer API; fall back to SetFilled.
    try:
        fill_solid = getattr(pcbnew, "FILL_T_FILLED_SHAPE", None)
        if fill_solid is not None and hasattr(shape, "SetFillMode"):
            shape.SetFillMode(fill_solid)
        else:
            shape.SetFilled(True)
    except Exception:
        try:
            shape.SetFilled(True)
        except Exception:
            pass
    return shape


def draw_highlights(board, flagged, styles=None) -> int:
    """Draw outlines for *flagged* parts; group them so they can be cleared.
    Returns the number of outlines drawn."""
    styles = styles or highlight.DEFAULT_STYLES
    groups: dict[str, object] = {}
    margin = _mm(_HL_MARGIN_MM)
    drawn = 0
    for entry in flagged:
        bbox = _footprint_bbox(entry["fp"])
        if not bbox:
            continue
        x0, y0, x1, y1 = highlight.inflate(*bbox, margin)
        for flag in entry["flags"]:
            style = styles.get(flag)
            layer_id = getattr(pcbnew, style.layer, None) if style else None
            if layer_id is None:
                continue
            group = groups.get(flag)
            if group is None:
                group = pcbnew.PCB_GROUP(board)
                group.SetName(_HL_GROUP_PREFIX + flag)
                board.Add(group)
                groups[flag] = group
            shape = _make_box(board, x0, y0, x1, y1, layer_id)
            board.Add(shape)
            try:
                group.AddItem(shape)
            except Exception:
                pass
            drawn += 1
    if drawn:
        try:
            pcbnew.Refresh()
        except Exception:
            pass
    return drawn


def clear_highlights(board) -> int:
    """Remove every CertifyMe highlight group and its outlines. Returns count."""
    if board is None:
        return 0
    removed = 0
    try:
        groups = list(board.Groups())
    except Exception:
        groups = []
    for group in groups:
        try:
            name = group.GetName()
        except Exception:
            continue
        if not name.startswith(_HL_GROUP_PREFIX):
            continue
        try:
            items = list(group.GetItems())
        except Exception:
            items = []
        for item in items:
            try:
                board.Remove(item)
                removed += 1
            except Exception:
                pass
        try:
            board.Remove(group)
        except Exception:
            pass
    if removed:
        try:
            pcbnew.Refresh()
        except Exception:
            pass
    return removed


class CertifyMeDialog(wx.Dialog):
    def __init__(self, project: str):
        super().__init__(None, title="CertifyMe — Link Datasheets", size=(640, 760))
        self.project = project
        panel = wx.Panel(self)
        outer = wx.BoxSizer(wx.VERTICAL)

        outer.Add(
            wx.StaticText(panel, label=f"Project: {project or '(unsaved board)'}"),
            0, wx.ALL, 8,
        )

        # --- DigiKey credentials -----------------------------------------
        cred_box = wx.StaticBoxSizer(wx.VERTICAL, panel, "DigiKey API credentials")
        info = config.resolve(project or None)
        grid = wx.FlexGridSizer(2, 2, 4, 6)
        grid.AddGrowableCol(1, 1)
        grid.Add(wx.StaticText(panel, label="Client ID:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.id_ctrl = wx.TextCtrl(panel, value=info["client_id"])
        grid.Add(self.id_ctrl, 1, wx.EXPAND)
        grid.Add(wx.StaticText(panel, label="Client Secret:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.secret_ctrl = wx.TextCtrl(panel, value=info["client_secret"], style=wx.TE_PASSWORD)
        grid.Add(self.secret_ctrl, 1, wx.EXPAND)
        cred_box.Add(grid, 0, wx.EXPAND | wx.ALL, 4)

        cred_btns = wx.BoxSizer(wx.HORIZONTAL)
        self.sandbox_cb = wx.CheckBox(panel, label="Sandbox")
        self.sandbox_cb.SetValue(info["sandbox"])
        self.save_btn = wx.Button(panel, label="Save credentials")
        self.test_btn = wx.Button(panel, label="Test")
        cred_btns.Add(self.sandbox_cb, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 12)
        cred_btns.AddStretchSpacer()
        cred_btns.Add(self.save_btn, 0, wx.RIGHT, 6)
        cred_btns.Add(self.test_btn, 0)
        cred_box.Add(cred_btns, 0, wx.EXPAND | wx.ALL, 4)
        src = "saved" if info["configured"] else "not set - enter keys above"
        self.cred_status = wx.StaticText(panel, label=f"Status: {src}")
        cred_box.Add(self.cred_status, 0, wx.ALL, 4)
        outer.Add(cred_box, 0, wx.EXPAND | wx.ALL, 8)

        # Provider
        prov_box = wx.BoxSizer(wx.HORIZONTAL)
        prov_box.Add(wx.StaticText(panel, label="Provider:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.provider_choice = wx.Choice(panel, choices=["digikey", "dummy"])
        self.provider_choice.SetSelection(0)
        prov_box.Add(self.provider_choice, 0)
        outer.Add(prov_box, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # Options
        self.dry_run = wx.CheckBox(panel, label="Dry run (don't write anything)")
        self.overwrite = wx.CheckBox(panel, label="Overwrite existing datasheet links")
        self.do_board = wx.CheckBox(panel, label="Update footprints on the open board")
        self.do_files = wx.CheckBox(panel, label="Update project files (symbols, footprint libs, schematic)")
        self.dry_run.SetValue(True)
        self.do_board.SetValue(True)
        self.do_files.SetValue(True)
        for cb in (self.dry_run, self.overwrite, self.do_board, self.do_files):
            outer.Add(cb, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)

        # Search-key field
        key_box = wx.BoxSizer(wx.HORIZONTAL)
        key_box.Add(wx.StaticText(panel, label="Search-key field (optional):"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.field_ctrl = wx.TextCtrl(panel, value="", size=(160, -1))
        self.field_ctrl.SetHint("e.g. MPN")
        key_box.Add(self.field_ctrl, 0)
        outer.Add(key_box, 0, wx.ALL, 8)

        # Log
        self.log_ctrl = wx.TextCtrl(
            panel, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL, size=(-1, 150)
        )
        outer.Add(self.log_ctrl, 1, wx.EXPAND | wx.ALL, 8)

        # Flagged-parts list (the PCB-highlight interface). Double-click zooms.
        outer.Add(
            wx.StaticText(panel, label="Flagged parts (double-click to zoom) — "
                          "missing: white=datasheet, cyan=price; verify: magenta=mismatch:"),
            0, wx.LEFT | wx.RIGHT, 8,
        )
        self.flag_list = wx.ListCtrl(panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL, size=(-1, 120))
        self.flag_list.InsertColumn(0, "Ref", width=70)
        self.flag_list.InsertColumn(1, "Value", width=140)
        self.flag_list.InsertColumn(2, "Missing", width=180)
        outer.Add(self.flag_list, 1, wx.EXPAND | wx.ALL, 8)
        self._flag_fps: list[object] = []  # row index -> footprint

        # Open the BOM after writing it (Windows shows the "How do you want to
        # open this file?" chooser with Always / Just once).
        self.open_after_bom = wx.CheckBox(panel, label="Open BOM after export")
        self.open_after_bom.SetValue(True)
        outer.Add(self.open_after_bom, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # For parts DigiKey can't match, look up a representative part of the same
        # type (e.g. "10k resistor 0805") and use its datasheet, marked generic.
        # Applies to both "Link Datasheets" and "Generate BOM".
        guess_row = wx.BoxSizer(wx.HORIZONTAL)
        self.guess_datasheets = wx.CheckBox(
            panel, label="Guess datasheets for unfound parts (BOM + datasheet linking)"
        )
        self.guess_datasheets.SetValue(True)
        guess_row.Add(self.guess_datasheets, 0, wx.ALIGN_CENTER_VERTICAL)
        warn = wx.StaticBitmap(panel, bitmap=_warning_bitmap())
        warn.SetToolTip(
            "Generic datasheets are a best guess from the part's type and package. "
            "They may not be the exact part — verify before relying on them."
        )
        guess_row.Add(warn, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 6)
        outer.Add(guess_row, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # Buttons
        btns = wx.BoxSizer(wx.HORIZONTAL)
        self.run_btn = wx.Button(panel, label="Link Datasheets")
        self.bom_btn = wx.Button(panel, label="Generate BOM...")
        self.verify_btn = wx.Button(panel, label="Verify BOM")
        self.hl_btn = wx.Button(panel, label="Highlight Missing")
        self.clear_btn = wx.Button(panel, label="Clear Highlights")
        close_btn = wx.Button(panel, id=wx.ID_CANCEL, label="Close")
        btns.Add(self.hl_btn, 0, wx.RIGHT, 6)
        btns.Add(self.clear_btn, 0, wx.RIGHT, 6)
        btns.AddStretchSpacer()
        btns.Add(self.run_btn, 0, wx.RIGHT, 6)
        btns.Add(self.bom_btn, 0, wx.RIGHT, 6)
        btns.Add(self.verify_btn, 0, wx.RIGHT, 6)
        btns.Add(close_btn, 0)
        outer.Add(btns, 0, wx.EXPAND | wx.ALL, 8)

        panel.SetSizer(outer)
        self.run_btn.Bind(wx.EVT_BUTTON, self.on_run)
        self.bom_btn.Bind(wx.EVT_BUTTON, self.on_bom)
        self.verify_btn.Bind(wx.EVT_BUTTON, self.on_verify)
        self.hl_btn.Bind(wx.EVT_BUTTON, self.on_highlight)
        self.clear_btn.Bind(wx.EVT_BUTTON, self.on_clear_highlights)
        self.flag_list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.on_zoom_to_part)
        self.save_btn.Bind(wx.EVT_BUTTON, self.on_save_creds)
        self.test_btn.Bind(wx.EVT_BUTTON, self.on_test_creds)

        self._theme_path = None      # set when we recolour the theme
        self._theme_prev = None

    def log(self, msg: str) -> None:
        self.log_ctrl.AppendText(msg + "\n")
        wx.GetApp().Yield()

    # -- credential handling ------------------------------------------------

    def _apply_creds_to_env(self) -> None:
        """Push whatever is currently in the credential fields into the env so a
        run uses them even if they haven't been saved to disk."""
        config.load_into_env(self.project or None)
        cid = self.id_ctrl.GetValue().strip()
        secret = self.secret_ctrl.GetValue().strip()
        if cid:
            os.environ["DIGIKEY_CLIENT_ID"] = cid
        if secret:
            os.environ["DIGIKEY_CLIENT_SECRET"] = secret
        os.environ["DIGIKEY_SANDBOX"] = "1" if self.sandbox_cb.GetValue() else "0"

    def on_save_creds(self, _evt) -> None:
        cid = self.id_ctrl.GetValue().strip()
        secret = self.secret_ctrl.GetValue().strip()
        if not cid or not secret:
            wx.MessageBox("Enter both a Client ID and Client Secret first.",
                          "CertifyMe", wx.OK | wx.ICON_WARNING)
            return
        # Prefer per-project storage when a project is open, else global.
        scope = "project" if (self.project and os.path.isdir(self.project)) else "global"
        path = config.save_credentials(
            cid, secret, sandbox=self.sandbox_cb.GetValue(),
            scope=scope, project_dir=self.project or None,
        )
        self.cred_status.SetLabel(f"Status: saved to {path}")
        self.log(f"Saved DigiKey credentials to {path}")

    def on_test_creds(self, _evt) -> None:
        self._apply_creds_to_env()
        self.test_btn.Disable()
        try:
            provider = build_provider("digikey")
            url = provider.find_datasheet("STM32F103C8T6")
            if url:
                self.cred_status.SetLabel("Status: connection OK")
                self.log(f"Test OK - example datasheet: {url}")
            else:
                self.cred_status.SetLabel("Status: connected (no result for test part)")
                self.log("Test connected, but no datasheet for the test part.")
        except Exception as exc:
            self.cred_status.SetLabel("Status: test failed")
            self.log(f"Test failed: {exc}")
        finally:
            self.test_btn.Enable()

    def on_run(self, _evt) -> None:
        self.log_ctrl.SetValue("")
        self.run_btn.Disable()
        try:
            self._do_run()
        except Exception:
            self.log("ERROR:\n" + traceback.format_exc())
        finally:
            self.run_btn.Enable()

    def on_bom(self, _evt) -> None:
        self.log_ctrl.SetValue("")
        self.bom_btn.Disable()
        try:
            self._do_bom()
        except Exception:
            self.log("ERROR:\n" + traceback.format_exc())
        finally:
            self.bom_btn.Enable()

    def _do_bom(self) -> None:
        provider_name = self.provider_choice.GetStringSelection()
        self._apply_creds_to_env()
        try:
            provider = build_provider(provider_name)
        except Exception as exc:
            self.log(f"Cannot start provider '{provider_name}': {exc}")
            self.log("Enter your DigiKey keys above (Save / Test), then retry.")
            return

        if not self.project or not os.path.isdir(self.project):
            self.log("Save the project first so its schematic can be found.")
            return

        default_name = f"{os.path.basename(self.project)}-BOM.xlsx"
        dlg = wx.FileDialog(
            self, "Save BOM", defaultDir=self.project, defaultFile=default_name,
            wildcard="Excel workbook (*.xlsx)|*.xlsx",
            style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT,
        )
        try:
            if dlg.ShowModal() != wx.ID_OK:
                self.log("BOM cancelled.")
                return
            out_path = dlg.GetPath()
        finally:
            dlg.Destroy()

        self.log("Building BOM (pricing parts)...\n")
        bom = bom_mod.build_bom(
            Path(self.project),
            provider,
            guess_datasheets=self.guess_datasheets.GetValue(),
            on_event=lambda l: self.log(
                f"  {l.quantity:>3}x  {l.value:16} "
                f"{(l.unit_price and f'{l.unit_price:.4f}') or '-':>9}  [{l.refs_text}]"
            ),
        )
        if not bom.lines:
            self.log("No components found in the project's schematic.")
            return
        bom_mod.write_xlsx_bom(bom, out_path)
        csv_path = os.path.splitext(out_path)[0] + ".csv"
        bom_mod.write_csv_bom(bom, csv_path)
        self.log("\n" + bom_mod.summarize(bom))
        self.log(f"\nWrote:\n  {out_path}\n  {csv_path}")

    # -- BOM verification ---------------------------------------------------

    def on_verify(self, _evt) -> None:
        self.log_ctrl.SetValue("")
        self.verify_btn.Disable()
        try:
            self._do_verify()
        except Exception:
            self.log("ERROR:\n" + traceback.format_exc())
        finally:
            self.verify_btn.Enable()

    def _do_verify(self) -> None:
        provider_name = self.provider_choice.GetStringSelection()
        self._apply_creds_to_env()
        try:
            provider = build_provider(provider_name)
        except Exception as exc:
            self.log(f"Cannot start provider '{provider_name}': {exc}")
            self.log("Enter your DigiKey keys above (Save / Test), then retry.")
            return
        if not self.project or not os.path.isdir(self.project):
            self.log("Save the project first so its schematic/board can be verified.")
            return

        self.log("Verifying BOM parts against DigiKey (MPN / value / package)...\n")
        bom = bom_mod.build_bom(Path(self.project), provider)
        if not bom.lines:
            self.log("No components found in the project to verify.")
            return
        verdicts = verify_mod.verify_bom(bom)

        # Report: list everything that isn't a clean pass.
        problems = [v for v in verdicts if v.status != verify_mod.V_OK]
        for v in problems:
            badge = {
                verify_mod.V_FAIL: "MISMATCH",
                verify_mod.V_WARN: "warn",
                verify_mod.V_NO_MATCH: "not found",
            }.get(v.status, v.status)
            self.log(f"  [{badge}] {v.refs_text}  {v.value}  {v.mpn}")
            for msg in v.problems:
                self.log(f"        - {msg}")
        self.log("\n" + verify_mod.summarize(verdicts))

        # Box the parts whose board specs contradict DigiKey (magenta) so they
        # can be found on the canvas, and list them for click-to-zoom.
        board = pcbnew.GetBoard()
        fails = [v for v in verdicts if v.status == verify_mod.V_FAIL]
        ref_to_fp = self._ref_to_fp(board)
        self._populate_verify_list(problems, ref_to_fp)

        if board is not None and fails:
            clear_highlights(board)
            flagged = []
            for v in fails:
                for ref in v.references:
                    fp = ref_to_fp.get(ref)
                    if fp is not None:
                        flagged.append({"fp": fp, "ref": ref, "value": v.value,
                                        "flags": {highlight.FLAG_SPEC}})
            drawn = draw_highlights(board, flagged, styles=highlight.SPEC_STYLES)
            self.log(f"\nBoxed {drawn} mismatching part(s) in magenta on the board.")
            self._recolor_layers(highlight.SPEC_STYLES)
            self.log("Tip: double-click a row above to zoom to that part.")
        elif fails:
            self.log("\n(Open the board in the PCB editor to box mismatches on the canvas.)")

    def _ref_to_fp(self, board) -> dict:
        out: dict[str, object] = {}
        if board is None:
            return out
        for fp in board.GetFootprints():
            try:
                out[fp.GetReference()] = fp
            except Exception:
                pass
        return out

    def _populate_verify_list(self, verdicts, ref_to_fp) -> None:
        self.flag_list.DeleteAllItems()
        self._flag_fps = []
        for v in verdicts:
            summary = "; ".join(v.problems) or v.status
            row = self.flag_list.InsertItem(self.flag_list.GetItemCount(), v.refs_text)
            self.flag_list.SetItem(row, 1, v.value)
            self.flag_list.SetItem(row, 2, summary)
            fp = next((ref_to_fp.get(r) for r in v.references if ref_to_fp.get(r)), None)
            self._flag_fps.append(fp)

    # -- PCB highlighting ---------------------------------------------------

    def on_highlight(self, _evt) -> None:
        self.log_ctrl.SetValue("")
        self.hl_btn.Disable()
        try:
            self._do_highlight()
        except Exception:
            self.log("ERROR:\n" + traceback.format_exc())
        finally:
            self.hl_btn.Enable()

    def _do_highlight(self) -> None:
        board = pcbnew.GetBoard()
        if board is None:
            self.log("No board open in the PCB editor.")
            return
        provider_name = self.provider_choice.GetStringSelection()
        self._apply_creds_to_env()
        try:
            provider = build_provider(provider_name)
        except Exception as exc:
            self.log(f"Cannot start provider '{provider_name}': {exc}")
            self.log("Enter your DigiKey keys above (Save / Test), then retry.")
            return

        clear_highlights(board)  # start from a clean slate
        self.log("Scanning board footprints for missing datasheet / price...\n")
        flagged = scan_missing(board, provider, log=self.log)

        self._populate_flag_list(flagged)
        if not flagged:
            self.log("All footprints have a datasheet and a price. Nothing to flag.")
            return

        ds = sum(1 for e in flagged if highlight.FLAG_DATASHEET in e["flags"])
        pr = sum(1 for e in flagged if highlight.FLAG_PRICE in e["flags"])
        drawn = draw_highlights(board, flagged)
        self.log(f"Outlined {len(flagged)} part(s): {ds} missing datasheet (white), "
                 f"{pr} missing price (cyan). Drew {drawn} outline(s).")

        self._recolor_layers()
        self.log("\nTip: double-click a row above to zoom to that part.")

    def _recolor_layers(self, styles=None) -> None:
        """Set the user layers used by *styles* to their translucent colours if
        the active theme is editable (defaults to the missing-info styles)."""
        styles = styles or highlight.DEFAULT_STYLES
        mapping = {s.theme_key: s.rgba for s in styles.values()}
        theme = kicad_theme.find_color_theme()
        if theme is None:
            names = ", ".join(f"'{s.layer.replace('_', '.')}'" for s in styles.values())
            self.log(
                f"\nColours: couldn't auto-set them (built-in/locked theme). In the "
                f"Appearance panel set {names} at ~30% opacity to match."
            )
            return
        try:
            prev = kicad_theme.apply_highlight_colors(theme, mapping)
            # Merge with anything already saved so a later pass (e.g. verify after
            # missing-info) doesn't lose the original colours of other layers.
            if self._theme_prev is None:
                self._theme_prev = {}
            for key, val in prev.items():
                self._theme_prev.setdefault(key, val)
            self._theme_path = theme
            applied = ", ".join(
                f"{s.layer.replace('_', '.')} ({s.label})" for s in styles.values()
            )
            self.log(
                f"\nColours: set {applied} @30% in {theme.name}. "
                "If the canvas colours don't change, reopen the board or re-select the "
                "colour theme in Preferences."
            )
        except Exception as exc:
            self.log(f"\nColours: could not edit theme ({exc}); set them manually in Appearance.")

    def on_clear_highlights(self, _evt) -> None:
        board = pcbnew.GetBoard()
        removed = clear_highlights(board)
        if self._theme_path and self._theme_prev is not None:
            try:
                kicad_theme.restore_colors(self._theme_path, self._theme_prev)
            except Exception:
                pass
            self._theme_path = self._theme_prev = None
        self.flag_list.DeleteAllItems()
        self._flag_fps = []
        self.log(f"Cleared {removed} highlight outline(s).")

    def _populate_flag_list(self, flagged) -> None:
        self.flag_list.DeleteAllItems()
        self._flag_fps = []
        order = {highlight.FLAG_DATASHEET: "datasheet", highlight.FLAG_PRICE: "price"}
        for entry in flagged:
            missing = ", ".join(order[f] for f in order if f in entry["flags"])
            row = self.flag_list.InsertItem(self.flag_list.GetItemCount(), entry["ref"])
            self.flag_list.SetItem(row, 1, entry["value"])
            self.flag_list.SetItem(row, 2, missing)
            self._flag_fps.append(entry["fp"])

    def on_zoom_to_part(self, evt) -> None:
        idx = evt.GetIndex()
        if 0 <= idx < len(self._flag_fps):
            fp = self._flag_fps[idx]
            if fp is None:
                self.log("That part isn't on the open board (schematic-only).")
                return
            try:
                pcbnew.FocusOnItem(fp)
                pcbnew.Refresh()
            except Exception:
                self.log("Could not zoom to the selected part (API unavailable).")

    def _do_run(self) -> None:
        provider_name = self.provider_choice.GetStringSelection()
        dry = self.dry_run.GetValue()
        overwrite = self.overwrite.GetValue()
        prefer_field = self.field_ctrl.GetValue().strip() or None

        # Use the credential fields (saved or just typed) for this run.
        self._apply_creds_to_env()

        try:
            provider = build_provider(provider_name)
        except Exception as exc:
            self.log(f"Cannot start provider '{provider_name}': {exc}")
            if provider_name == "digikey":
                self.log(
                    "Enter your DigiKey Client ID and Secret above, then click "
                    "'Save credentials' (or 'Test').\n"
                    "Get keys at https://developer.digikey.com/\n"
                )
            return

        if dry:
            self.log("[dry run — no files or fields will be written]\n")

        if self.do_board.GetValue():
            self.log("== Live board footprints ==")
            n = _update_board(
                pcbnew.GetBoard(), provider,
                overwrite=overwrite, prefer_field=prefer_field,
                dry_run=dry, guess_datasheets=self.guess_datasheets.GetValue(),
                log=self.log,
            )
            self.log(f"Board footprints linked: {n}\n")

        if self.do_files.GetValue():
            if not self.project or not os.path.isdir(self.project):
                self.log("No saved project directory to scan for files.")
            else:
                self.log("== Project files ==")
                report = link_project(
                    Path(self.project),
                    provider,
                    dry_run=dry,
                    overwrite=overwrite,
                    prefer_field=prefer_field,
                    guess_datasheets=self.guess_datasheets.GetValue(),
                    on_event=lambda r: self._log_event(r),
                )
                self.log("\n" + summarize(report))

        self.log("\nDone." + ("  (dry run — nothing written)" if dry else ""))

    def _log_event(self, r: "PartResult") -> None:
        glyph = {
            "linked": "+", "linked-generic": "!", "already": "=",
            "not-found": "?", "no-key": "-",
        }.get(r.status, " ")
        if r.status in ("linked", "linked-generic", "not-found"):
            suffix = "  (generic - verify)" if r.status == "linked-generic" else ""
            self.log(f"  [{glyph}] {r.part.kind:8} {r.part.name:24} {r.url or r.query or ''}{suffix}")
