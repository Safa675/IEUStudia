"""Credentials + session cookies, encrypted for the current Windows user with DPAPI.

Only this Windows account on this PC can decrypt ``secrets.bin``; nothing is ever logged.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Optional

from .paths import secrets_path

log = logging.getLogger("studia")
_lock = threading.Lock()
_ENTROPY = b"LectureScribe/secrets/v1"
CRYPTPROTECT_UI_FORBIDDEN = 0x1


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> _Blob:
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _crypt(data: bytes, protect: bool) -> bytes:
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    src, ent, out = _blob(data), _blob(_ENTROPY), _Blob()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(src), None, ctypes.byref(ent), None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out))
    if not ok:
        raise OSError(ctypes.get_last_error(), "DPAPI operation failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def protect(data: bytes) -> bytes:
    return _crypt(data, True)


def unprotect(data: bytes) -> bytes:
    return _crypt(data, False)


def _file(folder: Optional[Path]) -> Path:
    """The active profile's secrets, or those of the profile in ``folder`` (while it is being added)."""
    return Path(folder) / "secrets.bin" if folder else secrets_path()


def _load(folder: Optional[Path] = None) -> dict[str, Any]:
    path = _file(folder)
    if not path.exists():
        return {}
    try:
        data = json.loads(unprotect(path.read_bytes()).decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        log.warning("Stored credentials could not be decrypted; they will be asked for again")
        return {}


def _save(data: dict[str, Any], folder: Optional[Path] = None) -> None:
    path = _file(folder)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(protect(json.dumps(data).encode("utf-8")))
    os.replace(tmp, path)


def get_credentials() -> Optional[tuple[str, str]]:
    with _lock:
        d = _load()
    u, p = d.get("username"), d.get("password")
    return (u, p) if u and p else None


def set_credentials(username: str, password: str, folder: Optional[Path] = None) -> None:
    with _lock:
        d = _load(folder)
        d.update(username=username, password=password)
        _save(d, folder)


def clear_credentials() -> None:
    with _lock:
        d = _load()
        d.pop("username", None)
        d.pop("password", None)
        _save(d)


def load_cookies() -> list[dict[str, Any]]:
    with _lock:
        cookies = _load().get("cookies") or []
    return [c for c in cookies if isinstance(c, dict) and c.get("name")]


def save_cookies(cookies: list[dict[str, Any]], folder: Optional[Path] = None) -> None:
    with _lock:
        d = _load(folder)
        d["cookies"] = cookies
        _save(d, folder)


def clear_all() -> None:
    with _lock:
        try:
            secrets_path().unlink()
        except FileNotFoundError:
            pass
