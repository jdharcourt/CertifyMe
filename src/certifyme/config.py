"""Credential storage and resolution for CertifyMe.

Keys can live in three places, checked in this precedence (highest first):

1. **Environment variables** — ``DIGIKEY_CLIENT_ID`` etc. (good for CI).
2. **Project ``.env``** — a file in the KiCad project folder (per-project keys).
3. **Global config** — a single file in the user's config dir, written by
   ``certifyme setup`` or the plugin's *Save credentials* button. Set once, used
   everywhere.

The file format is the familiar ``KEY=VALUE`` ``.env`` style, so anything
already written by hand keeps working.
"""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "CertifyMe"
CONFIG_FILENAME = "credentials.env"

CRED_KEYS = (
    "DIGIKEY_CLIENT_ID",
    "DIGIKEY_CLIENT_SECRET",
    "DIGIKEY_SANDBOX",
    "DIGIKEY_LOCALE_SITE",
    "DIGIKEY_LOCALE_LANGUAGE",
    "DIGIKEY_LOCALE_CURRENCY",
)


def global_config_dir() -> Path:
    """Per-user config directory (``%APPDATA%\\CertifyMe`` on Windows,
    ``$XDG_CONFIG_HOME/certifyme`` or ``~/.config/certifyme`` elsewhere)."""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_NAME
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "certifyme"


def global_config_path() -> Path:
    return global_config_dir() / CONFIG_FILENAME


def project_env_path(project_dir) -> Path:
    return Path(project_dir) / ".env"


def parse_env_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not Path(path).exists():
        return data
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        data[key.strip()] = value.strip().strip('"').strip("'")
    return data


def load_into_env(project_dir=None) -> None:
    """Populate ``os.environ`` from the global config and project ``.env`` without
    clobbering variables already set in the real environment."""
    merged: dict[str, str] = {}
    merged.update(parse_env_file(global_config_path()))
    if project_dir:
        merged.update(parse_env_file(project_env_path(project_dir)))
    for key, value in merged.items():
        os.environ.setdefault(key, value)  # real env vars win


def save_credentials(
    client_id: str,
    client_secret: str,
    *,
    sandbox: bool = False,
    scope: str = "global",
    project_dir=None,
) -> Path:
    """Write credentials to the global config (default) or a project ``.env``.

    Returns the path written. Existing non-credential lines are preserved.
    """
    if scope == "project":
        if not project_dir:
            raise ValueError("project_dir is required when scope='project'")
        path = project_env_path(project_dir)
    elif scope == "global":
        path = global_config_path()
    else:
        raise ValueError(f"unknown scope: {scope!r}")

    existing = parse_env_file(path)
    existing["DIGIKEY_CLIENT_ID"] = client_id
    existing["DIGIKEY_CLIENT_SECRET"] = client_secret
    if sandbox:
        existing["DIGIKEY_SANDBOX"] = "1"
    else:
        existing.pop("DIGIKEY_SANDBOX", None)

    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# CertifyMe DigiKey API credentials",
        "# Written by `certifyme setup` / the KiCad plugin. Keep this private.",
        "",
    ]
    for key in CRED_KEYS:
        if key in existing and existing[key] != "":
            lines.append(f"{key}={existing[key]}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # Best-effort lock-down of the secret file on POSIX.
    try:
        if os.name != "nt":
            os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def resolve(project_dir=None) -> dict:
    """Resolve the effective credentials and report where each came from."""
    g = parse_env_file(global_config_path())
    p = parse_env_file(project_env_path(project_dir)) if project_dir else {}

    def pick(key: str) -> tuple[str, str]:
        if os.environ.get(key):
            return os.environ[key], "environment"
        if p.get(key):
            return p[key], "project .env"
        if g.get(key):
            return g[key], "global config"
        return "", "unset"

    client_id, id_src = pick("DIGIKEY_CLIENT_ID")
    secret, secret_src = pick("DIGIKEY_CLIENT_SECRET")
    sandbox, _ = pick("DIGIKEY_SANDBOX")
    return {
        "client_id": client_id,
        "client_secret": secret,
        "sandbox": sandbox.lower() in ("1", "true", "yes"),
        "id_source": id_src,
        "secret_source": secret_src,
        "configured": bool(client_id and secret),
    }


def mask(secret: str) -> str:
    """Render a secret safely for display, e.g. ``ab****wxyz``."""
    if not secret:
        return "(not set)"
    if len(secret) <= 6:
        return "*" * len(secret)
    return f"{secret[:2]}{'*' * (len(secret) - 6)}{secret[-4:]}"
