"""Command-line entry point: ``certifyme`` / ``python -m certifyme``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .linker import PartResult, link_project, summarize
from .providers import build_provider

_STATUS_GLYPH = {
    "linked": "+",
    "already": "=",
    "not-found": "?",
    "no-key": "-",
}


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader so credentials don't have to be exported manually."""
    if not path.exists():
        return
    import os

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="certifyme",
        description="Scan KiCad libraries/schematics and link each part to its "
        "datasheet via the DigiKey API.",
    )
    p.add_argument(
        "project",
        type=Path,
        help="KiCad project directory (scanned recursively) or a single file.",
    )
    p.add_argument(
        "--provider",
        default="digikey",
        help="Datasheet provider: 'digikey' (default) or 'dummy'.",
    )
    p.add_argument(
        "--dummy-map",
        type=Path,
        help="With --provider dummy: a JSON file of {query: url} for offline use.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve datasheets and report, but do not modify any files.",
    )
    p.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace existing Datasheet values (default: leave them untouched).",
    )
    p.add_argument(
        "--field",
        dest="prefer_field",
        help="Property name to use as the search key (e.g. MPN). "
        "Falls back to common MPN fields, then Value, then the part name.",
    )
    p.add_argument(
        "--env",
        type=Path,
        default=Path(".env"),
        help="Path to a .env file with DigiKey credentials (default: ./.env).",
    )
    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print one line per part as it is processed.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _load_dotenv(args.env)

    if not args.project.exists():
        print(f"error: path not found: {args.project}", file=sys.stderr)
        return 2

    provider_kwargs = {}
    if args.provider == "dummy":
        mapping = {}
        if args.dummy_map and args.dummy_map.exists():
            mapping = json.loads(args.dummy_map.read_text(encoding="utf-8"))
        provider_kwargs["mapping"] = mapping

    try:
        provider = build_provider(args.provider, **provider_kwargs)
    except Exception as exc:  # credential / config errors are user-facing
        print(f"error: {exc}", file=sys.stderr)
        return 2

    def on_event(r: PartResult) -> None:
        if not args.verbose:
            return
        glyph = _STATUS_GLYPH.get(r.status, " ")
        detail = r.url or r.query or ""
        rel = _safe_relpath(r.part.file, args.project)
        print(f"  [{glyph}] {r.part.kind:8} {r.part.name:24} {detail}  ({rel})")

    report = link_project(
        args.project,
        provider,
        dry_run=args.dry_run,
        overwrite=args.overwrite,
        prefer_field=args.prefer_field,
        on_event=on_event,
    )

    if args.dry_run:
        print("\n[dry run -- no files written]")
    print("\n" + summarize(report))
    return 0


def _safe_relpath(file: Path, project: Path) -> str:
    try:
        base = project if project.is_dir() else project.parent
        return str(file.relative_to(base))
    except ValueError:
        return file.name


if __name__ == "__main__":
    raise SystemExit(main())
