"""The course "study pack": every transcribed class day of a course in one compact file."""

from __future__ import annotations

import datetime as dt
from typing import Any, Sequence

from ..domain import Course, Recording
from .common import (course_display, day_label, format_duration, format_ts, kind_label, lang_tag, md_escape,
                     time_range)
from .markdown import UNCLEAR_MD, part_views

DayData = tuple[dt.date, str, Sequence[Recording], Any]   # (date, kind, recordings, transcripts)


def render_study_pack(course: Course, days: Sequence[DayData], warnings: Any = None) -> str:
    built = []
    for date, kind, recs, trans in sorted(days, key=lambda d: (d[0], d[1])):
        parts = [p for p in part_views(recs, trans, warnings=warnings) if p.transcript]
        if parts:
            built.append((date, kind, parts))
    total = sum(p.rec.duration_s for _, _, parts in built for p in parts)
    n_recs = sum(len(parts) for _, _, parts in built)
    lines = [f"# {md_escape(course_display(course))} · Study pack", ""]
    meta = []
    if course.instructor:
        meta.append(f"Lecturer: {md_escape(course.instructor)}")
    meta.append(f"{len(built)} class day{'s' if len(built) != 1 else ''} · {n_recs} recording{'s' if n_recs != 1 else ''}"
                f" · {format_duration(total)}")
    meta.append(f"Generated {dt.date.today().isoformat()} by IEUStudia")
    lines.append(" · ".join(meta))
    lines.append("")
    lines.append("Automatic transcript of the course recordings (may contain recognition errors). Timestamps are "
                 "mm:ss from the start of each part. **[TR]** = passage spoken in Turkish, ⭐ = something the "
                 "lecturer emphasised (exam, important, homework, schedule), ⚠ = unclear audio.")
    lines.append("")
    if not built:
        lines.append("_Nothing transcribed yet._")
        return "\n".join(lines) + "\n"
    lines += ["## Contents", ""]
    for i, (date, kind, parts) in enumerate(built, start=1):
        stars = sum(1 for p in parts for para in p.paragraphs if para.star)
        lines.append(f"{i}. {day_label(date)} · {kind_label(kind)} — {len(parts)} part{'s' if len(parts) != 1 else ''}"
                     + (f", {stars} ⭐" if stars else ""))
    lines.append("")
    for date, kind, parts in built:
        lines += [f"## {day_label(date)} · {kind_label(kind)}", ""]
        for p in parts:
            lines += [f"### Part {p.index} · {time_range(p.rec)}", ""]
            for para in p.paragraphs:
                tag = lang_tag(para.lang)
                marks = (f"[{tag}] " if tag else "") + ("⭐ " if para.star else "") + ("⚠ " if para.uncertain else "")
                text = md_escape(para.text).replace(r"\[unclear\]", UNCLEAR_MD)
                lines.append(f"[{format_ts(para.start)}] {marks}{text}")
                lines.append("")
    return "\n".join(lines).rstrip() + "\n"
