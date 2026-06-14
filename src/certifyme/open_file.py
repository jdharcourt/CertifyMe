"""Open an exported file in an external application.

The goal is the familiar OS "open this file" experience after a BOM is written.
On Windows, the shell ``openas`` verb raises the standard *"How do you want to
open this file?"* dialog, which offers the **Always** / **Just once** choices and
remembers the user's pick for next time. On macOS and Linux we hand the file to
the platform's default opener (``open`` / ``xdg-open``).

Kept dependency-free so it works inside KiCad's bundled Python.
"""

from __future__ import annotations

import os
import subprocess
import sys


def open_file(path, *, choose: bool = False) -> bool:
    """Open *path* in an external application.

    When *choose* is true on Windows, show the "How do you want to open this
    file?" chooser (Always / Just once); otherwise open with the program already
    associated with the file type. On other platforms *choose* is ignored and the
    default opener is used. Returns ``True`` if the open was launched.
    """
    path = os.fspath(path)
    try:
        if sys.platform.startswith("win"):
            # "openas" -> chooser dialog with Always / Just once; "open" -> default app.
            os.startfile(path, "openas" if choose else "open")  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            # Absolute path: KiCad's bundled Python may launch with a minimal
            # PATH where a bare "open" isn't resolvable.
            subprocess.Popen(["/usr/bin/open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return True
    except OSError:
        # The chooser verb can fail when no handler is registered; fall back to
        # a plain default-open so the user still gets the file.
        if choose and sys.platform.startswith("win"):
            try:
                os.startfile(path)  # type: ignore[attr-defined]
                return True
            except OSError:
                return False
        return False
