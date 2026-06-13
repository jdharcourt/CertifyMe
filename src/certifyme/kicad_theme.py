"""Best-effort editing of KiCad's board colour theme.

To render the highlight layers as *translucent white* and *translucent cyan*
we set those user-layer colours (with an alpha component) in KiCad's active
colour theme JSON. KiCad stores per-layer colours like ``"eco1_user":
"rgba(255, 255, 255, 0.30)"``.

This only works when the active theme is an editable user theme (not a built-in
one). All edits are reversible: :func:`apply_highlight_colors` returns the
previous values, which :func:`restore_colors` puts back.

KiCad may need the board reopened (or a theme re-select) to pick up colour
changes; the plugin tells the user so.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def kicad_config_root() -> Path | None:
    """Locate KiCad's per-user config directory across platforms."""
    env = os.environ.get("KICAD_CONFIG_HOME")
    if env:
        return Path(env)
    if os.name == "nt":
        base = os.environ.get("APPDATA")
        if base:
            return Path(base) / "kicad"
    home = Path.home()
    mac = home / "Library" / "Preferences" / "kicad"
    if mac.exists():
        return mac
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else home / ".config") / "kicad"


def _version_dirs(root: Path) -> list[Path]:
    try:
        dirs = [p for p in root.iterdir() if p.is_dir() and p.name[:1].isdigit()]
    except OSError:
        return []
    return sorted(dirs, key=lambda p: p.name, reverse=True)


def find_color_theme(root: Path | None = None) -> Path | None:
    """Return the path of the active, editable board colour theme, or None.

    Built-in themes (names starting with ``_builtin``) cannot be edited; in that
    case we look for a ``user.json`` theme to fall back to.
    """
    root = root or kicad_config_root()
    if not root or not root.exists():
        return None
    for ver in _version_dirs(root):
        name = "user"
        pcbnew_json = ver / "pcbnew.json"
        if pcbnew_json.exists():
            try:
                data = json.loads(pcbnew_json.read_text(encoding="utf-8"))
                name = (data.get("appearance", {}) or {}).get("color_theme") or name
            except (OSError, ValueError):
                pass
        if name.startswith("_builtin"):
            fallback = ver / "colors" / "user.json"
            if fallback.exists():
                return fallback
            continue
        candidate = ver / "colors" / f"{name}.json"
        if candidate.exists():
            return candidate
    return None


def apply_highlight_colors(theme_path, mapping: dict[str, str]) -> dict[str, str | None]:
    """Set ``board.<key> = rgba`` for each entry in *mapping*.

    Returns the previous values (None where the key was absent) for restoring.
    """
    path = Path(theme_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    board = data.setdefault("board", {})
    previous: dict[str, str | None] = {}
    for key, value in mapping.items():
        previous[key] = board.get(key)
        board[key] = value
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return previous


def restore_colors(theme_path, previous: dict[str, str | None]) -> None:
    """Undo :func:`apply_highlight_colors` using its returned values."""
    path = Path(theme_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    board = data.setdefault("board", {})
    for key, value in previous.items():
        if value is None:
            board.pop(key, None)
        else:
            board[key] = value
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
