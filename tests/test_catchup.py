"""Full sync / course sync: which lectures and materials it queues (own section, newest first, nothing twice)."""

import datetime as dt

from studia.catchup import course_needs, queue_order, summary
from studia.config import CoursePrefs, Settings
from studia.coursesite import Resource, Unit
from studia.domain import Course, Recording
from studia.library import Library


def _rec(did, course, day, name="Lecture", duration=3000, hour=9):
    return Recording(delivery_id=did, course_id=course, folder_id="f", folder_path="", name=name,
                     start=dt.datetime(2026, 10, day, hour, 0), duration_s=duration, viewer_url=f"https://v/{did}")


def _lib(tmp_path):
    lib = Library(tmp_path / "lib.db")
    lib.upsert_courses([Course(id="a", title="2026-1 MATH 240 P 1", code="MATH 240", name="Probability", section="1"),
                        Course(id="b", title="2026-1 EEE 215 0 - 1", code="EEE 215", name="Circuits", section="0 - 1")])
    lib.merge_recordings("a", [_rec("a1", "a", 5), _rec("a2", "a", 12), _rec("a3", "a", 19, duration=60),   # too short
                               _rec("a4", "a", 14)], first_sync=True)
    lib.merge_recordings("b", [_rec("b1", "b", 6, "EEE 215 Section 1"), _rec("b2", "b", 6, "EEE 215 Section 3", hour=13),
                               _rec("b3", "b", 13, "EEE 215 Section 1"), _rec("b4", "b", 13, "EEE 215 Section 3", hour=13)],
                         first_sync=True)
    lib.set_status("a4", "done")
    lib.merge_units("a", [Unit("u1", 0, "Hafta 1", dt.date(2026, 10, 5), dt.date(2026, 10, 11), "",
                               [Resource("m1", "Week01", "Week01.pdf", "pdf", "pdf", "u", "d"),
                                Resource("m2", "Week01b", "Week01b.pdf", "pdf", "pdf", "u", "d")])], first_sync=True)
    (tmp_path / "w.pdf").write_bytes(b"%PDF")
    lib.set_material_file("m2", str(tmp_path / "w.pdf"), 4, "u")
    return lib


def test_own_section_newest_first_and_skips(tmp_path):
    lib = _lib(tmp_path)
    s = Settings(min_duration_s=300)
    needs = {n.code: n for n in course_needs(lib, s)}
    a, b = needs["MATH 240"], needs["EEE 215"]
    assert [d for d, _ in a.lectures] == ["a2", "a1"]              # done, too short: skipped; newest first
    assert a.materials == ["m1"] and a.audio_s == 6000             # m2 is downloaded
    assert b.needs_section and b.waiting == 4 and b.lectures == []   # several sections, yours unknown: held back
    s.course_prefs["b"] = CoursePrefs(my_section="3")
    b = course_needs(lib, s, ["b"])[0]
    assert [d for d, _ in b.lectures] == ["b4", "b2"] and b.other_sections == 2 and not b.needs_section
    order = queue_order(course_needs(lib, s))
    assert order == ["b4", "a2", "b2", "a1"]                       # across courses, newest first
    assert course_needs(lib, s, ["a"], busy={"a2"})[0].lectures[0][0] == "a1"   # already queued: not twice


def test_summary_estimates(tmp_path):
    lib = _lib(tmp_path)
    s = Settings(min_duration_s=300)
    out = summary(course_needs(lib, s), gpu=True, model="large-v3")
    assert out["lectures"] == 2 and out["audio_s"] == 6000 and out["materials"] == 1
    assert out["work_s"] == 500 and out["needs_section"] == ["EEE 215"]
    assert summary(course_needs(lib, s), gpu=False, model="large-v3-turbo")["work_s"] == 6000


def test_course_counts_follow_your_section_and_heads_ups_from_others_are_labelled(tmp_path):
    from studia import views

    lib = _lib(tmp_path)
    s = Settings(min_duration_s=300)
    s.course_prefs["b"] = CoursePrefs(my_section="3")
    lib.set_status("b1", "done")                  # a section 1 lecture someone transcribed
    v = next(c for c in views.all_course_views(lib, s, []) if c["id"] == "b")
    assert v["counts"]["total"] == 2 and v["counts"]["done"] == 0                  # yours: b2, b4
    assert v["counts"]["done_any"] == 1 and v["counts"]["other_sections"] == 2
    s.course_prefs["b"] = CoursePrefs(my_section="3", show_mode="all")
    v = next(c for c in views.all_course_views(lib, s, []) if c["id"] == "b")
    assert v["counts"]["total"] == 4 and v["counts"]["done"] == 1                  # showing every section
    days = next(c for c in views.all_course_views(lib, Settings(min_duration_s=300, course_prefs={"b": CoursePrefs(my_section="3")}), [])
                if c["id"] == "b")["days"]
    recs = [r for d in days for r in d["recordings"]]
    alerts = {"items": [{"viewer_url": r["viewer_url"], "source": "lecture"} for r in recs] + [{"source": "announcement"}]}
    labelled = views._label_other_sections(alerts, days)
    label = {it.get("viewer_url"): it.get("other_section") for it in labelled["items"]}
    assert {r["id"]: label[r["viewer_url"]] for r in recs} == {"b1": "Section 1", "b2": None, "b3": "Section 1", "b4": None}
    assert "other_section" not in alerts["items"][0]                               # the cached original is untouched


