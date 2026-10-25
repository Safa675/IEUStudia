"""Syncing against a fake IEULearn and Panopto: what still works when Panopto is down, a changed password, a page that
comes back without your name, signing out while files download, a course whose recordings can't be listed."""

import datetime as dt
import types

import pytest

from studia import secrets_store, sync as sync_mod
from studia.config import Settings
from studia.domain import Course, Profile, Recording
from studia.ieulearn import AuthError, NeedLogin
from studia.library import Library
from studia.net import NetworkError

COURSES = [Course(id="a", title="2026-1 MATH 240 P 1", code="MATH 240", name="Probability"),
           Course(id="b", title="2026-1 EEE 215 E 2", code="EEE 215", name="Electronics")]


def _rec(cid, n):
    return Recording(delivery_id=f"{cid}{n}", course_id=cid, folder_id="f", folder_path="", name="lecture",
                     start=dt.datetime(2026, 10, 13 + n, 9, 0), duration_s=3000, viewer_url="v")


@pytest.fixture
def site(monkeypatch):
    st = types.SimpleNamespace(profile=Profile(name="Ali Veli", student_number="20250001"), courses=list(COURSES),
                               logged_in=True, password="right", logins=[], panopto=True, folders={"a": "fa", "b": "fb"},
                               listing={"a": [_rec("a", 0)], "b": [_rec("b", 0)]}, launches=[], server_error=False)

    class FakeLearn:
        def __init__(self, s):
            pass

        def dashboard(self):
            if not st.logged_in:
                raise NeedLogin("expired")
            return Profile(**vars(st.profile)), [Course(**vars(c)) for c in st.courses]

        def is_logged_in(self):
            return st.logged_in

        def login(self, user, password):
            st.logins.append(password)
            if st.server_error:
                raise AuthError("Kimlik doğrulama servisine ulaşılamadı.", server_error=True)
            if password != st.password:
                raise AuthError("Invalid credentials.")
            st.logged_in = True

        def launch_panopto(self, cid):
            st.launches.append(cid)
            return st.folders.get(cid)

    class FakePanopto:
        def __init__(self, s):
            pass

        def is_authenticated(self):
            if isinstance(st.panopto, Exception):
                raise st.panopto
            return st.panopto

        def list_recordings(self, cid, folder):
            got = st.listing[cid]
            if isinstance(got, Exception):
                raise got
            return list(got)

    monkeypatch.setattr(sync_mod, "IEULearn", FakeLearn)
    monkeypatch.setattr(sync_mod, "Panopto", FakePanopto)
    return st


def _service(tmp_path):
    lib = Library(tmp_path / "lib.db")
    return lib, sync_mod.SyncService(lib, Settings)


def test_with_panopto_down_ieulearn_still_syncs(site, tmp_path):
    lib, sync = _service(tmp_path)
    site.panopto = NetworkError("Can't reach panopto.ieu.edu.tr. Check your internet connection.")
    res = sync.sync()
    assert res.errors == ["Panopto isn't answering right now: new recordings are checked again next time."]
    assert lib.get_meta("last_sync") and sync.profile.name == "Ali Veli" and len(lib.courses()) == 2


def test_courses_without_panopto_are_not_an_error(site, tmp_path):
    lib, sync = _service(tmp_path)
    site.panopto, site.folders = False, {}
    for _ in range(2):                                   # the second sync launches nothing, and still no error
        assert sync.sync().errors == []
    assert lib.get_meta("last_sync")


def test_a_course_whose_recordings_cant_be_listed_is_one_plain_line(site, tmp_path):
    lib, sync = _service(tmp_path)
    sync.sync()
    site.listing = {"a": RuntimeError("Expecting value: line 1 column 1 (char 0)"),
                    "b": RuntimeError("500 Server Error")}
    res = sync.sync()
    assert res.errors == ["Couldn't check new recordings for EEE 215, MATH 240: Panopto didn't answer. "
                          "They're checked again next time."]


