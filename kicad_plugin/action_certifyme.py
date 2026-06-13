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
datasheet couldn't be found get a translucent white outline, those whose price
couldn't be found get a translucent cyan one, with a clickable list to zoom to
each flagged part.

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
    from .certifyme import highlight, kicad_theme
    from .certifyme.linker import PartResult, link_project, summarize
    from .certifyme.providers import build_provider
except ImportError:  # dev checkout: engine lives in ../src
    _src = os.path.join(os.path.dirname(__file__), "..", "src")
    if _src not in sys.path:
        sys.path.insert(0, _src)
    from certifyme import bom as bom_mod  # type: ignore
    from certifyme import config  # type: ignore
    from certifyme import highlight, kicad_theme  # type: ignore
    from certifyme.linker import PartResult, link_project, summarize  # type: ignore
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


def _update_board(board, provider, *, overwrite, prefer_field, dry_run, log):
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
        if not url:
            log(f"  [?] {fp.GetReference():6} {key}  (no datasheet found)")
            continue
        log(f"  [+] {fp.GetReference():6} {key} -> {url}")
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
# Missing-info highlighter: outline footprints that are missing a datasheet
# (white) or a price (cyan) with translucent rectangles on user layers.
# --------------------------------------------------------------------------

_HL_GROUP_PREFIX = "CertifyMe-Highlight:"
_HL_OUTLINE_MM = 0.2          # outline line width
_HL_MARGIN_MM = 0.3           # gap between the part and its outline


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


def _make_outline(board, x0, y0, x1, y1, layer_id):
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
    try:
        shape.SetFilled(False)
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
            shape = _make_outline(board, x0, y0, x1, y1, layer_id)
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
            wx.StaticText(panel, label="Missing info (white = datasheet, cyan = price):"),
            0, wx.LEFT | wx.RIGHT, 8,
        )
        self.flag_list = wx.ListCtrl(panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL, size=(-1, 120))
        self.flag_list.InsertColumn(0, "Ref", width=70)
        self.flag_list.InsertColumn(1, "Value", width=140)
        self.flag_list.InsertColumn(2, "Missing", width=180)
        outer.Add(self.flag_list, 1, wx.EXPAND | wx.ALL, 8)
        self._flag_fps: list[object] = []  # row index -> footprint

        # Buttons
        btns = wx.BoxSizer(wx.HORIZONTAL)
        self.run_btn = wx.Button(panel, label="Link Datasheets")
        self.bom_btn = wx.Button(panel, label="Generate BOM...")
        self.hl_btn = wx.Button(panel, label="Highlight Missing")
        self.clear_btn = wx.Button(panel, label="Clear Highlights")
        close_btn = wx.Button(panel, id=wx.ID_CANCEL, label="Close")
        btns.Add(self.hl_btn, 0, wx.RIGHT, 6)
        btns.Add(self.clear_btn, 0, wx.RIGHT, 6)
        btns.AddStretchSpacer()
        btns.Add(self.run_btn, 0, wx.RIGHT, 6)
        btns.Add(self.bom_btn, 0, wx.RIGHT, 6)
        btns.Add(close_btn, 0)
        outer.Add(btns, 0, wx.EXPAND | wx.ALL, 8)

        panel.SetSizer(outer)
        self.run_btn.Bind(wx.EVT_BUTTON, self.on_run)
        self.bom_btn.Bind(wx.EVT_BUTTON, self.on_bom)
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

    def _recolor_layers(self) -> None:
        """Make Eco1.User white@30% and Eco2.User cyan@30% if the theme allows."""
        mapping = {s.theme_key: s.rgba for s in highlight.DEFAULT_STYLES.values()}
        theme = kicad_theme.find_color_theme()
        if theme is None:
            self.log(
                "\nColours: couldn't auto-set them (built-in/locked theme). In the "
                "Appearance panel set 'Eco1.User' to white and 'Eco2.User' to cyan "
                "at ~30% opacity to match."
            )
            return
        try:
            self._theme_prev = kicad_theme.apply_highlight_colors(theme, mapping)
            self._theme_path = theme
            self.log(
                f"\nColours: set Eco1.User=white@30%, Eco2.User=cyan@30% in {theme.name}. "
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
            try:
                pcbnew.FocusOnItem(self._flag_fps[idx])
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
                dry_run=dry, log=self.log,
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
                    on_event=lambda r: self._log_event(r),
                )
                self.log("\n" + summarize(report))

        self.log("\nDone." + ("  (dry run — nothing written)" if dry else ""))

    def _log_event(self, r: "PartResult") -> None:
        glyph = {"linked": "+", "already": "=", "not-found": "?", "no-key": "-"}.get(r.status, " ")
        if r.status in ("linked", "not-found"):
            self.log(f"  [{glyph}] {r.part.kind:8} {r.part.name:24} {r.url or r.query or ''}")
