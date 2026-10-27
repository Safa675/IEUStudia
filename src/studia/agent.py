"""The tray helper (``IEUStudia --agent``): a small background process, without the app window, that checks
IEULearn now and then and shows a Windows notification when something new arrives — a lecture recording of your
section, an announcement (quiz and exam news first) or, if you want, new files. Clicking the notification or the
tray icon opens IEUStudia.

It stays out of the way of the app: while the window is open the app does its own checking and the helper waits.
It never downloads materials or transcribes anything, and it uses only what the app stored (your encrypted
IEULearn login). Memory: tens of MB (no speech engine, no browser engine).
"""

from __future__ import annotations

import datetime as dt
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
REMIND_FROM = dt.time(17)     # a reminder comes the evening before, from 17:00 ...
REMIND_UNTIL = dt.time(14)    # ... or on the day itself until 14:00, if the PC was off that evening


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
    """Add to what was told: the app and the helper may both have told something since this was loaded."""
    now = _load_seen()
    merged = {k: list(dict.fromkeys(now.get(k, []) + seen.get(k, [])))[-KEEP_SEEN:] for k in set(now) | set(seen)}
    path = _seen_path()
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(merged), encoding="utf-8")
        tmp.replace(path)
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
    tab = {0: "announcements", 1: "announcements", 2: "lectures", 3: "materials"}[lines[0][0]]   # what it's about
    payload = f"course:{next(iter(targets))}:{tab}" if len(targets) == 1 else "home"
    return title, text, payload


def _day(d: dt.date) -> str:
    from .outputs.common import MONTHS, WEEKDAYS_SHORT

    return f"{WEEKDAYS_SHORT[d.weekday()]} {d.day} {MONTHS[d.month - 1][:3]}"


def _los(los: list[int]) -> str:
    """[3, 4, 6] → "LO3–LO4, LO6"."""
    runs: list[list[int]] = []
    for n in sorted(set(los)):
        if runs and n == runs[-1][1] + 1:
            runs[-1][1] = n
        else:
            runs.append([n, n])
    return ", ".join(f"LO{a}" if a == b else f"LO{a}–LO{b}" for a, b in runs)