def test_a_changed_password_is_tried_once_then_the_app_asks(site, tmp_path):
    lib, sync = _service(tmp_path)
    secrets_store.set_credentials("ali", "old-password")
    site.logged_in = False                               # the session expired, and the password was changed
    with pytest.raises(AuthError):
        sync.sync()
    assert site.logins == ["old-password"] and secrets_store.rejected() and not sync.has_credentials()
    with pytest.raises(NeedLogin, match="didn't accept your saved password"):
        sync.ensure_login()
    assert site.logins == ["old-password"]               # never tried again in the background
    secrets_store.set_credentials("ali", "right")        # signing in again clears it
    assert not secrets_store.rejected() and sync.has_credentials()


def test_ieulearns_words_along_with_a_server_error_dont_lock_out_the_saved_password(site, tmp_path):
    lib, sync = _service(tmp_path)
    secrets_store.set_credentials("ali", "right")
    site.logged_in, site.server_error = False, True     # the sign-in service is down and says so on the form
    with pytest.raises(AuthError, match="ulaşılamadı"):
        sync.sync()
    assert not secrets_store.rejected() and sync.has_credentials()
    site.server_error = False                            # back up: the next background sync signs in by itself
    assert sync.sync().errors == [] and site.logins == ["right", "right"]


def test_a_page_without_your_name_keeps_who_you_are(site, tmp_path):
    lib, sync = _service(tmp_path)
    sync.sync()
    site.profile = Profile(name="")
    sync.sync()
    assert (sync.profile.name, sync.profile.student_number) == ("Ali Veli", "20250001")


def test_signing_out_while_files_download_stays_signed_out(site, tmp_path):
    lib, sync = _service(tmp_path)
    worker = sync.worker_session()                       # a material download started before the sign-out
    worker.cookies.set("PHPSESSID", "still-valid", domain="learn.ieu.edu.tr")
    sync.sign_out()
    sync.absorb(worker)                                  # ...and finishes after it
    assert not sync.can_resume() and secrets_store.load_cookies() == []
    fresh = sync.worker_session()
    fresh.cookies.set("PHPSESSID", "new", domain="learn.ieu.edu.tr")
    sync.absorb(fresh)
    assert sync.can_resume()


def test_a_recording_that_moved_to_another_course_moves_and_is_not_new_again(tmp_path):
    lib = Library(tmp_path / "lib.db")
    lib.upsert_courses(list(COURSES))
    lib.merge_recordings("a", [_rec("a", 0), _rec("x", 1)], first_sync=True)
    assert lib.merge_recordings("b", [_rec("x", 1)], first_sync=False) == []      # listed by both: stays in a
    assert lib.get_recording("x1").course_id == "a"
    lib.merge_recordings("a", [_rec("a", 0)], first_sync=False)                     # gone from a's folder
    for _ in range(2):
        assert lib.merge_recordings("b", [_rec("x", 1)], first_sync=False) == []
    assert lib.get_recording("x1").course_id == "b"


def test_a_syllabus_that_cant_be_fetched_is_kept_and_looked_for_again_tomorrow(site, tmp_path, monkeypatch):
    from studia import coursesite, syllabus

    lib, sync = _service(tmp_path)
    lib.upsert_courses(list(COURSES))
    lib.set_course_info("a", syllabus={"weeks": [{"no": 8, "subject": "Midterm"}]}, syllabus_at="2026-01-01T00:00:00")

    class Site:
        def __init__(self, s):
            pass

        def unit_plan(self, cid, title):
            return []

        def announcements(self, cid):
            return []

        def overview(self, cid):
            return types.SimpleNamespace(to_json=lambda: {})

    monkeypatch.setattr(coursesite, "CourseSite", Site)
    monkeypatch.setattr(syllabus, "fetch_syllabus", lambda code, session=None: None)   # the department site is down
    sync.sync_content()
    a, b = lib.course_info("a"), lib.course_info("b")
    assert a["syllabus"]["weeks"][0]["subject"] == "Midterm"
    left = sync.SYLLABUS_EVERY - (dt.datetime.now() - dt.datetime.fromisoformat(a["syllabus_at"]))
    assert dt.timedelta(hours=23) < left <= dt.timedelta(days=1)
    assert b["syllabus"] == {} and b["syllabus_at"]                 # none stored: "no syllabus" is kept for a week
