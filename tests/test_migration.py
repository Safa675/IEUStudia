"""LectureScribe -> Studia: the data folder and the default output folder are moved once, nothing is lost."""

import json

from studia import paths


def _real_mode(tmp_path, monkeypatch):
    monkeypatch.delenv("STUDIA_HOME", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(paths, "documents_dir", lambda: tmp_path / "docs")
    paths._legacy_in_use.cache_clear()


def test_data_folder_is_moved_once(tmp_path, monkeypatch):
    _real_mode(tmp_path, monkeypatch)
    old = tmp_path / "local" / "LectureScribe"
    (old / "transcripts").mkdir(parents=True)
    (old / "library.db").write_text("db")
    (old / "transcripts" / "x.json").write_text("{}")
    assert paths.app_dir() == old                       # not moved yet (old app running?): keep using it
    assert paths.migrate_legacy_data() == "moved"
    new = tmp_path / "local" / "Studia"
    assert (new / "library.db").read_text() == "db" and (new / "transcripts" / "x.json").exists()
    assert not old.exists() and paths.app_dir() == new
    assert paths.migrate_legacy_data() == "none"


def test_data_folder_merges_into_a_new_folder_without_library(tmp_path, monkeypatch):
    _real_mode(tmp_path, monkeypatch)
    old, new = tmp_path / "local" / "LectureScribe", tmp_path / "local" / "Studia"
    old.mkdir(parents=True)
    (old / "library.db").write_text("db")
    (new / "logs").mkdir(parents=True)                  # e.g. created by an earlier start
    assert paths.migrate_legacy_data() == "moved"
    assert (new / "library.db").exists() and (new / "logs").is_dir() and not old.exists()


def test_default_output_folder_is_renamed_and_slides_follow(tmp_path, monkeypatch):
    _real_mode(tmp_path, monkeypatch)
    paths.migrate_legacy_data()
    old_out = tmp_path / "docs" / "LectureScribe"
    (old_out / "MATH 240" / "slides").mkdir(parents=True)
    (old_out / "MATH 240" / "slides" / "s1.jpg").write_bytes(b"jpg")
    home = paths.app_dir()
    (home / "settings.json").write_text(json.dumps({"output_dir": str(old_out)}), encoding="utf-8")
    (home / "transcripts").mkdir(exist_ok=True)
    (home / "transcripts" / "r1.slides.json").write_text(json.dumps(
        [{"index": 1, "t": 0, "end": 5, "image": "slides/s1.jpg", "path": str(old_out / "MATH 240" / "slides" / "s1.jpg")}]))
    from studia.services import Services

    s = Services()
    new_out = tmp_path / "docs" / "IEUStudia"
    assert s.settings.output_dir == str(new_out) and (new_out / "MATH 240" / "slides" / "s1.jpg").exists()
    assert not old_out.exists()
    moved = json.loads((home / "transcripts" / "r1.slides.json").read_text())[0]["path"]
    assert moved == str(new_out / "MATH 240" / "slides" / "s1.jpg")
    s.shutdown()
