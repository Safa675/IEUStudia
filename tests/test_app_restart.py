"""Switching profiles from the window: the app closes its window and starts a fresh copy for the other account
(the GUI toolkit is replaced by a fake, so this runs without a window)."""

import subprocess
import sys
import threading
import types

import pytest

from studia import accounts


class _Handlers(list):
    def __iadd__(self, fn):
        self.append(fn)
        return self


class FakeWindow:
    def __init__(self, api):
        self.api = api
        self.events = types.SimpleNamespace(shown=_Handlers(), loaded=_Handlers(), closing=_Handlers())
        self.closed = threading.Event()
        self.closing_result = None

    def destroy(self):
        self.closing_result = [fn() for fn in self.events.closing]
        self.closed.set()


def test_switching_profiles_restarts_into_the_other_account(isolated_home, monkeypatch):
    from studia import app, winutil

    other = accounts.create("bob", "Bob B", "2024")
    seen = {}
    fake = types.ModuleType("webview")

    def create_window(title, url=None, js_api=None, **kw):
        seen["window"] = FakeWindow(js_api)
        return seen["window"]

    def start(**kw):
        w = seen["window"]
        api = w.api
        assert api._ready.wait(30)
        seen["switch"] = api.profile_switch(other.id)
        assert w.closed.wait(10), "the window was not closed for the restart"

    fake.create_window, fake.start = create_window, start
    monkeypatch.setitem(sys.modules, "webview", fake)
    monkeypatch.setattr(winutil, "acquire_single_instance", lambda *a, **k: True)
    monkeypatch.setattr(app, "acquire_single_instance", lambda *a, **k: True)
    monkeypatch.setattr(app, "set_app_id", lambda *a, **k: None)
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: calls.append(cmd))
    app._main(["--debug"])
    assert seen["switch"]["ok"] and accounts.load()[0] == other.id
    assert seen["window"].closing_result == [True]                       # no "transcription running" question
    restarts = [c for c in calls if c[1:3] == ["-m", "studia.app"]]
    splashes = [c for c in calls if any(str(x).endswith("splash.py") for x in c)]
    assert len(restarts) == 1 and restarts[0][3:] == ["--debug", "--restarted"]
    assert len(splashes) == 1 and splashes[0][1:3] == ["-I", "-S"] and splashes[0][5] in ("light", "dark")
    assert splashes[0][6:8] == ["--python", restarts[0][0]]             # a restart from it uses the app's Python


@pytest.mark.parametrize("area,size", [((1440, 852), (1280, 820, False)),           # fits: the usual size
                                       ((1366, 720), (1280, 704, True)),            # 1366 x 768: maximized
                                       ((1280, 672), (1264, 656, True)),            # 1920 x 1080 at 150 %
                                       ((0, 0), (1280, 820, False))])               # unknown
def test_the_window_fits_the_screen(monkeypatch, area, size):
    from studia import app, winutil

    monkeypatch.setattr(winutil, "work_area", lambda: area)
    assert app._window_size() == size
