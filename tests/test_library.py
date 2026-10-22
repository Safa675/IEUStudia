import datetime as dt

from studia.domain import Course, Recording, Segment, Transcript
from studia.library import Library


def rec(did, minute, dur=2400.0, kind="lecture"):
    return Recording(delivery_id=did, course_id="c1", folder_id="f", folder_path="", name=f"rec {did}",
                     start=dt.datetime(2026, 10, 13, 11, 0) + dt.timedelta(minutes=minute), duration_s=dur,
                     viewer_url="u", kind=kind)


def test_merge_new_duplicates_and_empty_listing(tmp_path):
    lib = Library(tmp_path / "lib.db")
    lib.upsert_courses([Course(id="c1", title="t", code="EEE 215", name="n")])
    assert lib.merge_recordings("c1", [rec("a", 0), rec("b", 50)], first_sync=True) == []
    assert not any(r.is_new for r in lib.recordings("c1"))
    # later sync: one new recording + an exact duplicate of it
    new = lib.merge_recordings("c1", [rec("a", 0), rec("b", 50), rec("c", 100), rec("d", 100, 2400.5)], first_sync=False)
    assert new == ["c"]
    d = lib.get_recording("d")
    assert d.duplicate_of == "c" and not d.is_new
    # an empty listing (auth hiccup) must not mark everything as gone
    lib.merge_recordings("c1", [], first_sync=False)
    assert len(lib.recordings("c1")) == 4


def test_status_and_fts_search(tmp_path):
    lib = Library(tmp_path / "lib.db")
    lib.upsert_courses([Course(id="c1", title="t", code="EEE 215", name="n")])
    lib.merge_recordings("c1", [rec("a", 0)], first_sync=False)
    t = Transcript(delivery_id="a", segments=[Segment(0, 5, "The Thevenin equivalent circuit", "en", 0.9),
                                              Segment(6, 9, "Arkadaşlar bu sınavda çıkacak", "tr", 0.9)],
                   duration_s=10, speech_s=9, language_seconds={"en": 5, "tr": 3}, model="m", device="cpu",
                   engine_version="x", created_at="2026-10-20T10:00:00")
    lib.save_transcript(t, tmp_path / "a.json")
    lib.set_status("a", "done")
    assert lib.get_recording("a").status == "done" and not lib.get_recording("a").is_new
    assert lib.search("theven")[0]["delivery_id"] == "a"
    assert lib.search("sınavda")[0]["t"] == 6
    lib.set_status("a", "transcribing")
    lib.reset_interrupted()
    assert lib.get_recording("a").status == "done"   # transcript exists -> back to done
