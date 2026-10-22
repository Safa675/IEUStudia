"""Profiles: several IEULearn accounts on one PC. Each has its own library, transcripts, settings, stored password and
output folder, so two students never see each other's courses. Switching restarts the app, so nothing of the other
account stays in memory.

The first account keeps the app folder itself (nothing is moved); added accounts live in ``profiles\\<id>``.
``profiles.json`` in the app folder lists them and remembers the active one.
"""

from __future__ import annotations

import dataclasses as dc
import datetime as dt
import json
import logging
import os
import re
import secrets
import shutil
import threading
from pathlib import Path
from typing import Any, Optional

from .paths import _home_override, app_dir, documents_dir, set_profile_dir

log = logging.getLogger("studia")
MAIN = "main"
_ID = re.compile(r"p-[0-9a-f]{8}")
_lock = threading.RLock()


@dc.dataclass
class Account:
    id: str
    dir: str = ""                 # relative to the app folder; "" = the app folder itself (the first account)
    username: str = ""
    name: str = ""
    student_number: str = ""
    added_at: str = ""
    last_used: str = ""

    @property
    def path(self) -> Path:
        return app_dir() / self.dir if self.dir else app_dir()

    def view(self, active_id: str) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "username": self.username, "student_number": self.student_number,
                "active": self.id == active_id, "main": self.id == MAIN, "last_used": self.last_used,
                "has_login": (self.path / "secrets.bin").exists()}


def _registry() -> Path:
    return app_dir() / "profiles.json"


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def load() -> tuple[str, list[Account]]:
    """(active id, accounts). Before profiles.json exists there is one account: the app folder itself."""
    with _lock:
        try:
            raw = json.loads(_registry().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        fields = {f.name for f in dc.fields(Account)}
        accounts: list[Account] = []
        for a in raw.get("accounts") or []:
            if isinstance(a, dict) and (a.get("id") == MAIN or _ID.fullmatch(str(a.get("id", "")))):
                acc = Account(**{k: str(v or "") for k, v in a.items() if k in fields})
                acc.dir = "" if acc.id == MAIN else f"profiles/{acc.id}"      # never trust a stored path
                if not any(x.id == acc.id for x in accounts):
                    accounts.append(acc)
        if not any(a.id == MAIN for a in accounts):
            accounts.insert(0, Account(MAIN))
        active = str(raw.get("active") or MAIN)
        if not any(a.id == active for a in accounts):
            active = MAIN
        return active, accounts


def _save(active: str, accounts: list[Account]) -> None:
    path = _registry()
    tmp = path.with_name(path.name + ".tmp")
    data = {"active": active, "accounts": [dc.asdict(a) for a in accounts]}
    tmp.write_bytes(json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"))
    os.replace(tmp, path)


def get(account_id: str) -> Optional[Account]:
    return next((a for a in load()[1] if a.id == account_id), None)


def active() -> Account:
    active_id, accounts = load()
    return next(a for a in accounts if a.id == active_id)


def find(username: str) -> Optional[Account]:
    u = (username or "").strip().casefold()
    return next((a for a in load()[1] if u and a.username.strip().casefold() == u), None)


def activate(account_id: Optional[str] = None) -> Account:
    """Point the data paths (library, transcripts, settings, password) at an account: the active one by default."""
    active_id, accounts = load()
    acc = next((a for a in accounts if a.id == (account_id or active_id)), accounts[0])
    set_profile_dir(acc.dir)
    return acc


def set_active(account_id: str) -> Account:
    """Make an account the one the app opens next (the caller restarts the app)."""
    with _lock:
        active_id, accounts = load()
        acc = next((a for a in accounts if a.id == account_id), None)
        if acc is None:
            raise KeyError("This profile no longer exists.")
        acc.last_used = _now()
        _save(acc.id, accounts)
        return acc


def remember(account_id: str, username: str, name: str = "", student_number: str = "") -> None:
    """Keep the account's identity in the list (shown in the profile switcher without opening its library)."""
    with _lock:
        active_id, accounts = load()
        acc = next((a for a in accounts if a.id == account_id), None)
        if acc is None:
            return
        new = (username or acc.username, name or acc.name, student_number or acc.student_number)
        if new == (acc.username, acc.name, acc.student_number) and _registry().exists():
            return
        acc.username, acc.name, acc.student_number = new
        acc.added_at = acc.added_at or _now()
        _save(active_id, accounts)


def create(username: str, name: str = "", student_number: str = "") -> Account:
    """A new, empty account folder in the list (not active yet)."""
    with _lock:
        active_id, accounts = load()
        aid = "p-" + secrets.token_hex(4)
        acc = Account(aid, f"profiles/{aid}", username.strip(), name, student_number, _now(), "")
        acc.path.mkdir(parents=True, exist_ok=True)
        accounts.append(acc)
        _save(active_id, accounts)
        log.info("Added profile %s", aid)
        return acc


def remove(account_id: str) -> None:
    """Delete an added account and its data folder (library, transcripts, stored password). Its output folder in
    Documents is left alone. The first account can't be removed (sign out instead), nor the active one."""
    with _lock:
        active_id, accounts = load()
        if account_id == MAIN:
            raise ValueError("The first profile can't be removed; sign out of it instead.")
        if account_id == active_id:
            raise ValueError("Switch to another profile before removing this one.")
        acc = next((a for a in accounts if a.id == account_id), None)
        if acc is None or not _ID.fullmatch(acc.id):
            raise KeyError("This profile no longer exists.")
        accounts.remove(acc)
        _save(active_id, accounts)
    folder = app_dir() / "profiles" / acc.id
    shutil.rmtree(folder, ignore_errors=True)
    log.info("Removed profile %s", acc.id)


def output_dir_for(acc: Account, display: str) -> Path:
    """An added account's own output folder: Documents\\IEUStudia - <name> (<home>\\profiles\\<id>\\output in tests)."""
    if acc.id == MAIN:
        from .paths import default_output_dir

        return default_output_dir()
    if _home_override() is not None:
        return acc.path / "output"
    label = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", display or acc.username or acc.id).strip(" .") or acc.id
    return documents_dir() / f"IEUStudia - {label[:60]}"


def seed(acc: Account, profile: Any, courses: list[Any], base_settings: Any) -> None:
    """A new account's settings (the machine-wide choices of the current one, its own output folder) and library
    (identity and course list), so it opens signed in and ready to sync."""
    from .config import save_settings
    from .library import Library

    settings = dc.replace(base_settings, course_prefs={}, first_run_done=False,
                          output_dir=str(output_dir_for(acc, getattr(profile, "name", ""))))
    save_settings(settings, acc.path / "settings.json")
    lib = Library(acc.path / "library.db")
    try:
        if courses:
            lib.upsert_courses(courses)
        lib.set_meta("profile", json.dumps(dc.asdict(profile), ensure_ascii=False))
    finally:
        lib.close()
