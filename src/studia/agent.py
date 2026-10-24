"""The tray helper (``IEUStudia --agent``): a small background process, without the app window, that checks
IEULearn now and then and shows a Windows notification when something new arrives — a lecture recording of your
section, an announcement (quiz and exam news first) or, if you want, new files. Clicking the notification or the
tray icon opens IEUStudia.

It stays out of the way of the app: while the window is open the app does its own checking and the helper waits.
It never downloads materials or transcribes anything, and it uses only what the app stored (your encrypted
IEULearn login). Memory: tens of MB (no speech engine, no browser engine).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("studia")

CMD_OPEN, CMD_SYNC, CMD_PAUSE, CMD_STARTUP, CMD_QUIT = 1, 2, 3, 4, 9
FIRST_CHECK_S = 90            # after signing in to Windows, let the PC settle first
KEEP_SEEN = 3000


def instance_name() -> str:
    from .winutil import instance_name as ui_name

    return ui_name().replace("SingleInstance", "Helper")


def ui_running() -> bool:
    """Is the app window open (it holds its single-instance lock)?"""
    import ctypes

    from .winutil import instance_name as ui_name

    k = ctypes.windll.kernel32
    k.OpenMutexW.restype = ctypes.c_void_p
    h = k.OpenMutexW(0x00100000, False, ui_name())          # SYNCHRONIZE
    if h:
        k.CloseHandle(ctypes.c_void_p(h))
        return True
    return False


def running() -> bool:
    """Is the helper running?"""
    import ctypes

    k = ctypes.windll.kernel32
    k.OpenMutexW.restype = ctypes.c_void_p
    h = k.OpenMutexW(0x00100000, False, instance_name())
    if h:
        k.CloseHandle(ctypes.c_void_p(h))
        return True
    return False


def ensure_running() -> bool:
    """Start the helper if the user wants it and it is not running (called by the app)."""
    from . import appconfig, launch

    if not appconfig.load().tray or running():
        return False
    return launch.spawn(launch.app_command("--agent"))


# ----------------------------------------------------------------------------- what to tell the user
def _seen_path() -> Path:
    from .paths import profile_dir

    return profile_dir() / "notified.json"


def _load_seen() -> dict[str, list[str]]:
    try:
        d = json.loads(_seen_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_seen(seen: dict[str, list[str]]) -> None:
    try:
        _seen_path().write_text(json.dumps({k: v[-KEEP_SEEN:] for k, v in seen.items()}), encoding="utf-8")
    except OSError:
        pass


def summarize(lib: Any, settings: Any, cfg: Any, new_recordings: list[str], new_announcements: list[str],
              new_materials: list[str], seen: dict[str, list[str]]) -> Optional[tuple[str, str, Any]]:
    """One notification for what a check found (or None): title, text and what clicking it opens."""
    from .alerts import matches
    from .sections import available_sections, classify, guess_my_section

    lines: list[tuple[int, str, str]] = []            # (priority, course id, line)
    courses = {c.id: c for c in lib.courses()}
    done = {k: set(v) for k, v in seen.items()}
    if cfg.notify_recordings and new_recordings:
        by_course: dict[str, list[Any]] = {}
        for did in new_recordings:
            r = lib.get_recording(did)
            if r is None or r.duplicate_of or r.hidden or did in done.get("rec", set()):
                continue
            by_course.setdefault(r.course_id, []).append(r)
        for cid, recs in by_course.items():
            c = courses.get(cid)
            if c is None:
                continue
            all_recs = lib.recordings(cid)
            prefs = settings.prefs(cid)
            mine = classify(c, all_recs, prefs.my_section or guess_my_section(c)) if available_sections(all_recs) else {}
            own = [r for r in recs if mine.get(r.delivery_id) is not False or prefs.show_mode == "all"]
            if own:
                n = len(own)
                lines.append((2, cid, f"{c.code}: {n} new lecture recording{'s' if n > 1 else ''}"))
    if cfg.notify_announcements and new_announcements:
        fresh = set(new_announcements) - done.get("ann", set())
        for a in lib.announcements(limit=200):
            if a["id"] not in fresh:
                continue
            c = courses.get(a["course_id"])
            cats = matches(f"{a['title']}\n{a['body'] or ''}")
            key = next((k for k in ("final", "midterm", "quiz", "exam", "project", "homework", "schedule") if k in cats), "")
            prefix = f"{key.title()} · " if key else ""
            lines.append((0 if key else 1, a["course_id"], f"{c.code if c else 'Course'}: {prefix}{a['title']}"))
    if cfg.notify_materials and new_materials:
        fresh = [m for m in new_materials if m not in done.get("mat", set())]
        per: dict[str, int] = {}
        for rid in fresh:
            m = lib.get_material(rid)
            if m:
                per[m["course_id"]] = per.get(m["course_id"], 0) + 1
        for cid, n in per.items():
            c = courses.get(cid)
            lines.append((3, cid, f"{c.code if c else 'Course'}: {n} new file{'s' if n > 1 else ''} on IEULearn"))
    seen.setdefault("rec", []).extend(new_recordings)
    seen.setdefault("ann", []).extend(new_announcements)
    seen.setdefault("mat", []).extend(new_materials)
    if not lines:
        return None
    lines.sort(key=lambda x: x[0])
    targets = {cid for _, cid, _ in lines}
    title = "IEUStudia: heads-up" if lines[0][0] == 0 else "IEUStudia: new on IEULearn"
    text = "\n".join(line for _, _, line in lines[:4]) + (f"\n+{len(lines) - 4} more" if len(lines) > 4 else "")
    payload = f"course:{next(iter(targets))}" if len(targets) == 1 else "home"
    return title, text, payload


# ----------------------------------------------------------------------------- the helper
class Helper:
    def __init__(self, at_login: bool) -> None:
        from . import appconfig

        self.cfg = appconfig.load()
        self.paused = False
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.first_delay = FIRST_CHECK_S if at_login else 20
        self.last = ""
        self.told_rejected = False       # said once that IEULearn refused the saved password
        from .tray import Tray

        self.tray = Tray("IEUStudia", _icon(), self._menu, self._command, self._open)

    # ---- tray
    def _menu(self) -> list[tuple[int, str, bool, bool]]:
        from . import launch

        return [(CMD_OPEN, "Open IEUStudia", False, True),
                (CMD_SYNC, "Check IEULearn now", False, not ui_running()),
                (0, "", False, False),
                (CMD_PAUSE, "Pause notifications", self.paused, True),
                (CMD_STARTUP, "Start with Windows", launch.startup_enabled(), True),
                (0, "", False, False),
                (CMD_QUIT, "Quit the helper", False, True)]

    def _command(self, cmd: int) -> None:
        from . import appconfig, launch

        if cmd == CMD_OPEN:
            self._open(None)
        elif cmd == CMD_SYNC:
            self.wake.set()
        elif cmd == CMD_PAUSE:
            self.paused = not self.paused
        elif cmd == CMD_STARTUP:
            on = not launch.startup_enabled()
            launch.set_startup(on)
            appconfig.update({"start_with_windows": on})
        elif cmd == CMD_QUIT:
            self.stop.set()
            self.wake.set()
            self.tray.stop()

    def _open(self, payload: Optional[Any]) -> None:
        from . import launch

        target = payload if isinstance(payload, str) and payload else ""
        launch.spawn(launch.app_command(*(["--open", target] if target else [])))

    # ---- work
    def _loop(self) -> None:
        from . import appconfig

        self.wake.wait(self.first_delay)
        while not self.stop.is_set():
            self.wake.clear()
            self.cfg = appconfig.load()
            if not self.cfg.tray:                       # turned off in the app: end
                log.info("Helper: background mode was turned off")
                self.tray.stop()
                return
            if not ui_running():
                try:
                    self._check()
                except Exception:  # noqa: BLE001
                    log.exception("Helper: check failed")
            self.wake.wait(self.cfg.sync_minutes * 60)

    def _check(self) -> None:
        from . import __version__
        from .accounts import activate
        from .config import load_settings
        from .library import Library
        from .paths import db_path
        from .sync import SyncService

        activate()
        settings = load_settings()
        lib = Library(db_path(), app_version=__version__)
        try:
            from . import secrets_store

            sync = SyncService(lib, lambda: settings, log)
            if not sync.has_credentials():
                if secrets_store.rejected():
                    self.tray.set_tooltip("IEUStudia · IEULearn didn't accept your saved password: open IEUStudia "
                                          "to sign in again")
                    if not self.told_rejected:
                        self.told_rejected = True
                        self.tray.notify("IEULearn didn't accept your saved password",
                                         "Did you change it? Open IEUStudia and sign in again; until then there are "
                                         "no notifications.")
                    return
                self.tray.set_tooltip("IEUStudia · sign in in the app to get notifications")
                return
            self.told_rejected = False
            res = sync.sync()
            content = sync.sync_content()
            seen = _load_seen()
            note = summarize(lib, settings, self.cfg, res.new_ids, content["new_announcements"],
                             content["new_materials"], seen)
            _save_seen(seen)
            self.last = time.strftime("%H:%M")
            self.tray.set_tooltip(f"IEUStudia · checked {self.last}")
            if note and not self.paused:
                self.tray.notify(*note)
            log.info("Helper: checked IEULearn (%d new recordings, %d announcements, %d files)",
                     len(res.new_ids), len(content["new_announcements"]), len(content["new_materials"]))
        finally:
            lib.close()

    def run(self) -> None:
        threading.Thread(target=self._loop, name="helper", daemon=True).start()
        self.tray.run()
        self.stop.set()
        self.wake.set()


def _icon() -> Path:
    return Path(__file__).with_name("assets") / "icon.ico"


def main(argv: Optional[list[str]] = None) -> None:
    import sys

    from .accounts import activate
    from .log import setup_logging
    from .paths import migrate_legacy_data
    from .winutil import acquire_single_instance, set_app_id

    argv = list(sys.argv[1:] if argv is None else argv)
    if not acquire_single_instance(instance_name()):
        return
    set_app_id()
    migrate_legacy_data()
    activate()
    setup_logging("--debug" in argv, "helper.log")
    from . import appconfig

    if not appconfig.load().tray:
        log.info("Helper: background mode is off; not starting")
        return
    log.info("Helper started%s", " at Windows sign-in" if "--at-login" in argv else "")
    Helper("--at-login" in argv).run()
    log.info("Helper stopped")
