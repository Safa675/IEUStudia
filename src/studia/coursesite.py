"""A course's own content on IEULearn: learning outcomes, weekly topics, grading, the weekly unit plan with
its materials (slides, PDFs, PowerPoints) and the announcements.

All of it is server-rendered HTML: the course page and three panels it loads lazily
(``/student/courses/<id>/overview/unit-plan``, ``…/announcements``). Read-only.
"""

from __future__ import annotations

import dataclasses as dc
import datetime as dt
import hashlib
import html
import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

import requests

from .i18n import t
from .ieulearn import BASE, NeedLogin

log = logging.getLogger("studia")

TR_MONTHS = {"oca": 1, "şub": 2, "sub": 2, "mar": 3, "nis": 4, "may": 5, "haz": 6, "tem": 7, "ağu": 8, "agu": 8,
             "eyl": 9, "eki": 10, "kas": 11, "ara": 12}


@dc.dataclass
class Outcome:
    no: int
    text: str


@dc.dataclass
class WeekTopic:
    no: int
    topic: str


@dc.dataclass
class GradeItem:
    name: str
    count: int
    weight: float          # percent of the final grade


@dc.dataclass
class Overview:
    description: str = ""
    outcomes: list[Outcome] = dc.field(default_factory=list)
    weeks: list[WeekTopic] = dc.field(default_factory=list)
    grading: list[GradeItem] = dc.field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return dc.asdict(self)

    @staticmethod
    def from_json(d: dict[str, Any]) -> "Overview":
        return Overview(d.get("description", ""), [Outcome(**o) for o in d.get("outcomes", [])],
                        [WeekTopic(**w) for w in d.get("weeks", [])], [GradeItem(**g) for g in d.get("grading", [])])


@dc.dataclass
class Resource:
    id: str
    title: str
    file_name: str = ""
    kind: str = ""           # pdf | presentation | document | link | …
    ext: str = ""
    url: str = ""
    download_url: str = ""


@dc.dataclass
class Unit:
    id: str
    position: int
    title: str
    start: Optional[dt.date] = None
    end: Optional[dt.date] = None
    readings: str = ""
    resources: list[Resource] = dc.field(default_factory=list)


@dc.dataclass
class Announcement:
    id: str
    title: str
    author: str
    posted_at: Optional[dt.datetime]
    body: str


def _text(fragment: str) -> str:
    """HTML fragment -> plain text with paragraph breaks."""
    s = re.sub(r"(?i)<br\s*/?>", "\n", fragment or "")
    s = re.sub(r"(?i)</(p|div|li|h\d|tr)>", "\n", s)
    s = html.unescape(re.sub(r"<[^>]+>", "", s))
    s = re.sub(r"[ \t ]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n\n", "\n".join(line.strip() for line in s.splitlines()))
    return s.strip()


def _inner(page: str, cls: str) -> str:
    m = re.search(rf'class="{re.escape(cls)}"[^>]*>(.*?)</', page, re.S)
    return _text(m.group(1)) if m else ""


def parse_overview(page: str) -> Overview:
    ov = Overview()
    m = re.search(r'class="ov-intro-lo-list"[^>]*>(.*?)</ol>', page, re.S)
    if m:
        for no, text in re.findall(r'class="ov-intro-lo-no"[^>]*>\s*(\d+)\.?\s*</span>\s*'
                                   r'<span class="ov-intro-lo-text"[^>]*>(.*?)</span>', m.group(1), re.S):
            ov.outcomes.append(Outcome(int(no), _text(text)))
    for no, topic in re.findall(r'class="ov-ects-week-no"[^>]*>\s*(\d+)\s*</span>\s*'
                                r'<span class="ov-ects-week-topic"[^>]*>(.*?)</span>', page, re.S):
        if not any(w.no == int(no) for w in ov.weeks):
            ov.weeks.append(WeekTopic(int(no), _text(topic)))
    for item in re.findall(r'<li class="ov-ects-assess-item"[^>]*>(.*?)</li>', page, re.S):
        name = _inner(item, "ov-ects-assess-name")
        count = re.search(r'class="ov-ects-assess-count"[^>]*>\s*(\d+)', item)
        pct = re.search(r'class="ov-ects-assess-pct"[^>]*>\s*([\d.,]+)\s*%', item)
        if name:
            ov.grading.append(GradeItem(name, int(count.group(1)) if count else 1,
                                        float(pct.group(1).replace(",", ".")) if pct else 0.0))
    d = re.search(r'class="ov-intro-paragraph"[^>]*>(.*?)</p>', page, re.S)
    ov.description = _text(d.group(1)) if d else ""
    return ov


def term_year(course_title: str) -> tuple[int, int]:
    """``"2026-1 MATH 240 …"`` -> (2026, 1): academic year 2026-27, first (fall) semester."""
    m = re.match(r"\s*(\d{4})-(\d)", course_title or "")
    if m:
        return int(m.group(1)), int(m.group(2))
    today = dt.date.today()
    return (today.year if today.month >= 8 else today.year - 1), (1 if today.month >= 8 or today.month == 1 else 2)