def reminders(events: list[dict[str, Any]], now: dt.datetime, seen: dict[str, list[str]]
              ) -> Optional[tuple[str, str, str]]:
    """The announced quizzes and exams to remind you of now, each once (``seen["rem"]``): a quiz the evening before
    the first of its week's class days still to come (or that day, until that class is over), a midterm or final the
    evening before its week, since the exam schedule gives its day (until 14:00 on the first day). One notification
    (title, text, what clicking it opens) or None. ``events``: calendar_export.assessment_events."""
    told = set(seen.get("rem", []))
    today, hm = now.date(), f"{now:%H:%M}"
    due = []
    for e in events:
        quiz = e.get("kind") == "quiz"
        classes = list(zip(e.get("classes") or [], e.get("ends") or ["23:59"] * len(e.get("classes") or [])))
        if quiz and classes:
            left = [(d, end) for d, end in classes if d > today.isoformat() or (d == today.isoformat() and end > hm)]
            if not left:                                  # all its class hours are over: it's done
                continue
            first = dt.date.fromisoformat(left[0][0])
            until = dt.datetime.combine(first, dt.time.fromisoformat(left[0][1]))
            rest = [dt.date.fromisoformat(d) for d, _ in left[1:]]
        else:
            first, rest = dt.date.fromisoformat(e["start"]), []
            until = dt.datetime.combine(first, REMIND_UNTIL)
        # a quiz moved to another week is said again; an exam week once, even when an announcement replaces the
        # syllabus's word for it
        key = f"{e['uid']}@{e['start']}" if quiz else f"{e.get('course_id') or e['course']}-exam-week{e['week']}"
        if key not in told and dt.datetime.combine(first - dt.timedelta(days=1), REMIND_FROM) <= now <= until:
            due.append((key, e, first, rest, len(classes)))
    if not due:
        return None
    seen.setdefault("rem", []).extend(key for key, *_ in due)
    lines = []
    for _, e, first, rest, n in due:
        when = "today" if first == today else f"tomorrow ({_day(first)})"
        los = f" Covers {_los(e['los'])}." if e.get("los") else ""
        if e.get("kind") == "quiz" and n:
            lines.append(f"{e['course']}: {e['name']} {'may be ' if n > 1 else ''}{when}"
                         f"{' or ' + _day(rest[0]) if rest else ''}, as announced.{los}")
        elif e.get("kind") == "quiz":
            lines.append(f"{e['course']}: {e['name']} is in week {e['week']}, which starts {when}.{los}")
        else:
            lines.append(f"{e['course']}: {e['name']} is in week {e['week']}, which starts {when}. "
                         f"The exam schedule on IEULearn has the day.{los}")
    _, e, first, rest, n = due[0]
    title = "IEUStudia: coming up"
    if len(due) == 1 and e.get("kind") == "quiz" and n == 1:
        title = f"IEUStudia: {e['name']} {'today' if first == today else 'tomorrow'}"
    payload = "home"
    if len(due) == 1 and e.get("course_id"):           # Get ready knows what was announced; a syllabus week: the week
        payload = (f"week:{e['course_id']}:{e['week']}" if "-syllabus-week" in e["uid"]
                   else f"ready:{e['course_id']}:{e['name']}")
    return title, "\n".join(lines[:4]), payload


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
                (CMD_QUIT, "Stop background checks", False, True)]

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
        elif cmd == CMD_QUIT:            # off until it's turned on again in Settings, not just until the app opens
            appconfig.update({"tray": False, "start_with_windows": False})
            launch.set_startup(False)
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
                told = False
                try:
                    told = self._check()
                except Exception:  # noqa: BLE001
                    log.exception("Helper: check failed")
                if not told:            # one notification at a time: a click opens what the last one was about
                    self._check_update()
            self.wake.wait(self.cfg.sync_minutes * 60)

    def _check_update(self) -> None:
        """A new version of IEUStudia: said once, and opening the app shows how to update."""
        from . import updates

        try:
            if not self.cfg.check_updates or not updates.installed():
                return
            st = updates.check()
            if st["available"] and not self.paused and updates.tell_once(st["version"]):
                self.tray.notify(f"IEUStudia {st['version']} is available",
                                 "Open IEUStudia to update. It takes about a minute.", "update")
        except Exception:  # noqa: BLE001
            log.debug("Helper: update check failed", exc_info=True)

    def _check(self) -> bool:
        """Check IEULearn; True if a notification was shown."""
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

            seen = _load_seen()
            note = None
            sync = SyncService(lib, lambda: settings, log)
            if not sync.has_credentials():
                if secrets_store.rejected():
                    self.tray.set_tooltip("IEUStudia · IEULearn didn't accept your saved password: open IEUStudia "
                                          "to sign in again")
                    if not self.told_rejected:
                        self.told_rejected = True
                        self.tray.notify("IEULearn didn't accept your saved password",
                                         "Did you change it? Open IEUStudia and sign in again; until then it can't "
                                         "check for new lectures.")
                        return True
                else:
                    self.tray.set_tooltip("IEUStudia · sign in in the app to get notifications")
            else:
                self.told_rejected = False
                try:
                    res = sync.sync()
                    content = sync.sync_content()
                    note = summarize(lib, settings, self.cfg, res.new_ids, content["new_announcements"],
                                     content["new_materials"], seen)
                    self.last = time.strftime("%H:%M")
                    self.tray.set_tooltip(f"IEUStudia · checked {self.last}")
                    log.info("Helper: checked IEULearn (%d new recordings, %d announcements, %d files)",
                             len(res.new_ids), len(content["new_announcements"]), len(content["new_materials"]))
                except Exception:  # noqa: BLE001 - IEULearn can't be reached: the reminders still come
                    log.warning("Helper: check failed", exc_info=True)
            if note is None and self.cfg.notify_reminders and not self.paused:     # one notification at a time
                from .calendar_export import assessment_events

                told = _load_seen().get("rem", [])      # the app may have reminded meanwhile (the check takes a while)
                seen["rem"] = list(dict.fromkeys(told + seen.get("rem", [])))
                note = reminders(assessment_events(lib, settings), dt.datetime.now(), seen)
            _save_seen(seen)
            told = bool(note) and not self.paused
            if told:
                self.tray.notify(*note)
            return told
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
