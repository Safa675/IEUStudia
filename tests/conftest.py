import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, tmp_path_factory, monkeypatch):
    """Every test gets its own app folder (never the real %LOCALAPPDATA%\\IEUStudia), with the first profile active,
    and its own temp folder (so what the app leaves there, such as its instance file, goes with pytest's files)."""
    import tempfile

    from studia import paths

    monkeypatch.setenv("STUDIA_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path_factory.mktemp("temp")))
    paths.set_profile_dir("")
    yield tmp_path / "home"
    paths.set_profile_dir("")
