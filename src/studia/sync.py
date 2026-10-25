"""Sign-in + fast synchronisation of courses and Panopto recordings."""

from __future__ import annotations

import dataclasses as dc
import datetime as dt
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

import requests

from . import secrets_store
from .config import Settings
from .domain import Course, Profile
from .ieulearn import AuthError, IEULearn, NeedLogin
from .library import Library
from .net import clone_session, export_cookies, import_cookies, make_session, merge_cookies
from .panopto import Panopto

log = logging.getLogger("studia")


@dc.dataclass
class SyncResult:
    profile: Optional[Profile]
    courses: list[Course]
    new_ids: list[str]
    errors: list[str]
    seconds: float


class SyncService:
    def __init__(self, library: Library, settings_getter: Callable[[], Settings], log_: Optional[logging.Logger] = None) -> None:
        self.lib = library
        self.settings = settings_getter
        self.log = log_ or log
        self._lock = threading.RLock()
        self._session = make_session()
        self._gen = 0                    # + 1 at each sign-in and sign-out
        import_cookies(self._session, secrets_store.load_cookies())

    # ------------------------------------------------------------------ session helpers
    def http(self) -> requests.Session:
        return self._session

    def worker_session(self) -> requests.Session:
        """A private session (copy of the cookies) for a background thread."""
        with self._lock:
            s = clone_session(self._session)
            s._studia_gen = self._gen  # type: ignore[attr-defined]
            return s

    def absorb(self, session: requests.Session) -> None:
        """Merge cookies a worker session received (e.g. a refreshed Panopto ticket) and persist them."""
        with self._lock:
            if getattr(session, "_studia_gen", self._gen) != self._gen:
                return          # taken before a sign-out (or another sign-in): it must not sign this PC in again
            merge_cookies(self._session, session)
            self._save_cookies()

    def _save_cookies(self) -> None:
        try:
            secrets_store.save_cookies(export_cookies(self._session))
        except OSError:
            self.log.warning("Could not store session cookies", exc_info=True)

    @property
    def profile(self) -> Optional[Profile]:
        raw = self.lib.get_meta("profile")
        if not raw:
            return None
        try:
            return Profile(**json.loads(raw))
        except (TypeError, ValueError):
            return None

    def _set_profile(self, p: Profile) -> None:
        self.lib.set_meta("profile", json.dumps(dc.asdict(p), ensure_ascii=False))

    def has_credentials(self) -> bool:
        return secrets_store.get_credentials() is not None

    def can_resume(self) -> bool:
        """A stored password or session exists, so the app can try to work without asking to sign in."""
        return self.has_credentials() or bool(secrets_store.load_cookies())

    # ------------------------------------------------------------------ account
    def sign_in(self, username: str, password: str) -> Profile:
        with self._lock:
            s = make_session()
            learn = IEULearn(s)
            learn.login(username, password)
            profile, courses = learn.dashboard()
            profile.username = username.strip()
            secrets_store.set_credentials(username.strip(), password)
            self._session = s
            self._gen += 1
            self._save_cookies()
            self.lib.upsert_courses(courses)
            self._set_profile(profile)
            self.log.info("Signed in as %s (%d courses)", profile.name or username, len(courses))
            return profile

    def sign_out(self) -> None:
        """Forget the stored password and session. The library (and whose it is) stays, so signing in again
        continues where it was; another person's sign-in gets a profile of its own."""
        with self._lock:
            secrets_store.clear_all()
            self._session = make_session()
            self._gen += 1

    @staticmethod
    def check_account(username: str, password: str) -> tuple[Profile, list[Course], list[dict]]:
        """Sign in to an IEULearn account in a session of its own (the current one is not touched): the account's
        identity, its courses and the new session cookies. Raises AuthError for a wrong password."""
        s = make_session()
        learn = IEULearn(s)
        learn.login(username, password)
        profile, courses = learn.dashboard()
        profile.username = username.strip()
        return profile, courses, export_cookies(s)

    def ensure_login(self) -> None:
        """Make sure the IEULearn session is valid, signing in again with stored credentials if needed."""
        with self._lock:
            learn = IEULearn(self._session)
            if learn.is_logged_in():
                return
            creds = secrets_store.get_credentials()
            if not creds:
                if secrets_store.rejected():
                    raise NeedLogin("IEULearn didn't accept your saved password (was it changed?). Sign in again.")
                raise NeedLogin("Please sign in to IEULearn.")
            self.log.info("IEULearn session expired; signing in again")
            try:
                learn.login(*creds)
            except AuthError as exc:       # the password was changed: never try it again in the background
                if not exc.server_error:   # (IEULearn's words along with a server error may be about the server)
                    secrets_store.mark_rejected(creds[1])
                raise
            self._save_cookies()

    def ensure_panopto(self, course_id: Optional[str] = None) -> None:
        with self._lock:
            if Panopto(self._session).is_authenticated():
                return
            self.ensure_login()
            cid = course_id or next((c.id for c in self.lib.courses() if c.has_panopto), None)
            if cid is None:
                raise RuntimeError("None of your courses has Panopto.")
            IEULearn(self._session).launch_panopto(cid)
            if not Panopto(self._session).is_authenticated():
                raise RuntimeError("Could not open Panopto through IEULearn. Try again in a moment.")
            self._save_cookies()

    # ------------------------------------------------------------------ sync
    def _dashboard(self) -> tuple[Profile, list[Course]]:
        try:
            return IEULearn(self._session).dashboard()
        except NeedLogin:
            self.ensure_login()   # only ever called on the thread that holds the lock
            return IEULearn(self._session).dashboard()

    def _launch(self, course_id: str, s: requests.Session) -> tuple[str, Optional[str], requests.Session]:
        try:
            return course_id, IEULearn(s).launch_panopto(course_id), s
        except RuntimeError as exc:  # transient: try again on the next sync, keep has_panopto
            self.log.warning("%s", exc)
            return course_id, "", s

    def _panopto_open(self) -> bool:
        try:
            return Panopto(self._session).is_authenticated()
        except (requests.RequestException, OSError):
            return False

    @staticmethod
    def _list(course: Course, s: requests.Session) -> tuple[Course, list, requests.Session]:
        return course, Panopto(s).list_recordings(course.id, course.folder_id or ""), s

    def sync(self, progress: Callable[[str], None] = lambda m: None,
             partial: Callable[[list[str]], None] = lambda ids: None) -> SyncResult:
        t0 = time.monotonic()
        errors: list[str] = []
        new_ids: list[str] = []
        with self._lock:
            progress("Checking IEULearn and Panopto…")
            listed: dict[str, list] = {}
            with ThreadPoolExecutor(max_workers=8, thread_name_prefix="sync") as pool:
                probe_session = clone_session(self._session)
                dash_session = clone_session(self._session)
                f_dash = pool.submit(lambda: IEULearn(dash_session).dashboard())
                panopto_down = False
                try:
                    panopto_ok = Panopto(probe_session).is_authenticated()
                except (requests.RequestException, OSError) as exc:   # IEULearn's own content still syncs
                    self.log.warning("Panopto can't be reached: %s", exc)
                    panopto_ok, panopto_down = False, True
                early = []
                if panopto_ok:  # fast path: list known folders while the dashboard loads
                    early = [pool.submit(self._list, c, clone_session(probe_session))
                             for c in self.lib.courses() if c.folder_id]
                for f in early:
                    try:
                        course, recs, s = f.result()
                        merge_cookies(self._session, s)
                        listed[course.id] = recs
                        first = not self.lib.has_recordings(course.id)
                        new_ids += self.lib.merge_recordings(course.id, recs, first_sync=first)
                    except Exception as exc:  # noqa: BLE001 - retried below
                        self.log.info("Early listing failed (%s); retrying", exc)
                if listed:
                    partial(list(new_ids))   # the UI can show new recordings before the dashboard arrives
                try:
                    profile, courses = f_dash.result()      # NetworkError propagates
                    merge_cookies(self._session, dash_session)
                except NeedLogin:
                    profile, courses = self._dashboard()    # signs in again with stored credentials
            old = self.profile
            if old and old.username and not profile.username:
                profile.username = old.username
            if old and not profile.name:          # a page that came back without the name: keep who this is
                profile.name, profile.student_number = old.name, profile.student_number or old.student_number
            self._set_profile(profile)
            self.lib.upsert_courses(courses)
            known = {c.id: c for c in self.lib.courses()}

            # courses whose Panopto folder we don't know yet (first sync / new course)
            todo = []
            for c in known.values():
                if c.folder_id:
                    continue
                last = self.lib.get_meta(f"nofolder:{c.id}")
                if not c.has_panopto and last and (dt.datetime.now() - dt.datetime.fromisoformat(last)).days < 1:
                    continue
                todo.append(c.id)
            if panopto_down:
                errors.append("Panopto isn't answering right now: new recordings are checked again next time.")
            elif todo or not panopto_ok:
                progress("Connecting to Panopto…")
                self.ensure_login()
                launch_ids = todo or [next((c.id for c in known.values() if c.folder_id), None)]
                launch_ids = [i for i in launch_ids if i]
                sessions = [clone_session(self._session) for _ in launch_ids]  # cloned here: workers must not take the lock
                with ThreadPoolExecutor(max_workers=4, thread_name_prefix="lti") as pool:
                    for cid, folder, s in pool.map(self._launch, launch_ids, sessions):
                        merge_cookies(self._session, s)
                        if folder:
                            self.lib.set_course_folder(cid, folder, True)
                        elif folder is None and cid in todo:   # "" = launch failed, retry next time
                            self.lib.set_course_folder(cid, None, False)
                            self.lib.set_meta(f"nofolder:{cid}", dt.datetime.now().isoformat(timespec="seconds"))
                self._save_cookies()
                if any(c.folder_id for c in self.lib.courses()) and not self._panopto_open():
                    panopto_down = True
                    errors.append("Panopto didn't open through IEULearn: new recordings are checked again next time.")

            progress("Looking for new recordings…")
            with_folders = [c for c in self.lib.courses() if c.folder_id] if not panopto_down else []
            missing = [c for c in with_folders if c.id not in listed]
            failed: list[str] = []
            with ThreadPoolExecutor(max_workers=6, thread_name_prefix="list") as pool:
                futures = {pool.submit(self._list, c, clone_session(self._session)): c for c in missing}
                for f, c in futures.items():
                    try:
                        course, recs, s = f.result()
                    except Exception as exc:  # noqa: BLE001 - one course failing must not stop the rest
                        self.log.warning("Listing the recordings of %s failed: %s", c.code or c.id, exc)
                        failed.append(c.code or c.name or c.id)
                        continue
                    merge_cookies(self._session, s)
                    listed[course.id] = recs
                    first = not self.lib.has_recordings(course.id)
                    new_ids += self.lib.merge_recordings(course.id, recs, first_sync=first)
            if failed:
                errors.append(f"Couldn't check new recordings for {', '.join(sorted(set(failed)))}: Panopto didn't "
                              "answer. They're checked again next time.")
            self._save_cookies()
            self.lib.set_meta("last_sync", dt.datetime.now().isoformat(timespec="seconds"))
            secs = time.monotonic() - t0
            self.log.info("Sync done in %.1fs: %d courses, %d new recordings", secs, len(courses), len(new_ids))
            return SyncResult(self.profile, self.lib.courses(), new_ids, errors, secs)

    # ------------------------------------------------------------------ course content
    OVERVIEW_EVERY = dt.timedelta(hours=20)       # outcomes, weekly topics, grading: rarely change
    SYLLABUS_EVERY = dt.timedelta(days=7)

    @staticmethod
    def _stale(stamp: str, every: dt.timedelta) -> bool:
        try:
            return dt.datetime.now() - dt.datetime.fromisoformat(stamp) > every
        except (TypeError, ValueError):
            return True

    def _content(self, course: Course, s: requests.Session, info: dict) -> dict:
        from .coursesite import CourseSite

        site = CourseSite(s)
        out: dict = {"course": course, "session": s}
        out["units"] = site.unit_plan(course.id, course.title)
        out["announcements"] = site.announcements(course.id)
        if self._stale(info.get("overview_at", ""), self.OVERVIEW_EVERY):
            out["overview"] = site.overview(course.id)
        return out

    def sync_content(self, progress: Callable[[str], None] = lambda m: None) -> dict:
        """Weekly units, materials and announcements of every course (each sync, ~100 KB per course), the course
        overview (daily) and the public English syllabus (weekly). Nothing is downloaded: materials are listed."""
        from .coursesite import cover_file, fetch_cover
        from .syllabus import fetch_syllabus

        t0 = time.monotonic()
        courses = [c for c in self.lib.courses() if c.code]
        infos = {c.id: self.lib.course_info(c.id) for c in courses}
        with self._lock:
            sessions = [clone_session(self._session) for _ in courses]   # cloned here: workers must not take the lock
        new_materials: list[str] = []
        new_announcements: list[str] = []
        errors: list[str] = []
        progress("Loading course content…")
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="content") as pool:
            futures = [pool.submit(self._content, c, s, infos[c.id]) for c, s in zip(courses, sessions)]
            plain = requests.Session()
            syl = {c.id: pool.submit(fetch_syllabus, c.code, session=plain) for c in courses
                   if self._stale(infos[c.id].get("syllabus_at", ""), self.SYLLABUS_EVERY)}
            for f in futures:
                try:
                    res = f.result()
                except NeedLogin:
                    raise
                except Exception as exc:  # noqa: BLE001 - one course failing must not stop the rest
                    self.log.warning("Course content failed: %s", exc)
                    errors.append(str(exc))
                    continue
                c = res["course"]
                now = dt.datetime.now().isoformat(timespec="seconds")
                first = not infos[c.id].get("content_at")
                new_materials += self.lib.merge_units(c.id, res["units"], first_sync=first)
                new_announcements += self.lib.merge_announcements(c.id, res["announcements"], first_sync=first)
                fields = {"content_at": now}
                if "overview" in res:
                    fields.update(overview=res["overview"].to_json(), overview_at=now)
                self.lib.set_course_info(c.id, **fields)
                with self._lock:
                    merge_cookies(self._session, res["session"])
            for cid, f in syl.items():
                try:
                    s = f.result()
                except Exception as exc:  # noqa: BLE001 - the public site may be down; try again next time
                    self.log.info("Syllabus failed for %s: %s", cid, exc)
                    continue
                if not s and infos[cid].get("syllabus"):
                    # "not found" may also be a department site that didn't answer: keep the one we have and look
                    # again tomorrow instead of in a week
                    retry = dt.datetime.now() - self.SYLLABUS_EVERY + dt.timedelta(days=1)
                    self.lib.set_course_info(cid, syllabus_at=retry.isoformat(timespec="seconds"))
                    continue
                self.lib.set_course_info(cid, syllabus=s.to_json() if s else {},
                                         syllabus_at=dt.datetime.now().isoformat(timespec="seconds"))
            for f in [pool.submit(fetch_cover, c.cover_url) for c in courses
                      if c.cover_url and not cover_file(c.cover_url).is_file()]:
                try:
                    f.result()
                except Exception as exc:  # noqa: BLE001 - only a picture; the Home page has a fallback
                    self.log.info("Cover picture failed: %s", exc)
        self.log.info("Course content in %.1fs: %d new materials, %d new announcements", time.monotonic() - t0,
                      len(new_materials), len(new_announcements))
        return {"new_materials": new_materials, "new_announcements": new_announcements, "errors": errors}


__all__ = ["SyncService", "SyncResult", "AuthError", "NeedLogin"]
