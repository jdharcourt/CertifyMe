"""CertifyMe KiCad plugin entry point.

KiCad imports this package on start-up (when placed in the plugins directory)
and expects any ActionPlugin instances to register themselves here.
"""

from __future__ import annotations

from .action_certifyme import CertifyMeDatasheetPlugin

CertifyMeDatasheetPlugin().register()
