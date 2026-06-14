"""Orchestration: scan a project, resolve datasheets, write them back."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from . import parttype
from .kicad import Part, discover_files, scan_file, write_datasheets
from .providers.base import DatasheetProvider


@dataclass
class PartResult:
    part: Part
    query: str | None
    url: str | None
    status: str  # "linked" | "linked-generic" | "already" | "not-found" | "no-key"

    @property
    def approximate(self) -> bool:
        return self.status == "linked-generic"


@dataclass
class LinkReport:
    results: list[PartResult] = field(default_factory=list)
    files_changed: list[Path] = field(default_factory=list)

    def count(self, status: str) -> int:
        return sum(1 for r in self.results if r.status == status)

    @property
    def total(self) -> int:
        return len(self.results)


def link_project(
    project: Path,
    provider: DatasheetProvider,
    *,
    dry_run: bool = False,
    overwrite: bool = False,
    prefer_field: str | None = None,
    guess_datasheets: bool = False,
    on_event=None,
) -> LinkReport:
    """Scan *project*, look up a datasheet for each part, and write the URLs.

    * ``overwrite=False`` (default) leaves parts that already have a datasheet.
    * ``dry_run=True`` resolves everything but writes nothing.
    * ``guess_datasheets=True`` falls back to a generic, same-type datasheet for
      parts the provider can't match exactly (status ``linked-generic``).
    * ``on_event`` is an optional callback(PartResult) for progress reporting.
    """
    report = LinkReport()
    files = discover_files(project)

    for file in files:
        parts = scan_file(file)
        pending: list[tuple[Part, str]] = []

        for part in parts:
            result = _resolve_part(part, provider, overwrite, prefer_field, guess_datasheets)
            report.results.append(result)
            if on_event:
                on_event(result)
            if result.status in ("linked", "linked-generic") and result.url:
                pending.append((part, result.url))

        if pending and not dry_run:
            if write_datasheets(file, pending):
                report.files_changed.append(file)

    return report


def _resolve_part(
    part: Part,
    provider: DatasheetProvider,
    overwrite: bool,
    prefer_field: str | None,
    guess_datasheets: bool = False,
) -> PartResult:
    if part.current_datasheet and not overwrite:
        return PartResult(part, None, part.current_datasheet, "already")

    query = part.search_key(prefer_field)
    if not query:
        return PartResult(part, None, None, "no-key")

    url = provider.find_datasheet(query)
    if not url:
        if guess_datasheets:
            generic = _generic_datasheet(part, provider)
            if generic:
                gq, gurl = generic
                if gurl != part.current_datasheet:
                    return PartResult(part, gq, gurl, "linked-generic")
        return PartResult(part, query, None, "not-found")
    if url == part.current_datasheet:
        return PartResult(part, query, url, "already")
    return PartResult(part, query, url, "linked")


def _generic_datasheet(part: Part, provider: DatasheetProvider) -> tuple[str, str] | None:
    """Best-guess datasheet for an unfound part from its type + package, e.g.
    ``10k resistor 0805``. Returns ``(query, url)`` or None."""
    props = part.properties
    gq = parttype.generic_query_from(
        props.get("Reference", ""), props.get("Value", ""), props.get("Footprint", "")
    )
    if not gq:
        return None
    url = provider.find_datasheet(gq)
    return (gq, url) if url else None


def summarize(report: LinkReport) -> str:
    by_status = defaultdict(int)
    for r in report.results:
        by_status[r.status] += 1
    lines = [
        f"Parts scanned : {report.total}",
        f"Linked        : {by_status['linked']}",
    ]
    if by_status["linked-generic"]:
        lines.append(f"Linked generic: {by_status['linked-generic']}  (approximate - verify before use)")
    lines += [
        f"Already linked: {by_status['already']}",
        f"Not found     : {by_status['not-found']}",
        f"No search key : {by_status['no-key']}",
        f"Files changed : {len(report.files_changed)}",
    ]
    return "\n".join(lines)
