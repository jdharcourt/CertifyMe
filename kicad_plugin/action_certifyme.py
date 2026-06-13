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

The engine is imported as a bundled subpackage when installed, or from the
repo's ``src/`` directory when run from a checkout.
"""

from __future__ import annotations

import os
import sys
import traceback

import pcbnew
import wx

# --- locate the CertifyMe engine ------------------------------------------
try:  # installed layout: certifyme/ sits next to this file
    from .certifyme.linker import PartResult, link_project, summarize
    from .certifyme.providers import build_provider
except ImportError:  # dev checkout: engine lives in ../src
    _src = os.path.join(os.path.dirname(__file__), "..", "src")
    if _src not in sys.path:
        sys.path.insert(0, _src)
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


class CertifyMeDialog(wx.Dialog):
    def __init__(self, project: str):
        super().__init__(None, title="CertifyMe — Link Datasheets", size=(640, 520))
        self.project = project
        panel = wx.Panel(self)
        outer = wx.BoxSizer(wx.VERTICAL)

        outer.Add(
            wx.StaticText(panel, label=f"Project: {project or '(unsaved board)'}"),
            0, wx.ALL, 8,
        )

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
            panel, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL, size=(-1, 220)
        )
        outer.Add(self.log_ctrl, 1, wx.EXPAND | wx.ALL, 8)

        # Buttons
        btns = wx.BoxSizer(wx.HORIZONTAL)
        self.run_btn = wx.Button(panel, label="Run")
        close_btn = wx.Button(panel, id=wx.ID_CANCEL, label="Close")
        btns.AddStretchSpacer()
        btns.Add(self.run_btn, 0, wx.RIGHT, 6)
        btns.Add(close_btn, 0)
        outer.Add(btns, 0, wx.EXPAND | wx.ALL, 8)

        panel.SetSizer(outer)
        self.run_btn.Bind(wx.EVT_BUTTON, self.on_run)

    def log(self, msg: str) -> None:
        self.log_ctrl.AppendText(msg + "\n")
        wx.GetApp().Yield()

    def on_run(self, _evt) -> None:
        self.log_ctrl.SetValue("")
        self.run_btn.Disable()
        try:
            self._do_run()
        except Exception:
            self.log("ERROR:\n" + traceback.format_exc())
        finally:
            self.run_btn.Enable()

    def _do_run(self) -> None:
        provider_name = self.provider_choice.GetStringSelection()
        dry = self.dry_run.GetValue()
        overwrite = self.overwrite.GetValue()
        prefer_field = self.field_ctrl.GetValue().strip() or None

        # Load .env from the project dir for DigiKey credentials.
        _load_dotenv(os.path.join(self.project, ".env"))

        try:
            provider = build_provider(provider_name)
        except Exception as exc:
            self.log(f"Cannot start provider '{provider_name}': {exc}")
            if provider_name == "digikey":
                self.log(
                    "Create a .env in the project folder with:\n"
                    "  DIGIKEY_CLIENT_ID=...\n  DIGIKEY_CLIENT_SECRET=...\n"
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
                    __import__("pathlib").Path(self.project),
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


def _load_dotenv(path: str) -> None:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
