"""Credentials + session cookies, encrypted for the current Windows user with DPAPI.

Only this Windows account on this PC can decrypt ``secrets.bin``; nothing is ever logged.
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.wintypes as wt
import hashlib
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Iterator, Optional

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


_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateMutexW.restype = wt.HANDLE
_k32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
_k32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
_k32.ReleaseMutex.argtypes = [wt.HANDLE]
_k32.CloseHandle.argtypes = [wt.HANDLE]


@contextlib.contextmanager
def _turn(folder: Optional[Path] = None) -> Iterator[None]:
    """One reader-and-writer of ``secrets.bin`` at a time: the threads of this process, and the app and its tray
    helper (two processes) through a named mutex. Without it, one could write back what it read before the other
    saved a new password."""
    with _lock:
        name = "Local\\IEUStudia-secrets-" + hashlib.sha1(str(_file(folder)).lower().encode("utf-8")).hexdigest()[:16]
        handle = _k32.CreateMutexW(None, False, name)
        owned = bool(handle) and _k32.WaitForSingleObject(handle, 10_000) in (0, 0x80)   # WAIT_OBJECT_0, _ABANDONED
        try:
            yield
        finally:
            if owned:
                _k32.ReleaseMutex(handle)
            if handle:
                _k32.CloseHandle(handle)


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
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")          # each process its own
    tmp.write_bytes(protect(json.dumps(data).encode("utf-8")))
    os.replace(tmp, path)


def get_credentials() -> Optional[tuple[str, str]]:
    """The stored username and password; None when there are none, or IEULearn refused them (see rejected)."""
    with _turn():
        d = _load()
    u, p = d.get("username"), d.get("password")
    return (u, p) if u and p and not d.get("rejected") else None


def set_credentials(username: str, password: str, folder: Optional[Path] = None) -> None:
    with _turn(folder):
        d = _load(folder)
        d.update(username=username, password=password)
        d.pop("rejected", None)
        _save(d, folder)


def mark_rejected() -> None:
    """IEULearn refused the stored password (it was changed): stop signing in with it until the user signs in
    again, so nothing keeps trying a wrong password in the background."""
    with _turn():
        d = _load()
        if d.get("password"):
            d["rejected"] = True
            _save(d)


def rejected() -> bool:
    with _turn():
        return bool(_load().get("rejected"))


def clear_credentials() -> None:
    with _turn():
        d = _load()
        d.pop("username", None)
        d.pop("password", None)
        d.pop("rejected", None)
        _save(d)


def load_cookies() -> list[dict[str, Any]]:
    with _turn():
        cookies = _load().get("cookies") or []
    return [c for c in cookies if isinstance(c, dict) and c.get("name")]


def save_cookies(cookies: list[dict[str, Any]], folder: Optional[Path] = None) -> None:
    with _turn(folder):
        d = _load(folder)
        d["cookies"] = cookies
        _save(d, folder)


def clear_all() -> None:
    with _turn():
        try:
            secrets_path().unlink()
        except FileNotFoundError:
            pass
