"""Profiles: several IEULearn accounts on one PC, each with its own data; adding, switching, removing."""

import ctypes
import json
import subprocess
import sys
import threading
import time

import pytest

from studia import accounts, paths, secrets_store
from studia.config import Settings, load_settings
from studia.domain import Course, Profile
from studia.library import Library


def test_first_profile_is_the_app_folder(isolated_home):
    active, accs = accounts.load()
    assert active == "main" and [a.id for a in accs] == ["main"] and accs[0].path == isolated_home
    accounts.activate()
    assert paths.db_path() == isolated_home / "library.db" and paths.transcripts_dir() == isolated_home / "transcripts"


def test_added_profile_has_its_own_data(isolated_home):
    acc = accounts.create("ayse.yilmaz", "Ayşe Yılmaz", "20240001")
    assert acc.dir == f"profiles/{acc.id}" and acc.path.is_dir()
    accounts.activate(acc.id)
    assert paths.profile_dir() == isolated_home / "profiles" / acc.id
    for p in (paths.db_path(), paths.settings_path(), paths.secrets_path(), paths.transcripts_dir()):
        assert acc.path in p.parents or p.parent == acc.path
    assert paths.default_output_dir() == acc.path / "output"
    accounts.activate("main")
    assert paths.db_path() == isolated_home / "library.db"


def test_identity_switching_and_removal(isolated_home):
    accounts.remember("main", "ali.v", "Ali V", "2023")
    acc = accounts.create("Bob.B", "Bob B", "2024")
    assert accounts.find("bob.b").id == acc.id and accounts.find("ALI.V").id == "main" and accounts.find("nobody") is None
    accounts.set_active(acc.id)
    assert accounts.active().id == acc.id and accounts.active().last_used
    with pytest.raises(ValueError):
        accounts.remove(acc.id)                       # active
    with pytest.raises(ValueError):
        accounts.remove("main")
    with pytest.raises(KeyError):
        accounts.set_active("p-00000000")
    accounts.set_active("main")
    (acc.path / "library.db").write_text("x")
    accounts.remove(acc.id)
    assert not acc.path.exists() and [a.id for a in accounts.load()[1]] == ["main"]


def test_registry_paths_cannot_point_elsewhere(isolated_home):
    paths.app_dir()
    (isolated_home / "profiles.json").write_text(json.dumps({"active": "p-12345678", "accounts": [
        {"id": "p-12345678", "dir": "..\\..\\Windows", "username": "x"}, {"id": "../evil", "dir": "C:/"}]}))
    active, accs = accounts.load()
    assert active == "p-12345678" and [a.id for a in accs] == ["main", "p-12345678"]
    assert accs[1].path == isolated_home / "profiles" / "p-12345678"


def test_seed_copies_machine_choices_not_course_choices(isolated_home):
    base = Settings(model="large-v3", theme="dark", first_run_done=True, output_dir="C:/somewhere",
                    course_prefs={"1": __import__("studia.config", fromlist=["CoursePrefs"]).CoursePrefs(alias="x")})
    acc = accounts.create("bob", "Bob B", "2024")
    accounts.seed(acc, Profile(name="Bob B", student_number="2024", username="bob"),
                  [Course(id="c1", title="2026-1 MATH 240 P 1", code="MATH 240", name="P")], base)
    accounts.activate(acc.id)
    s = load_settings()
    assert (s.model, s.theme, s.first_run_done, s.course_prefs) == ("large-v3", "dark", False, {})
    assert s.output_dir == str(acc.path / "output")
    lib = Library(paths.db_path())
    assert [c.code for c in lib.courses()] == ["MATH 240"] and json.loads(lib.get_meta("profile"))["username"] == "bob"
    lib.close()


@pytest.fixture
def api(isolated_home, monkeypatch):
    from studia.api import Api
    from studia.services import Services
    from studia.sync import SyncService

    def check(username, password):
        if password != "right":
            from studia.ieulearn import AuthError
            raise AuthError("Wrong username or password.")
        return (Profile(name=username.title(), student_number="2024", username=username),
                [Course(id="c9", title="2026-1 EEE 215 X 1", code="EEE 215", name="X")], [{"name": "sid", "value": "1"}])

    monkeypatch.setattr(SyncService, "check_account", staticmethod(check))
    accounts.remember("main", "ali.v", "Ali V", "2023")
    a = Api(Services())
    yield a
    a._s.shutdown()


def test_add_switch_and_remove_profiles_through_the_api(api, isolated_home):
    assert api.profile_add("bob", "wrong")["ok"] is False
    assert api.profile_add("ali.v", "right")["error"].startswith("That is the profile")
    r = api.profile_add("bob", "right")
    assert r["ok"] and r["created"] and r["profile"]["name"] == "Bob" and r["profile"]["has_login"]
    bob = accounts.get(r["profile"]["id"])
    assert secrets_store._load(bob.path)["password"] == "right"
    assert api.profile_add("BOB", "right")["created"] is False                  # same person again: same profile
    views = api.profiles()["profiles"]
    assert [p["active"] for p in views] == [True, False]
    sw = api.profile_switch(bob.id)
    assert sw["ok"] and api._restart and accounts.load()[0] == bob.id          # the window closes, the app restarts
    api._restart = False
    accounts.set_active("main")
    assert api.profile_remove(bob.id)["ok"] and accounts.get(bob.id) is None


def test_signing_in_as_someone_else_creates_their_profile(api):
    r = api.sign_in("alice", "right")
    assert r["ok"] and r["switch_to"] and accounts.get(r["switch_to"]).username == "alice"
    assert accounts.load()[0] == "main"                                         # switching is the UI's next step


def test_switch_refuses_while_transcribing(api, monkeypatch):
    acc = accounts.create("bob", "Bob", "1")
    monkeypatch.setattr(api._s.jobs, "busy", lambda: True)
    cancelled = []
    monkeypatch.setattr(api._s.jobs, "cancel", lambda job=None: cancelled.append(job))
    r = api.profile_switch(acc.id)
    assert r["ok"] is False and r["busy"] and accounts.load()[0] == "main"
    assert api.profile_switch(acc.id, True)["ok"] and cancelled == [None]
    api._restart = False


def test_restart_command(monkeypatch, tmp_path):
    from studia import app

    (tmp_path / "python.exe").write_text("")
    (tmp_path / "pythonw.exe").write_text("")
    calls = []
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python.exe"))
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: calls.append(cmd))
    app._relaunch(["--debug", "--restarted"])
    assert calls == [[str(tmp_path / "pythonw.exe"), "-m", "studia.app", "--debug", "--restarted"]]


def test_single_instance_waits_for_a_closing_copy():
    from studia import winutil

    name = f"Local\\Studia-Test-{time.time_ns()}"
    assert winutil.acquire_single_instance(name)
    first = winutil._mutex
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    assert not winutil.acquire_single_instance(name, wait_s=0.3)
    kernel32.CloseHandle(winutil._mutex)
    threading.Timer(0.4, lambda: kernel32.CloseHandle(first)).start()
    t0 = time.monotonic()
    assert winutil.acquire_single_instance(name, wait_s=5)
    assert 0.3 < time.monotonic() - t0 < 4
    kernel32.CloseHandle(winutil._mutex)
