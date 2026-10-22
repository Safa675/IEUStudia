import json

from studia.coursesite import Resource, Unit
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


def test_relocate_outputs_points_downloads_and_digests_at_the_copies(tmp_path):
    s = Services()
    s.lib.upsert_courses([Course(id="1", title="t", code="EEE 215", name="n")])
    old, new = tmp_path / "old", tmp_path / "new"
    pdf = old / "EEE 215" / "Materials" / "Week 1" / "notes.pdf"
    digest = old / "Weekly digests" / "2026-10-19 weekly digest.md"
    stray = old / "Some other folder" / "x.pdf"              # not a folder the app copies: its path stays
    for f in (pdf, digest, stray):
        f.parent.mkdir(parents=True)
        f.write_bytes(b"x")
    s.lib.merge_units("1", [Unit("u1", 0, "Week 1", resources=[Resource("m1", "Notes"), Resource("m2", "X")])],
                      first_sync=True)
    s.lib.set_material_file("m1", str(pdf), 1, "u")
    s.lib.set_material_file("m2", str(stray), 1, "u")
    s.lib.set_meta("digest:2026-10-19", json.dumps({"text": "t", "file": str(digest)}))
    assert s.relocate_outputs(old, new) == 2
    assert s.lib.get_material("m1")["local_path"] == str(new / "EEE 215" / "Materials" / "Week 1" / "notes.pdf")
    assert s.lib.get_material("m2")["local_path"] == str(stray)
    assert json.loads(s.lib.get_meta("digest:2026-10-19"))["file"] == str(new / "Weekly digests" / digest.name)
    assert pdf.is_file() and digest.is_file()
    s.lib.close()
