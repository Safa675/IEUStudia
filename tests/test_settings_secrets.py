import json

from studia import secrets_store
from studia.config import apply_patch, load_settings, save_settings
from studia.paths import secrets_path, settings_path


def test_settings_roundtrip_and_tolerance():
    s = load_settings()
    assert s.model == "large-v3-turbo" and s.output_dir
    s = apply_patch(s, {"theme": "dark", "course_prefs": {"19226": {"show_mode": "all", "my_section": "1"}}})
    save_settings(s)
    raw = json.loads(settings_path().read_text(encoding="utf-8"))
    raw["unknown_future_key"] = 1
    raw["parallel_downloads"] = 99
    settings_path().write_text(json.dumps(raw), encoding="utf-8")
    s2 = load_settings()
    assert s2.theme == "dark" and s2.prefs("19226").my_section == "1" and s2.parallel_downloads == 4


def test_dpapi_secrets_roundtrip():
    secrets_store.set_credentials("student", "pässwörd!")
    secrets_store.save_cookies([{"name": "PHPSESSID", "value": "v", "domain": "learn.ieu.edu.tr", "path": "/"}])
    assert b"student" not in secrets_path().read_bytes()          # encrypted at rest
    assert secrets_store.get_credentials() == ("student", "pässwörd!")
    assert secrets_store.load_cookies()[0]["name"] == "PHPSESSID"
    secrets_store.clear_credentials()
    assert secrets_store.get_credentials() is None
    secrets_path().write_bytes(b"garbage")
    assert secrets_store.load_cookies() == []                     # corrupt file -> empty, no crash


def test_can_resume_needs_a_password_or_session(tmp_path):
    from studia.library import Library
    from studia.sync import SyncService

    sync = SyncService(Library(tmp_path / "lib.db"), load_settings)
    assert not sync.can_resume()                                   # e.g. a library copied from elsewhere
    secrets_store.save_cookies([{"name": "PHPSESSID", "value": "v", "domain": "learn.ieu.edu.tr", "path": "/"}])
    assert sync.can_resume()
    secrets_store.clear_all()
    secrets_store.set_credentials("student", "pw")
    assert sync.can_resume()


def test_the_app_and_its_helper_take_turns_with_the_secrets():
    """Two processes (the window and the tray helper) must not overwrite each other's secrets.bin."""
    import os
    import subprocess
    import sys
    import time

    hold = "import time, studia.secrets_store as s\nwith s._turn():\n    print('held', flush=True)\n    time.sleep(1.5)\n"
    other = subprocess.Popen([sys.executable, "-c", hold], stdout=subprocess.PIPE, env=dict(os.environ))
    try:
        assert other.stdout.readline().strip() == b"held"
        started = time.monotonic()
        secrets_store.set_credentials("student", "new-password")       # waits for the other process
        assert time.monotonic() - started > 0.8
    finally:
        other.wait(10)
    assert secrets_store.get_credentials() == ("student", "new-password")
    assert not list(secrets_path().parent.glob("secrets.bin.*tmp"))
