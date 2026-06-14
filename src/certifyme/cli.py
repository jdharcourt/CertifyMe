"""Command-line entry point: ``certifyme`` / ``python -m certifyme``.

Subcommands:

    certifyme setup            interactive wizard to store your DigiKey API keys
    certifyme status           show where keys are loaded from (masked)
    certifyme link <project>   scan a project and link datasheets
    certifyme bom <project>    generate a priced Excel/CSV BOM
    certifyme verify <project> cross-check each BOM part's specs against DigiKey

``certifyme <project> ...`` (no subcommand) is accepted as shorthand for ``link``.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

from . import bom as bom_mod
from . import config
from . import verify as verify_mod
from .linker import PartResult, link_project, summarize
from .providers import build_provider

_STATUS_GLYPH = {
    "linked": "+",
    "linked-generic": "!",
    "already": "=",
    "not-found": "?",
    "no-key": "-",
}

_SUBCOMMANDS = {"setup", "status", "link", "bom", "verify"}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="certifyme",
        description="Scan KiCad libraries/schematics and link each part to its "
        "datasheet via the DigiKey API.",
    )
    sub = p.add_subparsers(dest="command")

    # setup
    sp = sub.add_parser("setup", help="Store your DigiKey API keys (interactive).")
    sp.add_argument(
        "--project",
        type=Path,
        help="Save keys to this project's .env instead of the global config.",
    )
    sp.add_argument("--client-id", help="Provide the Client ID non-interactively.")
    sp.add_argument("--client-secret", help="Provide the Client Secret non-interactively.")
    sp.add_argument("--sandbox", action="store_true", help="Use the DigiKey sandbox host.")
    sp.add_argument("--no-test", action="store_true", help="Skip the live API test.")
    sp.set_defaults(func=cmd_setup)

    # status
    st = sub.add_parser("status", help="Show resolved credentials (masked).")
    st.add_argument("project", nargs="?", type=Path, help="Project folder to include its .env.")
    st.set_defaults(func=cmd_status)

    # link
    lk = sub.add_parser("link", help="Scan a project and link datasheets.")
    _add_link_args(lk)
    lk.set_defaults(func=cmd_link)

    # bom
    bm = sub.add_parser("bom", help="Generate a priced Excel BOM.")
    _add_bom_args(bm)
    bm.set_defaults(func=cmd_bom)

    # verify
    vf = sub.add_parser(
        "verify",
        help="Cross-check each BOM part's MPN/value/package against DigiKey.",
    )
    vf.add_argument("project", type=Path, help="KiCad project directory or a .kicad_sch/.kicad_pcb file.")
    vf.add_argument("--provider", default="digikey", help="'digikey' (default) or 'dummy'.")
    vf.add_argument("--dummy-map", type=Path, help="With --provider dummy: JSON {query: fields}.")
    vf.add_argument("--include-dnp", action="store_true", help="Also verify DNP parts.")
    vf.add_argument("-v", "--verbose", action="store_true", help="Show every check, not just problems.")
    vf.set_defaults(func=cmd_verify)

    return p


def _add_link_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "project",
        type=Path,
        help="KiCad project directory (scanned recursively) or a single file.",
    )
    p.add_argument("--provider", default="digikey", help="'digikey' (default) or 'dummy'.")
    p.add_argument("--dummy-map", type=Path, help="With --provider dummy: JSON {query: url}.")
    p.add_argument("--dry-run", action="store_true", help="Resolve and report, write nothing.")
    p.add_argument("--overwrite", action="store_true", help="Replace existing Datasheet values.")
    p.add_argument("--field", dest="prefer_field", help="Property to use as the search key (e.g. MPN).")
    p.add_argument(
        "--guess-datasheets",
        action="store_true",
        help="For parts DigiKey can't match, write a representative same-type "
        "datasheet (e.g. '10k resistor 0805'); these are approximate.",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="Print one line per part.")


def _add_bom_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("project", type=Path, help="KiCad project directory or a .kicad_sch/.kicad_pcb file.")
    p.add_argument("-o", "--output", type=Path, help="Output .xlsx path (default: <project>-BOM.xlsx).")
    p.add_argument("--csv", action="store_true", help="Also write a .csv alongside the .xlsx.")
    p.add_argument(
        "--open",
        dest="open_after",
        action="store_true",
        help="Open the BOM after writing it (Windows shows an app chooser with "
        "Always / Just once).",
    )
    p.add_argument("--provider", default="digikey", help="'digikey' (default) or 'dummy'.")
    p.add_argument("--dummy-map", type=Path, help="With --provider dummy: JSON {query: url|fields}.")
    p.add_argument("--currency", default="USD", help="Currency label for the BOM (default: USD).")
    p.add_argument(
        "--guess-datasheets",
        action="store_true",
        help="For parts DigiKey can't match, find a representative part of the same "
        "type (e.g. '10k resistor 0805') and use its datasheet, marked generic.",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="Print one line per BOM entry.")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Shorthand: `certifyme <path> ...` -> `certifyme link <path> ...`
    if argv and argv[0] not in _SUBCOMMANDS and not argv[0].startswith("-"):
        argv = ["link"] + argv

    args = build_parser().parse_args(argv)
    if not getattr(args, "command", None):
        build_parser().print_help()
        return 1
    return args.func(args)


# -- setup ------------------------------------------------------------------

def cmd_setup(args) -> int:
    scope = "project" if args.project else "global"
    project_dir = args.project
    current = config.resolve(project_dir)

    print("CertifyMe - DigiKey API setup")
    print("-----------------------------")
    print("Create a free app at https://developer.digikey.com/ to get a")
    print("Client ID and Client Secret (OAuth, 'Production' or 'Sandbox').\n")
    if current["configured"]:
        print(f"Current Client ID : {config.mask(current['client_id'])} "
              f"(from {current['id_source']})")
        print(f"Current Secret    : {config.mask(current['client_secret'])}\n")

    client_id = args.client_id
    if not client_id:
        prompt = f"Client ID [{current['client_id'] or 'none'}]: "
        client_id = input(prompt).strip() or current["client_id"]
    if not client_id:
        print("error: a Client ID is required.", file=sys.stderr)
        return 2

    client_secret = args.client_secret
    if not client_secret:
        client_secret = getpass.getpass(
            "Client Secret (hidden; blank = keep current): "
        ).strip() or current["client_secret"]
    if not client_secret:
        print("error: a Client Secret is required.", file=sys.stderr)
        return 2

    sandbox = args.sandbox or current["sandbox"]

    path = config.save_credentials(
        client_id, client_secret, sandbox=sandbox, scope=scope, project_dir=project_dir
    )
    print(f"\nSaved credentials to: {path}")
    if scope == "global":
        print("These will be used for every project automatically.")
    else:
        print("These apply to this project (added to its .env, which is gitignored).")

    if args.no_test:
        return 0
    return _test_connection(project_dir)


def _test_connection(project_dir=None) -> int:
    print("\nTesting DigiKey connection...")
    config.load_into_env(project_dir)
    try:
        provider = build_provider("digikey")
        url = provider.find_datasheet("STM32F103C8T6")  # a well-known part
    except Exception as exc:
        print(f"  Test failed: {exc}", file=sys.stderr)
        print("  Double-check the keys and that the app type matches --sandbox.")
        return 1
    if url:
        print(f"  Success! Example lookup returned:\n    {url}")
    else:
        print("  Connected, but no datasheet returned for the test part "
              "(keys look OK).")
    return 0


# -- status -----------------------------------------------------------------

def cmd_status(args) -> int:
    project_dir = args.project
    info = config.resolve(project_dir)
    print("CertifyMe credential status")
    print("---------------------------")
    print(f"Global config : {config.global_config_path()}")
    if project_dir:
        print(f"Project .env  : {config.project_env_path(project_dir)}")
    print(f"Client ID     : {config.mask(info['client_id'])}  (from {info['id_source']})")
    print(f"Client Secret : {config.mask(info['client_secret'])}  (from {info['secret_source']})")
    print(f"Sandbox       : {'yes' if info['sandbox'] else 'no'}")
    print(f"Configured    : {'yes' if info['configured'] else 'NO - run: certifyme setup'}")
    return 0 if info["configured"] else 1


# -- link -------------------------------------------------------------------

def cmd_link(args) -> int:
    if not args.project.exists():
        print(f"error: path not found: {args.project}", file=sys.stderr)
        return 2

    project_dir = args.project if args.project.is_dir() else args.project.parent
    config.load_into_env(project_dir)

    provider_kwargs = {}
    if args.provider == "dummy":
        mapping = {}
        if args.dummy_map and args.dummy_map.exists():
            mapping = json.loads(args.dummy_map.read_text(encoding="utf-8"))
        provider_kwargs["mapping"] = mapping

    try:
        provider = build_provider(args.provider, **provider_kwargs)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        if args.provider == "digikey":
            print("\nNo API keys found. Run:  certifyme setup", file=sys.stderr)
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
        guess_datasheets=args.guess_datasheets,
        on_event=on_event,
    )

    if args.dry_run:
        print("\n[dry run -- no files written]")
    print("\n" + summarize(report))
    return 0


# -- bom --------------------------------------------------------------------

def cmd_bom(args) -> int:
    if not args.project.exists():
        print(f"error: path not found: {args.project}", file=sys.stderr)
        return 2

    project_dir = args.project if args.project.is_dir() else args.project.parent
    config.load_into_env(project_dir)

    provider_kwargs = {}
    if args.provider == "dummy":
        mapping = {}
        if args.dummy_map and args.dummy_map.exists():
            mapping = json.loads(args.dummy_map.read_text(encoding="utf-8"))
        provider_kwargs["mapping"] = mapping

    try:
        provider = build_provider(args.provider, **provider_kwargs)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        if args.provider == "digikey":
            print("\nNo API keys found. Run:  certifyme setup", file=sys.stderr)
        return 2

    def on_event(line) -> None:
        if not args.verbose:
            return
        price = f"{line.unit_price:.4f}" if line.unit_price is not None else "    -   "
        print(f"  {line.quantity:>3}x  {line.value:16} {line.mpn:18} {price}  [{line.refs_text}]")

    bom = bom_mod.build_bom(
        args.project,
        provider,
        currency=args.currency,
        guess_datasheets=args.guess_datasheets,
        on_event=on_event,
    )

    if not bom.lines:
        print("No components found. Point at a project with a .kicad_sch (or a "
              ".kicad_pcb) containing placed parts.", file=sys.stderr)
        return 1

    out = args.output or (project_dir / f"{bom.project_name}-BOM.xlsx")
    bom_mod.write_xlsx_bom(bom, out)
    written = [out]
    if args.csv:
        csv_path = out.with_suffix(".csv")
        bom_mod.write_csv_bom(bom, csv_path)
        written.append(csv_path)

    print("\n" + bom_mod.summarize(bom))
    print("\nWrote:")
    for w in written:
        print(f"  {w}")

    if getattr(args, "open_after", False):
        from .open_file import open_file
        if not open_file(out, choose=True):
            print("\nwarning: could not open the BOM automatically.", file=sys.stderr)
    return 0


# -- verify -----------------------------------------------------------------

def cmd_verify(args) -> int:
    if not args.project.exists():
        print(f"error: path not found: {args.project}", file=sys.stderr)
        return 2

    project_dir = args.project if args.project.is_dir() else args.project.parent
    config.load_into_env(project_dir)

    provider_kwargs = {}
    if args.provider == "dummy":
        mapping = {}
        if args.dummy_map and args.dummy_map.exists():
            mapping = json.loads(args.dummy_map.read_text(encoding="utf-8"))
        provider_kwargs["mapping"] = mapping

    try:
        provider = build_provider(args.provider, **provider_kwargs)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        if args.provider == "digikey":
            print("\nNo API keys found. Run:  certifyme setup", file=sys.stderr)
        return 2

    bom = bom_mod.build_bom(args.project, provider)
    if not bom.lines:
        print("No components found to verify.", file=sys.stderr)
        return 1

    verdicts = verify_mod.verify_bom(bom, include_dnp=args.include_dnp)
    _badge = {
        verify_mod.V_OK: "OK  ",
        verify_mod.V_WARN: "WARN",
        verify_mod.V_FAIL: "FAIL",
        verify_mod.V_NO_MATCH: "MISS",
    }
    for v in verdicts:
        if v.status == verify_mod.V_OK and not args.verbose:
            continue
        print(f"[{_badge.get(v.status, '?')}] {v.refs_text:14} {v.value:14} {v.mpn}")
        details = v.checks if args.verbose else [
            c for c in v.checks if c.status in (verify_mod.MISMATCH, verify_mod.MISSING)
        ]
        for c in details:
            print(f"        - {c.name}: {c.status} ({c.detail})")

    print("\n" + verify_mod.summarize(verdicts))
    # Non-zero exit if any part's board data contradicts DigiKey.
    return 1 if verify_mod.counts(verdicts)[verify_mod.V_FAIL] else 0


def _safe_relpath(file: Path, project: Path) -> str:
    try:
        base = project if project.is_dir() else project.parent
        return str(file.relative_to(base))
    except ValueError:
        return file.name


if __name__ == "__main__":
    raise SystemExit(main())
