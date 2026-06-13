"""CertifyMe — link KiCad parts to their datasheets automatically."""

from __future__ import annotations

from .linker import LinkReport, PartResult, link_project, summarize

__version__ = "0.1.0"
__all__ = ["link_project", "LinkReport", "PartResult", "summarize", "__version__"]
