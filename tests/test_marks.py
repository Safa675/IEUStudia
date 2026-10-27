"""The student's own marks in transcripts: kept in the library, shown on the paragraph and in the transcript's list,
and listed by class day for Get ready."""

import datetime as dt
import threading
import types

from studia import store
from studia.api import Api
from studia.config import Settings
from studia.domain import Course, Recording, Segment, Transcript
from studia.library import Library
from studia.views import day_detail


def _lecture(tmp_path):
    lib = Library(tmp_path / "lib.db")
    lib.upsert_courses([Course(id="c", title="t", code="MATH 240", name="P")])
    start = dt.datetime(2026, 10, 14, 8, 30)
    lib.merge_recordings("c", [Recording(delivery_id=d, course_id="c", folder_id="f", folder_path="", name=d,
                                         start=start + dt.timedelta(minutes=55 * i), duration_s=2700, viewer_url="u")
                               for i, d in enumerate(["p1", "p2"])], first_sync=True)
    for did in ("p1", "p2"):
        t = Transcript(delivery_id=did, segments=[Segment(0, 6, "So today we start Bayes' theorem.", "en", 0.9),
                                                  Segment(40, 47, "This is exactly the kind of question for the midterm.", "en", 0.9)],
                       duration_s=60, speech_s=13, language_seconds={"en": 13}, model="m", device="cpu",
                       engine_version="x", created_at="2026-10-20T10:00:00")
        lib.save_transcript(t, store.save_transcript(t))
        lib.set_status(did, "done")
    return lib


def test_a_mark_is_kept_with_its_note_and_shown_on_its_paragraph(tmp_path):
    lib = _lecture(tmp_path)
    lib.set_mark("p2", 40.04, "This is exactly the kind of question for the midterm.")
    lib.set_mark("p2", 40.0, note="ask about the denominator")          # the same mark: only the note changes
    lib.set_mark("p1", 0.0, "So today we start Bayes' theorem.")
    assert [(m["delivery_id"], m["t"], m["note"]) for m in lib.marks()] == [("p1", 0.0, ""),
                                                                          ("p2", 40.0, "ask about the denominator")]
    d = day_detail(lib, Settings(), "c|2026-10-14|lecture", {})
    (p1, p2) = d["parts"]
    assert p2["paragraphs"][1]["mark"] == {"note": "ask about the denominator"} and p2["paragraphs"][0]["mark"] is None
    assert [(m["part"], m["ts"], m["note"]) for m in d["marks"]] == [(1, "00:00", ""), (2, "00:40", "ask about the denominator")]
    assert [(h["part"], h["ts"]) for h in d["highlights"]] == [(1, "00:40"), (2, "00:40")]     # the lecturer's, as before
    lib.delete_mark("p1", 0)
    assert [m["delivery_id"] for m in lib.marks(["p1", "p2"])] == ["p2"] and lib.marks([]) == []


def test_get_ready_gets_the_marks_of_a_course_by_class_day(tmp_path):
    lib = _lecture(tmp_path)
    lib.set_mark("p2", 40, "This is exactly the kind of question for the midterm.", "midterm!")
    ns = types.SimpleNamespace(_s=types.SimpleNamespace(lib=lib), _ready=threading.Event())
    res = Api.course_marks.__wrapped__(ns, "c")
    assert res["marks"] == [{"day_key": "c|2026-10-14|lecture", "date": "2026-10-14", "part": 2, "t": 40.0, "ts": "00:40",
                             "text": "This is exactly the kind of question for the midterm.", "note": "midterm!"}]
