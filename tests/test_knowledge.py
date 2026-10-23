"""Course knowledge for Claude: the course brief and per-question retrieval from course files and other lectures."""

import datetime as dt

from studia import store
from studia.config import Settings
from studia.coursesite import Announcement, Resource, Unit
from studia.domain import Course, Recording, Segment, Transcript
from studia.knowledge import course_brief, retrieve
from studia.library import Library


def _lib(tmp_path):
    lib = Library(tmp_path / "lib.db")
    lib.upsert_courses([Course(id="c", title="2026-1 MATH 240 P 1", code="MATH 240", name="Probability", instructor="Dr X")])
    lib.merge_units("c", [Unit("u1", 0, "Hafta 1", dt.date(2026, 9, 21), dt.date(2026, 9, 27), "",
                               [Resource("r1", "Week01", "Week01_Student.pdf", "pdf", "pdf", "u", "d")])], first_sync=True)
    lib.set_material_file("r1", str(tmp_path / "w.pdf"), 1, "u")
    lib.set_material_text("r1", ["Sample spaces and events", "Bayes theorem: the posterior is proportional to likelihood times prior"])
    lib.set_course_info("c", syllabus={"outcomes": ["count outcomes", "apply Bayes' theorem"],
                                       "grading": [{"activity": "Quizzes", "number": 4, "weight": 20}],
                                       "weeks": [{"no": 1, "subject": "Sample spaces", "preparation": ""}]})
    lib.merge_announcements("c", [Announcement("a1", "About Quiz 1", "Dr X", dt.datetime(2026, 9, 25, 9, 0),
                                               "Quiz 1 is in week 3 and covers LO1–LO2.\n\n-----\n\n**Good luck**")],
                            first_sync=True)
    recs = []
    for i, (d, text) in enumerate([(23, "Today we start with sample spaces."), (30, "Bayes theorem flips a conditional probability.")]):
        r = Recording(delivery_id=f"r{i}", course_id="c", folder_id="f", folder_path="", name="n",
                      start=dt.datetime(2026, 9, d, 9, 0), duration_s=3000, viewer_url="v")
        lib.merge_recordings("c", [r], first_sync=True)
        t = Transcript(delivery_id=r.delivery_id, segments=[Segment(0, 5, text, "en", 0.9)], duration_s=3000, speech_s=5,
                       language_seconds={"en": 5}, model="m", device="cpu", engine_version="t", created_at="x")
        lib.save_transcript(t, store.save_transcript(t))
        recs.append(r)
    return lib, recs


def test_course_brief(tmp_path):
    lib, _ = _lib(tmp_path)
    brief = course_brief(lib, Settings(), "c")
    assert "LO2: apply Bayes' theorem" in brief and "Quizzes ×4 20%" in brief and "Week 1 (2026-09-21 to 2026-09-27)" in brief
    assert "About Quiz 1: Quiz 1 is in week 3 and covers LO1–LO2. Good luck" in brief and "-----" not in brief


def test_retrieve_finds_files_and_other_lectures(tmp_path):
    lib, recs = _lib(tmp_path)
    text, sources = retrieve(lib, "c", "How does Bayes theorem work?")
    assert "[Week01_Student.pdf p.2] Bayes theorem: the posterior" in text
    assert any(s["kind"] == "lecture" and s["day_key"] == recs[1].day_key for s in sources)
    text, sources = retrieve(lib, "c", "How does Bayes theorem work?", skip_days=[recs[1].day_key])   # already in the chat
    assert all(s["kind"] == "file" for s in sources)
    assert retrieve(lib, "c", "what is the the of") == ("", [])


def test_weekly_digest(tmp_path):
    from studia.digest import digest_markdown, digest_prompt, weekly_digest

    lib, recs = _lib(tmp_path)
    lib.set_status("r1", "done")
    lib.merge_announcements("c", [Announcement("a2", "Quiz 1 next week", "Dr X", dt.datetime(2026, 9, 29, 9, 0),
                                               "Quiz 1 is in week 3 and covers LO1–LO2.")], first_sync=False)
    d = weekly_digest(lib, Settings(), today=dt.date(2026, 10, 1))
    assert d["week"]["start"] == "2026-09-28" and d["week"]["end"] == "2026-10-04"
    (c,) = d["courses"]
    assert [lec["date"] for lec in c["lectures"]] == ["2026-09-30"] and c["announcements"][0]["title"] == "Quiz 1 next week"
    assert any(e["name"] == "Quiz 1" for e in c["coming"])
    text = digest_prompt(d)
    assert "COURSE MATH 240" in text and "lecture" in text
    md = digest_markdown(d, "A calm week.")
    assert md.startswith("# Weekly digest") and "A calm week." in md and "## MATH 240" in md


def test_a_quiz_is_done_in_the_digest_once_its_class_is_over(tmp_path):
    from studia.digest import digest_prompt, weekly_digest

    lib, _ = _lib(tmp_path)                                   # Wednesday lectures, 09:00-09:50; Quiz 1 in week 3
    before = weekly_digest(lib, Settings(), now=dt.datetime(2026, 10, 7, 8, 0))
    (quiz,) = [e for e in before["courses"][0]["coming"] if e["name"] == "Quiz 1"]
    assert quiz["dates"] == ["2026-10-07"] and quiz["this_week"] and not quiz["done"]
    assert any(t["kind"] == "prepare" and "Quiz 1 is this week" in t["text"] for t in before["todo"])
    after = weekly_digest(lib, Settings(), now=dt.datetime(2026, 10, 9, 18, 0))
    (quiz,) = [e for e in after["courses"][0]["coming"] if e["name"] == "Quiz 1"]
    assert quiz["done"] and not any(t["kind"] == "prepare" for t in after["todo"])
    assert "done: Quiz 1 in week 3" in digest_prompt(after)
    nothing = weekly_digest(lib, Settings(), now=dt.datetime(2026, 10, 12, 9, 0))   # the week after: gone
    assert all(e["name"] != "Quiz 1" for c in nothing["courses"] for e in c["coming"])
