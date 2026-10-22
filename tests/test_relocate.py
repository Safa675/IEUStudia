import json

from studia.domain import Course
from studia.paths import transcripts_dir
from studia.services import Services


def test_relocate_outputs_copies_folders_and_rewrites_slide_paths(tmp_path):
    s = Services()
    s.lib.upsert_courses([Course(id="1", title="t", code="EEE 215", name="n")])
    old, new = tmp_path / "old", tmp_path / "new"
    slide = old / "EEE 215" / "slides" / "day" / "1119-01.jpg"
    slide.parent.mkdir(parents=True)
    slide.write_bytes(b"jpg")
    (old / "EEE 215" / "EEE 215 - day.md").write_text("x", encoding="utf-8")
    (old / "Unrelated folder").mkdir()
    idx = transcripts_dir() / "abc.slides.json"
    idx.write_text(json.dumps([{"index": 1, "t": 0, "end": 5, "image": "slides/day/1119-01.jpg", "path": str(slide)}]),
                   encoding="utf-8")
    assert s.relocate_outputs(old, new) == 1
    assert (new / "EEE 215" / "slides" / "day" / "1119-01.jpg").read_bytes() == b"jpg"
    assert not (new / "Unrelated folder").exists()            # only folders the app manages are copied
    assert slide.exists()                                      # the old folder is left untouched
    assert json.loads(idx.read_text(encoding="utf-8"))[0]["path"] == str(new / "EEE 215" / "slides" / "day" / "1119-01.jpg")
    s.lib.close()
