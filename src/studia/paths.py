"""Filesystem locations used by Studia.

Everything lives under ``%LOCALAPPDATA%\\Studia`` unless the ``STUDIA_HOME``
environment variable points somewhere else (tests and portable setups use that).
Every helper creates its directory on first use, so callers never need to ``mkdir``.

The app used to be called LectureScribe: :func:`migrate_legacy_data` moves its data folder once.
"""

from __future__ import annotations

import ctypes
import functools
import os
import shutil
import uuid
from pathlib import Path

APP_NAME = "Studia"            # the data folder's name (kept from before the app was called IEUStudia)
OUTPUT_NAME = "IEUStudia"      # new users' output folder in Documents (existing users keep theirs)
LEGACY_NAME = "LectureScribe"
HOME_ENV = "STUDIA_HOME"

# Known-folder id of the user's Documents folder (follows OneDrive / folder redirection).
_FOLDERID_DOCUMENTS = uuid.UUID("FDD39AD0-238F-46AF-ADB4-6C85480369C7")


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _home_override() -> Path | None:
    value = os.environ.get(HOME_ENV, "").strip()
    return Path(value).expanduser() if value else None


def _local_root() -> Path:
    local = os.environ.get("LOCALAPPDATA", "").strip()
    return Path(local) if local else Path.home() / "AppData" / "Local"


def migrate_legacy_data() -> str:
    """Move ``%LOCALAPPDATA%\\LectureScribe`` to ``%LOCALAPPDATA%\\Studia`` (once, before anything uses it).

    Same drive, so it is a rename. If a new folder already exists without a library (e.g. only logs),
    the old folder's entries are moved into it. Returns "moved", "none", "skipped" or "failed: …"."""
    if _home_override() is not None:
        return "skipped"
    new, old = _local_root() / APP_NAME, _local_root() / LEGACY_NAME
    if not (old / "library.db").exists() or (new / "library.db").exists():
        return "none"
    try:
        if not new.exists():
            os.replace(old, new)
        else:
            for child in old.iterdir():
                if not (new / child.name).exists():
                    shutil.move(str(child), str(new / child.name))
            if not any(old.iterdir()):
                old.rmdir()
        _legacy_in_use.cache_clear()
        return "moved"
    except OSError as exc:            # e.g. the old app is still running: keep using its folder for now
        return f"failed: {exc}"


@functools.lru_cache(maxsize=1)
def _legacy_in_use() -> bool:
    root = _local_root()
    return not (root / APP_NAME / "library.db").exists() and (root / LEGACY_NAME / "library.db").exists()


def app_dir() -> Path:
    """``%LOCALAPPDATA%\\Studia`` (or ``$STUDIA_HOME``; the old LectureScribe folder until it is moved)."""
    override = _home_override()
    if override is not None:
        return _ensure_dir(override)
    return _ensure_dir(_local_root() / (LEGACY_NAME if _legacy_in_use() else APP_NAME))


# Each IEULearn account ("profile") has its own library, transcripts, settings and stored password. The first one
# uses the app folder itself; added ones live in profiles\<id> (see accounts.py). Models, logs and caches are shared.
_profile_rel = ""


def set_profile_dir(rel: str = "") -> None:
    """Make the data helpers below point at an account's folder (relative to the app folder; "" = the app folder)."""
    global _profile_rel
    _profile_rel = (rel or "").replace("\\", "/").strip("/")


def profile_dir() -> Path:
    """The active account's data folder."""
    return _ensure_dir(app_dir() / _profile_rel) if _profile_rel else app_dir()


def settings_path() -> Path:
    return profile_dir() / "settings.json"


def secrets_path() -> Path:
    return profile_dir() / "secrets.bin"


def db_path() -> Path:
    return profile_dir() / "library.db"


def transcripts_dir() -> Path:
    """Canonical ``<delivery_id>.json`` transcript per recording."""
    return _ensure_dir(profile_dir() / "transcripts")


def audio_cache_dir() -> Path:
    return _ensure_dir(app_dir() / "cache" / "audio")


def logs_dir() -> Path:
    return _ensure_dir(app_dir() / "logs")


@functools.lru_cache(maxsize=1)
def documents_dir() -> Path:
    """The user's real Documents folder (honours OneDrive/known-folder redirection)."""
    try:
        guid = ctypes.create_string_buffer(_FOLDERID_DOCUMENTS.bytes_le, 16)
        out = ctypes.c_void_p()
        shell32 = ctypes.WinDLL("shell32")
        ole32 = ctypes.WinDLL("ole32")
        hr = shell32.SHGetKnownFolderPath(guid, 0, None, ctypes.byref(out))
        try:
            if hr == 0 and out.value:
                return Path(ctypes.wstring_at(out.value))
        finally:
            if out.value:
                ole32.CoTaskMemFree(out)
    except (AttributeError, OSError):  # not on Windows / API unavailable
        pass
    return Path.home() / "Documents"


def default_output_path() -> Path:
    """:func:`default_output_dir` without creating it."""
    if _home_override() is not None:
        return profile_dir() / "output"
    if _profile_rel:
        return documents_dir() / f"{OUTPUT_NAME} ({Path(_profile_rel).name})"
    return documents_dir() / OUTPUT_NAME


def default_output_dir() -> Path:
    """Where Markdown transcripts go by default: ``Documents\\IEUStudia`` (an added profile gets its own folder when it
    is created; this is only the fallback).

    When ``STUDIA_HOME`` is set (tests, portable mode) the output goes to
    ``<home>\\output`` (``<profile folder>\\output``) instead, so test runs never touch the real Documents folder.
    """
    return _ensure_dir(default_output_path())


def legacy_output_dir() -> Path:
    """The default output folder before the app became Studia (``Documents\\LectureScribe``)."""
    return documents_dir() / LEGACY_NAME