def _tr_date(text: str, year: int, semester: int) -> Optional[dt.date]:
    m = re.match(r"\s*(\d{1,2})\s+([A-Za-zÇĞİÖŞÜçğıöşü]{3})", text or "")
    if not m:
        return None
    key = m.group(2).lower()
    month = TR_MONTHS.get(key) or TR_MONTHS.get(key.replace("ı", "i").replace("ş", "s").replace("ğ", "g"))
    if not month:
        return None
    y = year + (1 if semester != 1 or month < 8 else 0)          # fall: Sep-Dec this year, Jan-Feb next year
    try:
        return dt.date(y, month, int(m.group(1)))
    except ValueError:
        return None


def parse_unit_plan(page: str, course_title: str = "") -> list[Unit]:
    year, sem = term_year(course_title)
    units: list[Unit] = []
    blocks = re.split(r'(?=<article class="curriculum-item)', page)
    for block in blocks:
        head = re.match(r'<article class="curriculum-item[^"]*"[^>]*data-item-id="(\d+)"', block)
        if not head:
            continue
        title = re.search(r'class="ovup-title-text"[^>]*title="([^"]*)"', block)
        title = html.unescape(title.group(1)).strip() if title else _inner(block, "item-title")
        sub = re.search(r'class="item-subtitle"[^>]*>(.*?)</div>', block, re.S)
        start = end = None
        if sub:
            parts = [p.strip() for p in _text(sub.group(1)).split("-")]
            start = _tr_date(parts[0], year, sem) if parts else None
            end = _tr_date(parts[1], year, sem) if len(parts) > 1 else None
            if start and end and end < start:          # a unit across New Year
                end = end.replace(year=end.year + 1)
        desc = re.search(r'class="ovup-syl-desc-body"[^>]*>(.*?)</div>', block, re.S)
        unit = Unit(head.group(1), len(units), title, start, end, _text(desc.group(1)) if desc else "")
        for raw in re.findall(r'data-rv-payload="([^"]+)"', block):
            try:
                p = json.loads(html.unescape(raw))
            except ValueError:
                continue
            if not p.get("id"):
                continue
            unit.resources.append(Resource(str(p["id"]), str(p.get("title") or p.get("file_name") or ""),
                                           str(p.get("file_name") or ""), str(p.get("type") or ""),
                                           str(p.get("file_ext") or "").lower(), str(p.get("url") or ""),
                                           str(p.get("download_url") or "")))
        units.append(unit)
    return units


def parse_announcements(page: str) -> list[Announcement]:
    out: list[Announcement] = []
    for card in re.findall(r'<article class="ov-ann-card[^"]*"[^>]*>(.*?)</article>', page, re.S):
        title = re.search(r'class="ov-ann-card-title"[^>]*>(.*?)</h4>', card, re.S)
        title_text = _text(re.sub(r'<span class="ov-badge-new".*?</span>', "", title.group(1), flags=re.S)) if title else ""
        meta = re.search(r'class="ov-ann-card-meta"[^>]*>(.*?)</div>', card, re.S)
        spans = [_text(s) for s in re.findall(r"<span[^>]*>(.*?)</span>", meta.group(1), re.S)] if meta else []
        author = spans[0] if spans else ""
        when = None
        for s in spans:
            m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})\s+(\d{1,2}):(\d{2})", s)
            if m:
                d, mo, y, h, mi = map(int, m.groups())
                try:
                    when = dt.datetime(y, mo, d, h, mi)
                except ValueError:
                    pass
        body = re.search(r'class="st-ov-ann-full"[^>]*>(.*)', card, re.S)
        body_text = _text(body.group(1)) if body else ""
        key = hashlib.sha1(f"{title_text}|{author}|{when}".encode("utf-8")).hexdigest()[:16]
        if title_text or body_text:
            out.append(Announcement(key, title_text, author, when, body_text))
    return out


class CourseSite:
    def __init__(self, session: requests.Session) -> None:
        self.s = session

    def _get(self, path: str) -> str:
        r = self.s.get(f"{BASE}{path}", allow_redirects=False, timeout=30)
        if r.status_code in (301, 302, 303):
            raise NeedLogin(t("Your IEULearn session has expired."))
        r.raise_for_status()
        return r.text

    def overview(self, course_id: str) -> Overview:
        return parse_overview(self._get(f"/student/courses/{course_id}"))

    def unit_plan(self, course_id: str, course_title: str = "") -> list[Unit]:
        return parse_unit_plan(self._get(f"/student/courses/{course_id}/overview/unit-plan"), course_title)

    def announcements(self, course_id: str) -> list[Announcement]:
        return parse_announcements(self._get(f"/student/courses/{course_id}/overview/announcements"))


def cover_file(url: str) -> Path:
    """Where a course's dashboard cover picture is cached (keyed by its URL, which carries a version)."""
    from .paths import app_dir

    return app_dir() / "cache" / "covers" / f"{hashlib.sha1(url.encode('utf-8')).hexdigest()[:16]}.jpg"


def fetch_cover(url: str) -> Optional[Path]:
    """Cache a cover picture (a public ~20 KB image) for the Home page; None if it isn't an image."""
    path = cover_file(url)
    if path.is_file():
        return path
    r = requests.get(url, timeout=20)
    if not r.ok or not r.headers.get("content-type", "").startswith("image/"):
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_bytes(r.content)
    tmp.replace(path)
    return path
