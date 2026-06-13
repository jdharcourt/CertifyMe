"""Orchestration: scan a project, resolve datasheets, write them back."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .kicad import Part, discover_files, scan_file, write_datasheets
from .providers.base import DatasheetProvider


@dataclass
class PartResult:
    part: Part
    query: str | None
    url: str | None
    status: str  # "linked" | "already" | "not-found" | "no-key"


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
    on_event=None,
) -> LinkReport:
    """Scan *project*, look up a datasheet for each part, and write the URLs.

    * ``overwrite=False`` (default) leaves parts that already have a datasheet.
    * ``dry_run=True`` resolves everything but writes nothing.
    * ``on_event`` is an optional callback(PartResult) for progress reporting.
    """
    report = LinkReport()
    files = discover_files(project)

    for file in files:
        parts = scan_file(file)
        pending: list[tuple[Part, str]] = []

        for part in parts:
            result = _resolve_part(part, provider, overwrite, prefer_field)
            report.results.append(result)
            if on_event:
                on_event(result)
            if result.status == "linked" and result.url:
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
) -> PartResult:
    if part.current_datasheet and not overwrite:
        return PartResult(part, None, part.current_datasheet, "already")

    query = part.search_key(prefer_field)
    if not query:
        return PartResult(part, None, None, "no-key")

    url = provider.find_datasheet(query)
    if not url:
        return PartResult(part, query, None, "not-found")
    if url == part.current_datasheet:
        return PartResult(part, query, url, "already")
    return PartResult(part, query, url, "linked")


def summarize(report: LinkReport) -> str:
    by_status = defaultdict(int)
    for r in report.results:
        by_status[r.status] += 1
    lines = [
        f"Parts scanned : {report.total}",
        f"Linked        : {by_status['linked']}",
        f"Already linked: {by_status['already']}",
        f"Not found     : {by_status['not-found']}",
        f"No search key : {by_status['no-key']}",
        f"Files changed : {len(report.files_changed)}",
    ]
    return "\n".join(lines)
