"""Logging setup: a rotating log file in the app folder (+ stderr when there is a console)."""

from __future__ import annotations

import logging
import logging.handlers
import sys

from .paths import logs_dir

LOGGER_NAME = "studia"
_FORMAT = "%(asctime)s %(levelname)-7s [%(threadName)s] %(message)s"


def setup_logging(debug: bool = False) -> logging.Logger:
    log = logging.getLogger(LOGGER_NAME)
    if getattr(log, "_lectures_configured", False):
        return log
    log.setLevel(logging.DEBUG if debug else logging.INFO)
    log.propagate = False
    fh = logging.handlers.RotatingFileHandler(logs_dir() / "studia.log", maxBytes=1_000_000,
                                              backupCount=5, encoding="utf-8")
    fh.setFormatter(logging.Formatter(_FORMAT))
    log.addHandler(fh)
    if sys.stderr is not None:  # pythonw.exe has no console
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        sh.setLevel(logging.DEBUG if debug else logging.WARNING)
        log.addHandler(sh)
    for noisy in ("urllib3", "faster_whisper", "huggingface_hub", "pywebview"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    log._lectures_configured = True  # type: ignore[attr-defined]
    return log
