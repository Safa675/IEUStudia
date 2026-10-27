"""The operating system's helpers: :mod:`winutil` (and :mod:`tray`) on Windows, :mod:`linuxutil` elsewhere.

``from .osutil import X`` gives the same function on every system; each module keeps its own implementation, so the
Windows code stays as it is. Names are looked up when they are imported (a function's own ``from .osutil import``
sees what the tests patched in the module behind it).
"""

from __future__ import annotations

import sys
from typing import Any

WINDOWS = sys.platform == "win32"
_TRAY = ("Tray", "Notifier")


def __getattr__(name: str) -> Any:
    if WINDOWS:
        if name in _TRAY:
            from . import tray

            return getattr(tray, name)
        from . import winutil

        return getattr(winutil, name)
    from . import linuxutil

    return getattr(linuxutil, name)


def impl() -> Any:
    """The module behind these names on this system (tests patch it)."""
    if WINDOWS:
        from . import winutil

        return winutil
    from . import linuxutil

    return linuxutil
