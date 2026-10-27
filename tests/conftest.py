import os
import sys

import pytest

_ended: dict[str, int] = {}


def pytest_sessionfinish(session, exitstatus):
    _ended["status"] = int(exitstatus)


@pytest.hookimpl(trylast=True)
def pytest_unconfigure(config):
    """The run ends here, with the tests' result, without waiting for what thousands of tests leave behind (the
    services' daemon threads, thread pools, native libraries): now and then Python's own shutdown hung for minutes
    after every test had passed, and a CI job would then run until GitHub stops it."""
    if "status" in _ended:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(_ended["status"])


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, tmp_path_factory, monkeypatch):
    """Every test gets its own app folder (never the real %LOCALAPPDATA%\\IEUStudia), with the first profile active,
    and its own temp folder (so what the app leaves there, such as its instance file, goes with pytest's files).
    Windows' certificates stay out of the test run: once in, they would stay for every later test."""
    import tempfile

    from studia import i18n, net, paths

    monkeypatch.setenv("STUDIA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("STUDIA_LANG", "en")          # the tests read English, also on a Turkish Windows
    i18n.set_language("en")
    if sys.platform != "win32":                  # Linux: a fixed key instead of the desktop keyring (none on CI)
        from studia import linux_secrets

        monkeypatch.setattr(linux_secrets, "_key", lambda: b"k" * 32)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path_factory.mktemp("temp")))
    monkeypatch.setattr(net, "use_windows_certificates", lambda: None)
    paths.set_profile_dir("")
    yield tmp_path / "home"
    paths.set_profile_dir("")
