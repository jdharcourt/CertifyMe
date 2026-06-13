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
    from .certifyme import config
    from .certifyme.linker import PartResult, link_project, summarize
    from .certifyme.providers import build_provider
except ImportError:  # dev checkout: engine lives in ../src
    _src = os.path.join(os.path.dirname(__file__), "..", "src")
    if _src not in sys.path:
        sys.path.insert(0, _src)
    from certifyme import config  # type: ignore
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
        self.save_btn.Bind(wx.EVT_BUTTON, self.on_save_creds)
        self.test_btn.Bind(wx.EVT_BUTTON, self.on_test_creds)

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