def test_quiet_sync_and_new_recordings_of_your_section(isolated_home):
    from studia.api import Api
    from studia.services import Services

    api = Api(Services())
    events = []
    api._push = events.append
    assert api._sync_lock.acquire(blocking=False)
    assert api._run_sync(quiet=True) is False                       # not signed in
    assert events[-1]["phase"] == "error" and events[-1]["quiet"] and events[-1]["need_login"]
    assert all(e.get("quiet") for e in events if e["type"] == "sync")
    assert not api._sync_lock.locked()
    course_views = [
        {"show_mode": "mine", "available_sections": ["1", "3"], "days": [{"recordings": [
            {"id": "a", "duplicate": False, "mine": True}, {"id": "b", "duplicate": False, "mine": False},
            {"id": "c", "duplicate": True, "mine": True}, {"id": "u", "duplicate": False, "mine": None}]}]},
        {"show_mode": "all", "available_sections": ["1", "3"], "days": [{"recordings": [
            {"id": "d", "duplicate": False, "mine": False}]}]}]
    assert Api._own_new(course_views, ["a", "b", "c", "d", "u", "x"]) == 3   # a, u (unknown: shown) and d (all shown)


def test_calendar_file_of_the_announced_assessments(tmp_path):
    import datetime as dt

    from studia.calendar_export import _fold, ics

    events = [{"uid": "c-quiz-2", "course": "MATH 240", "name": "Quiz 2", "start": "2026-10-19", "end": "2026-10-25",
               "week": 5, "notes": ["Week 5 (Mean, Variance; the named distributions)", "Covers LO3: Bayes' theorem"]}]
    text = ics(events, dt.datetime(2026, 10, 22, 9, 0, tzinfo=dt.timezone.utc))
    assert text.startswith("BEGIN:VCALENDAR\r\n") and text.endswith("END:VCALENDAR\r\n")
    assert "DTSTART;VALUE=DATE:20261019\r\n" in text and "DTEND;VALUE=DATE:20261026\r\n" in text   # exclusive end
    assert "UID:c-quiz-2@ieustudia" in text and "TRIGGER:-P2D" in text and "SUMMARY:MATH 240 · Quiz 2" in text
    assert "Variance\\; the named distributions" in text.replace("\r\n ", "")                      # escaped, unfolded
    assert all(len(line.encode("utf-8")) <= 75 for line in text.split("\r\n"))
    assert "".join(p[1:] if i else p for i, p in enumerate(_fold("x" * 200))) == "x" * 200


def test_calendar_events_from_the_assessment_plan(tmp_path, monkeypatch):
    from studia import calendar_export, views

    lib = _lib(tmp_path)
    detail = {"code": "MATH 240", "outcomes_en": ["count", "Bayes"],
              "weeks": [{"no": 5, "start": "2026-10-19", "end": "2026-10-25", "topic_en": "Bayes", "topic_tr": "", "exam": False, "exam_label": ""},
                        {"no": 8, "start": "2026-11-09", "end": "2026-11-15", "topic_en": "Midterm", "topic_tr": "", "exam": True, "exam_label": "Midterm"},
                        {"no": 9, "start": "", "end": "", "topic_en": "", "topic_tr": "", "exam": False, "exam_label": ""}],
              "alerts": {"plan": [{"kind": "quiz", "no": 2, "name": "Quiz 2", "week": 5, "los": [2], "sources": [{"title": "About quizzes"}]},
                                  {"kind": "quiz", "no": 3, "name": "Quiz 3", "week": 9, "los": [], "sources": []},
                                  {"kind": "quiz", "no": 4, "name": "Quiz 4", "week": None, "los": [], "sources": []}]}}
    monkeypatch.setattr(views, "course_detail", lambda lib, s, cid: detail if cid == "a" else None)
    import datetime as dt

    assert calendar_export.assessment_events(lib, Settings(), dt.date(2026, 11, 16)) == []       # all behind us
    evs = calendar_export.assessment_events(lib, Settings(), dt.date(2026, 10, 25))
    assert [(e["name"], e["start"], e["end"]) for e in evs] == [("Quiz 2", "2026-10-19", "2026-10-25"),
                                                               ("Midterm", "2026-11-09", "2026-11-15")]
    assert "Covers LO2: Bayes" in evs[0]["notes"] and evs[1]["uid"] == "a-syllabus-week8"   # week 9 has no dates
