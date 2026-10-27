import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, tmp_path_factory, monkeypatch):
    """Every test gets its own app folder (never the real %LOCALAPPDATA%\\IEUStudia), with the first profile active,
    and its own temp folder (so what the app leaves there, such as its instance file, goes with pytest's files)."""
    import tempfile

    from studia import i18n, paths

    monkeypatch.setenv("STUDIA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("STUDIA_LANG", "en")          # the tests read English, also on a Turkish Windows
    i18n.set_language("en")
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path_factory.mktemp("temp")))
    paths.set_profile_dir("")
    yield tmp_path / "home"
    paths.set_profile_dir("")
