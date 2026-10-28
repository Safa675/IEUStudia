"""The public English course syllabus (ECTS package) on the department sites, e.g.
https://ce.ieu.edu.tr/en/syllabus/type/read/id/MATH+240 — learning outcomes, weekly subjects with readings,
grading, lecturers, prerequisites and textbooks. No login needed.
"""

from __future__ import annotations

import dataclasses as dc
import html
import logging
import re
from typing import Any, Optional, Sequence

import requests

from .net import plain_session

log = logging.getLogger("studia")

DEPARTMENTS = ("ce", "se", "eee", "ie", "me", "ae", "fe", "math")    # first site that has the course wins


@dc.dataclass
class Syllabus:
    code: str
    url: str = ""
    name: str = ""
    semester: str = ""
    ects: str = ""
    prerequisites: str = ""
    language: str = ""
    course_type: str = ""
    coordinator: str = ""
    lecturers: str = ""
    assistants: str = ""
    objectives: str = ""
    outcomes: list[str] = dc.field(default_factory=list)
    description: str = ""
    weeks: list[dict[str, Any]] = dc.field(default_factory=list)       # {"no", "subject", "preparation"}
    grading: list[dict[str, Any]] = dc.field(default_factory=list)     # {"activity", "number", "weight"}
    textbooks: str = ""
    readings: str = ""

    def to_json(self) -> dict[str, Any]:
        return dc.asdict(self)

    @staticmethod
    def from_json(d: dict[str, Any]) -> "Syllabus":
        fields = {f.name for f in dc.fields(Syllabus)}
        return Syllabus(**{k: v for k, v in d.items() if k in fields})


def _text(fragment: str) -> str:
    t = re.sub(r"(?i)<br\s*/?>|</p>|</li>", "\n", fragment or "")
    t = re.sub(r"(?i)</t[dh]>", " ", t)                     # table cells stay apart in flattened text
    t = html.unescape(html.unescape(re.sub(r"<!--.*?-->|<[^>]+>", "", t, flags=re.S)))
    t = re.sub(r"[ \t ]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n", "\n".join(line.strip() for line in t.splitlines())).strip()


def _row(page: str, label: str) -> str:
    """The value cell next to a ``<strong>label</strong>`` cell."""
    m = re.search(rf"<strong>\s*{re.escape(label)}\s*</strong>\s*</td>\s*(?:<!--.*?-->\s*)*<td[^>]*>(.*?)</td>", page, re.S | re.I)
    return _text(m.group(1)) if m else ""


def parse_syllabus(page: str, code: str = "", url: str = "") -> Optional[Syllabus]:
    page = re.sub(r"<!--.*?-->", "", page, flags=re.S)      # the page keeps old columns in comments
    lis = re.search(r"<ul id=['\"]outcome['\"][^>]*>(.*?)</ul>", page, re.S)
    if not lis:
        return None
    s = Syllabus(code=code, url=url)
    s.outcomes = [t for t in (_text(x) for x in re.findall(r"<li[^>]*>(.*?)</li>", lis.group(1), re.S)) if t]
    name = re.search(r'id="course_name"[^>]*>(.*?)</div>', page, re.S)
    s.name = _text(name.group(1)) if name else ""
    pre = re.search(r'id="pre_requisites"[^>]*>(.*?)</div>', page, re.S)
    s.prerequisites = " ".join(_text(pre.group(1)).split()) if pre else _row(page, "Prerequisites")
    s.language = _row(page, "Course Language")
    s.course_type = _row(page, "Course Type")
    s.coordinator = _row(page, "Course Coordinator")
    s.lecturers = _row(page, "Course Lecturer(s)")
    s.assistants = _row(page, "Assistant(s)")
    s.objectives = _row(page, "Course Objectives")
    s.description = _row(page, "Course Description")
    s.textbooks = _row(page, "Course Notes/Textbooks")
    s.readings = _row(page, "Suggested Readings/Materials")
    flat = " ".join(_text(page[page.find("<strong>Code</strong>"):][:4000]).split()) if "<strong>Code</strong>" in page else ""
    m = re.search(r"Local Credits ECTS \S+ \S+ (\w+) (\d+) (\d+) (\d+) (\d+)", flat)   # code | semester | hours | credits | ECTS
    if m:
        s.semester, s.ects = m.group(1), m.group(5)
    weeks = re.search(r'<table[^>]*id="weeks"[^>]*>(.*?)</table>', page, re.S)
    if weeks:
        for no, body in re.findall(r'<tr id="hafta_(\d+)"[^>]*>(.*?)</tr>', weeks.group(1), re.S):
            cells = [_text(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", body, re.S)]
            if len(cells) >= 2:
                s.weeks.append({"no": int(no), "subject": cells[1], "preparation": cells[2] if len(cells) > 2 else ""})
    ev = re.search(r"EVALUATION SYSTEM(.*?)</table>", page, re.S)
    if ev:
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", ev.group(1), re.S):
            cells = [_text(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
            if len(cells) >= 3 and re.fullmatch(r"\d+", cells[1] or "") and re.fullmatch(r"[\d.,]+", cells[2] or ""):
                s.grading.append({"activity": cells[0], "number": int(cells[1]), "weight": float(cells[2].replace(",", "."))})
    return s


def syllabus_url(dept: str, code: str) -> str:
    return f"https://{dept}.ieu.edu.tr/en/syllabus/type/read/id/{'+'.join(code.split())}"


def fetch_syllabus(code: str, departments: Sequence[str] = DEPARTMENTS,
                   session: Optional[requests.Session] = None) -> Optional[Syllabus]:
    """The first department site with a syllabus for ``code`` (e.g. "MATH 240"), or None."""
    s = session or plain_session()
    for dept in departments:
        url = syllabus_url(dept, code)
        try:
            r = s.get(url, timeout=(6, 20), headers={"User-Agent": "Mozilla/5.0 (IEUStudia)"})
        except requests.RequestException:
            continue                       # this department has no site
        if r.status_code != 200:
            continue
        parsed = parse_syllabus(r.content.decode("utf-8", "replace"), code, url)   # served as UTF-8 without a charset
        if parsed and parsed.outcomes:
            return parsed
    return None
