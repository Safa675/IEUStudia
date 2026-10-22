import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Every test gets its own app folder (never the real %LOCALAPPDATA%\\Studia), with the first profile active."""
    from studia import paths

    monkeypatch.setenv("STUDIA_HOME", str(tmp_path / "home"))
    paths.set_profile_dir("")
    yield tmp_path / "home"
    paths.set_profile_dir("")
