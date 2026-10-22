"""Cooperative cancellation shared by the downloader, audio decoder and engine."""

from __future__ import annotations

import threading
from typing import Optional


class Cancelled(Exception):
    """Raised when the user cancels a running operation."""

    def __init__(self, message: str = "Cancelled.") -> None:
        super().__init__(message)


def check(cancel: Optional[threading.Event]) -> None:
    """Raise :class:`Cancelled` if ``cancel`` is set."""
    if cancel is not None and cancel.is_set():
        raise Cancelled()
