"""The announced quizzes and exams as a calendar file (.ics) for Google Calendar, Outlook or a phone.

Lecturers announce assessments by week ("Quiz 2: week 5"), not by day. A quiz is held in one of the course's classes
that week, so it becomes an all-day event over those class days; a midterm or final follows the exam schedule and
spans its week. Each gets a reminder two days before. Exam weeks from the syllabus are added when no announcement
covers them. The
event ids are stable, so importing a newer file again updates the events instead of duplicating them (in apps that
support it, such as Outlook).
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Iterable

from . import views
from .config import Settings


def _course(lib: Any, settings: Settings, course: Any) -> dict[str, Any]:
    """The part of views.course_detail that assessment_events reads (the same weeks, dates and class days), without
    the heads-ups from the lectures: those read every transcript, and the plan comes from the announcements alone."""
    from .alerts import assessment_plan
    from .materials import unit_week, week_starts

    info = lib.course_info(course.id)
    ov, syl = info.get("overview") or {}, info.get("syllabus") or {}
    plan = assessment_plan(lib.announcements(course.id))
    units = lib.units(course.id)
    first = week_starts(units)
    if first is None and course.term:      # no dates in this plan: the term's calendar from the other courses
        start = lib.term_start(course.term)
        if start:
            first = dt.date.fromisoformat(start)
            first -= dt.timedelta(days=first.weekday())

    def week(n: int) -> tuple[str, str]:
        start = first + dt.timedelta(days=7 * (n - 1)) if first else None
        return (start.isoformat(), (start + dt.timedelta(days=6)).isoformat()) if start else ("", "")

    tr_topics = {w["no"]: w["topic"] for w in ov.get("weeks", [])}
    en_weeks = {w["no"]: w for w in syl.get("weeks", [])}
    dates = {n: week(n) for n in [*tr_topics, *en_weeks, *(e["week"] for e in plan if e.get("week") and first)]}
    for u in units:                        # a dated unit gives its week's dates
        n = unit_week(u, first)
        if n is not None:
            dates.setdefault(n, week(n))
            if u.get("start_date"):
                dates[n] = (u["start_date"], u.get("end_date") or dates[n][1])
    times: dict[int, str] = {}
    if first and any(e["kind"] == "quiz" for e in plan):
        cv = views.course_view(course, lib.recordings(course.id), settings, {}, {}, set())
        times = views.class_times(cv["days"], dt.date.today())
    weeks = []
    for n in sorted(dates):
        topic_en, topic_tr = (en_weeks.get(n) or {}).get("subject", ""), tr_topics.get(n, "")
        exam = next((x for x in (topic_en, topic_tr) if views.exam_topic(x)), "")
        start, end = dates[n]
        weeks.append({"no": n, "start": start, "end": end, "topic_en": topic_en, "topic_tr": topic_tr,
                      "exam": bool(exam), "exam_label": exam, "classes": views.week_classes(start, end, times)})
    return {"code": course.code, "outcomes_en": syl.get("outcomes", []), "weeks": weeks, "alerts": {"plan": plan}}


def assessment_events(lib: Any, settings: Settings, today: dt.date | None = None) -> list[dict[str, Any]]:
    """The assessments still ahead (this week included), soonest first."""
    since = (today or dt.date.today()).isoformat()
    out: list[dict[str, Any]] = []
    for c in lib.courses():
        d = _course(lib, settings, c)
        weeks = {w["no"]: w for w in d["weeks"]}
        covered: set[int] = set()
        for e in (d.get("alerts") or {}).get("plan") or []:
            w = weeks.get(e.get("week") or 0)
            if not w or not w["start"]:
                continue
            covered.add(w["no"])
            los = [f"LO{n}: {d['outcomes_en'][n - 1]}" if n <= len(d["outcomes_en"]) else f"LO{n}" for n in e["los"]]
            src = e["sources"][0]["title"] if e.get("sources") else ""
            # a quiz is held in one of the week's classes; midterms and finals follow the exam schedule
            held = (w.get("classes") or []) if e["kind"] == "quiz" else []
            classes, ends = [x["date"] for x in held], [x["end"] for x in held]
            when = (f"Probably in class on {_days(classes)}; the exact day was not announced: check IEULearn."
                    if classes else "The exact day was not announced: check IEULearn." if e["kind"] == "quiz"
                    else "The exact day and time come from the exam schedule: check IEULearn.")
            out.append({"uid": f"{c.id}-{e['kind']}-{e['no']}", "course": d["code"], "name": e["name"],
                        "course_id": c.id, "kind": e["kind"], "classes": classes, "ends": ends, "los": list(e["los"]),
                        "start": classes[0] if classes else w["start"],
                        "end": classes[-1] if classes else w["end"] or w["start"], "week": w["no"],
                        "notes": [f"Week {w['no']}" + (f" ({w['topic_en'] or w['topic_tr']})" if w["topic_en"] or w["topic_tr"] else ""),
                                  *([f"Covers {', '.join(los)}"] if los else []),
                                  *([f"As announced in “{src}” on IEULearn."] if src else []), when]})
        for w in d["weeks"]:
            if w["exam"] and w["start"] and w["no"] not in covered:
                out.append({"uid": f"{c.id}-syllabus-week{w['no']}", "course": d["code"], "name": w["exam_label"] or "Exam",
                            "course_id": c.id, "kind": "exam", "classes": [], "ends": [], "los": [],
                            "start": w["start"], "end": w["end"] or w["start"], "week": w["no"],
                            "notes": [f"Week {w['no']} in the syllabus: {w['exam_label']}.",
                                      "The exact day and time come from the exam schedule: check IEULearn."]})
    out = [e for e in out if e["end"] >= since]
    out.sort(key=lambda e: (e["start"], e["course"]))
    return out


def _days(dates: list[str]) -> str:
    """["2026-10-13", "2026-10-15"] → "Tue 13 Oct or Thu 15 Oct"."""
    from .outputs.common import MONTHS, WEEKDAYS_SHORT

    ds = [dt.date.fromisoformat(x) for x in dates]
    return " or ".join(f"{WEEKDAYS_SHORT[d.weekday()]} {d.day} {MONTHS[d.month - 1][:3]}" for d in ds)


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> list[str]:
    """RFC 5545: lines longer than 75 octets continue on the next line after a space."""
    out, cur = [], ""
    for ch in line:
        if len((cur + ch).encode("utf-8")) > 75:
            out.append(cur)
            cur = " " + ch
        else:
            cur += ch
    return out + [cur]


def ics(events: Iterable[dict[str, Any]], now: dt.datetime | None = None) -> str:
    stamp = (now or dt.datetime.now(dt.timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//IEUStudia//Assessments//EN", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", "X-WR-CALNAME:IEUStudia quizzes and exams"]
    for e in events:
        start = dt.date.fromisoformat(e["start"])
        end = dt.date.fromisoformat(e["end"]) + dt.timedelta(days=1)          # DTEND of an all-day event is exclusive
        lines += ["BEGIN:VEVENT", f"UID:{e['uid']}@ieustudia", f"DTSTAMP:{stamp}",
                  f"DTSTART;VALUE=DATE:{start:%Y%m%d}", f"DTEND;VALUE=DATE:{end:%Y%m%d}",
                  f"SUMMARY:{_esc(e['course'] + ' · ' + e['name'])}", f"DESCRIPTION:{_esc(chr(10).join(e['notes']))}",
                  "TRANSP:TRANSPARENT", "STATUS:TENTATIVE",
                  "BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_esc(e['course'] + ' ' + e['name'] + ' is in two days')}",
                  "TRIGGER:-P2D", "END:VALARM", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(part for ln in lines for part in _fold(ln)) + "\r\n"
