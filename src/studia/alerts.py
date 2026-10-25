"""What the lecturer said about quizzes, the midterm, the final, projects, homework and schedule changes (plus
other moments they stressed): found in the transcribed lectures and the IEULearn announcements of a course with
English + Turkish cue phrases, newest first. No AI involved, so it is instant and free, and every lecture item
links back to the exact moment in the transcript.
"""

from __future__ import annotations

import datetime as dt
import re
import threading
from collections import defaultdict
from typing import Any, Iterable, Optional

from . import store
from .outputs.common import format_ts
from .outputs.highlights import _tr_lower
from .outputs.markdown import part_views

# (key, label, English cues, Turkish cues — matched on Turkish-lowercased text). Order = priority of the main category.
CATEGORIES: list[tuple[str, str, str, str]] = [
    ("final", "Final",
     r"\bfinal exams?\b|\bfinals? week\b|\b(?:in|on|for|before|after|until|till) the final\b(?!\s+(?!exams?\b)[a-z])",
     r"\bfinal sınav\w*|\bfinal(?:de|da|e|den|dan|i|ı)\b"),
    ("midterm", "Midterm", r"\bmid-?terms?\b", r"\bara sınav\w*|\bvize\w*"),
    ("quiz", "Quiz", r"\b(?:pop )?quiz(?:zes)?\b", r"\bquiz\w*|\bküçük sınav\w*|\bkısa sınav\w*"),
    ("project", "Project",
     r"\b(?:your|the|a|our|term|group|final|course|semester|class|mini|team) projects?\b|\bprojects? (?:report|proposal|"
     r"deadline|presentation|submission|groups?|teams?|topics?|is due|are due|will be)\b|\bterm papers?\b|\blab reports?\b",
     r"\bproje(?:si|niz|nizi|nizin|ler|leri|yi|de|ye|nin)?\b|\brapor(?:u|unuz|unuzu|ları|larınızı)?\b"),
    ("homework", "Homework",
     r"\bhomeworks?\b|\bproblem sets?\b|\b(?:your|this|next|first|second|third|last) assignments?\b|"
     r"\bassignments? (?:is|are|will)\b|\bdue (?:date|on|next|by|tomorrow|tonight|today|this)\b|\bdeadlines?\b|"
     r"\b(?:your|late|online) submissions?\b|\bsubmissions? (?:deadline|date|link|portal|is|are|will)\b|"
     r"\bsubmit (?:it|them|your|before|by|until|via|through|online)\b",
     r"\bödev\w*|\bteslim\w*|\bson gün\w*|\bson tarih\w*"),
    ("exam", "Exam",
     r"\bexams?\b|\bexaminations?\b|\bwill be asked\b|\bi will ask\b|\bon the (?:test|exam)\b(?!\s+statistic)|"
     r"\b(?:you are|you're) (?:not )?responsible for\b|\bwill (?:not )?be (?:covered|included) (?:in|on)\b|"
     r"\blearning outcomes? \d|\bL\.?O\.? ?\d",
     r"\bsınav\w*|\bsorarım\b|\bsoracağım\b|\bsorumlusunuz\b|\bsorumlu (?:olacaksınız|değilsiniz)\b|\bbütünleme\w*"),
    ("schedule", "Schedule",
     r"\bno (?:class|lecture)\b|\b(?:class|classes|lecture|quiz|exam) (?:is|are|was|has been|will be) cancell?ed\b|"
     r"\bmake-?up (?:class|lecture|exam|quiz)\b|\breschedul\w*|\bpostpone\w*|\boffice hours?\b|\bclassroom (?:change|changed)\b",
     r"\bders yok\b|\biptal\w*|\btelafi\w*|\berteleme\w*|\bertelen\w*|\bderslik\w*"),
    ("important", "Important",
     r"\bimportant\b|\bcrucial\b|\bessential\b|\bpay attention\b|\bbe careful\b|\bdon'?t forget\b|\bdo not forget\b|"
     r"\bremember this\b|\bkeep in mind\b|\bcommon mistake\b|\bmake sure you\b",
     r"\bönemli\w*|\bdikkat\w*|\bunutmayın\w*|\bunutmayalım\b|\baklınızda\w*|\bmutlaka\b|\bkesinlikle\b"),
]
_RE = [(k, re.compile(en, re.I), re.compile(tr)) for k, _, en, tr in CATEGORIES]
_ANY_EN = re.compile("|".join(f"(?:{en})" for _, _, en, _ in CATEGORIES), re.I)   # one pass rules out most paragraphs
_ANY_TR = re.compile("|".join(f"(?:{tr})" for _, _, _, tr in CATEGORIES))
LABELS = {k: label for k, label, _, _ in CATEGORIES}
MAX_ITEMS = 120
SAME_POINT_S = 120      # the same category again within two minutes of a lecture = the same point, keep the first

_cache: dict[str, tuple[Any, dict[str, Any]]] = {}
_cache_lock = threading.Lock()


def _turkish(text: str, lang: str) -> bool:
    return lang == "tr" or any(c in text for c in "çğışöüÇĞİŞÖÜ")


def matches(text: str, lang: str = "en") -> dict[str, tuple[int, int]]:
    """{category: (start, end) of its first cue} in priority order (final … important)."""
    if not text:
        return {}
    low = _tr_lower(text) if _turkish(text, lang) else None
    if not _ANY_EN.search(text) and (low is None or not _ANY_TR.search(low)):
        return {}
    out: dict[str, tuple[int, int]] = {}
    for key, en, tr in _RE:
        m = en.search(text) or (tr.search(low) if low is not None else None)
        if m:
            out[key] = m.span()
    return out


def categories(text: str, lang: str = "en") -> list[str]:
    return list(matches(text, lang))


def _excerpt(text: str, span: tuple[int, int], limit: int) -> tuple[str, list[int]]:
    """About ``limit`` characters around the cue, snapped to a sentence or word start; returns the excerpt and
    where the cue sits inside it (for highlighting)."""
    a, b = span
    if len(text) <= limit:
        return text, [a, b]
    start = max(0, a - limit // 3)
    end = min(len(text), start + limit)
    start = max(0, end - limit)
    if start > 0:
        cut = max([text.rfind(sep, start, a) + len(sep) for sep in (". ", "? ", "! ", "\n") if text.rfind(sep, start, a) >= 0],
                  default=-1)
        if cut > 0:
            start = cut
        else:
            sp = text.find(" ", start)
            start = sp + 1 if 0 <= sp < a else start
    if end < len(text):
        sp = text.rfind(" ", b, end)
        end = sp if sp > b else end
    seg = text[start:end]
    lead = len(seg) - len(seg.lstrip())
    pre = "…" if start > 0 else ""
    out = pre + seg.strip() + ("…" if end < len(text) else "")
    off = len(pre) + (a - start - lead)
    return out, [off, off + (b - a)]


def _tidy(text: str) -> str:
    """Announcement text for an excerpt: no separator lines (-----, ____), no **bold** markers, no blank lines."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"(?m)^\s*[-_=*~·.]{3,}\s*$", "", text)
    text = re.sub(r"[-_=~]{4,}", " ", text)
    return re.sub(r"[ \t]*\n\s*", "\n", text).strip()


def _item(cats: dict[str, tuple[int, int]], text: str, limit: int, **extra: Any) -> dict[str, Any]:
    main = next(iter(cats))
    excerpt, hl = _excerpt(text, cats[main], limit)
    return {"category": main, "categories": list(cats), "text": excerpt, "hl": hl, **extra}


def _signature(lib: Any, course_id: str, recs: list[Any], done: set[str]) -> tuple[Any, list[dict[str, Any]]]:
    files = []
    for r in recs:
        if r.delivery_id in done:
            try:
                files.append((r.delivery_id, store.transcript_path(r.delivery_id).stat().st_mtime_ns))
            except OSError:
                files.append((r.delivery_id, 0))
    flags = tuple(sorted((r.delivery_id, bool(r.hidden), bool(r.duplicate_of)) for r in recs))
    anns = lib.announcements(course_id)
    return (tuple(sorted(files)), flags,
            tuple((a["id"], a["posted_at"], bool(a["is_new"]), a["title"], a["body"]) for a in anns)), anns


def course_alerts(lib: Any, course_id: str, recordings: Optional[Iterable[Any]] = None) -> dict[str, Any]:
    """{"items": [...newest first], "counts": {category: n}, "total": n} for one course (cached until a transcript
    or an announcement changes)."""
    recs = list(recordings if recordings is not None else lib.recordings(course_id))
    done = lib.transcribed_ids()
    sig, anns = _signature(lib, course_id, recs, done)
    with _cache_lock:
        hit = _cache.get(course_id)
        if hit and hit[0] == sig:
            return hit[1]
    by_day: dict[str, list[Any]] = defaultdict(list)
    for r in recs:
        by_day[r.day_key].append(r)
    items: list[dict[str, Any]] = []
    for key, day_recs in by_day.items():
        ids = [r.delivery_id for r in day_recs if r.delivery_id in done]
        if not ids:
            continue
        for p in part_views(day_recs, store.load_transcripts(ids)):
            last: dict[str, float] = {}
            for para in p.paragraphs:
                cats = matches(para.text, para.lang)
                if not cats:
                    continue
                main = next(iter(cats))
                if para.start - last.get(main, -1e9) < SAME_POINT_S:
                    last[main] = para.start
                    continue
                last[main] = para.start
                date = (p.rec.start + dt.timedelta(seconds=para.start)).isoformat(timespec="minutes")
                items.append(_item(cats, para.text, 260, source="lecture", lang=para.lang, day_key=key, part=p.index,
                                   t=round(para.start, 1), ts=format_ts(para.start), viewer_url=p.rec.viewer_url,
                                   date=date))
    for a in anns:
        body = _tidy(a["body"] or "") or a["title"]
        title_cats = matches(a["title"])
        body_cats = matches(body)
        if not title_cats and not body_cats:
            continue
        order = list(title_cats) + [k for k in body_cats if k not in title_cats]   # the title says what it's about
        main = order[0]
        if main in body_cats:
            excerpt, hl = _excerpt(body, body_cats[main], 320)
        else:
            excerpt, hl = body[:320] + ("…" if len(body) > 320 else ""), [0, 0]
        items.append({"category": main, "categories": order, "text": excerpt, "hl": hl, "source": "announcement",
                      "title": a["title"], "lang": "tr" if _turkish(body, "") else "en", "announcement_id": a["id"],
                      "date": a["posted_at"] or a["first_seen"] or "", "is_new": bool(a["is_new"])})
    items.sort(key=lambda x: x["date"] or "", reverse=True)
    counts: dict[str, int] = defaultdict(int)
    for it in items:
        counts[it["category"]] += 1
    result = {"items": items[:MAX_ITEMS], "counts": dict(counts), "total": len(items), "plan": assessment_plan(anns)}
    with _cache_lock:
        _cache[course_id] = (sig, result)
    return result


# ------------------------------------------------------------------ the assessment plan: which week, which LOs
# Lecturers post it as a table (cells become lines: "Quiz 1" / "5. hafta" / "LO1–LO2"), as list items, as prose
# ("quizzes … in weeks 3, 5, 10 and 12") or in a note about one quiz ("About Quiz 1": "… Learning Outcome 2 …").
# The text is folded first (_fold), so the patterns spell ı as i.
# a month after a day ("Midterm - 2 December", "Quiz 2 - 3 Kasım", "3 Ara" as IEULearn shortens it): that day is no
# midterm or quiz number. In "Midterm 1 Nov 3" the day comes after the month; "may" is the month only where it can't
# be the verb ("Midterm 2 may be…"), and a Turkish month only as a word of its own or with a suffix ("Kasımda").
_MONTH = (r"(?:(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|"
          r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|oca|[sş]ub|nis|haz|tem|a[gğ]u|eyl|eki|kas)\b|ara\b(?!\s*sinav)|"
          r"may\b(?=\s*(?:$|[^\w\s]|\d|(?:at|in|on|will|is|covers|covering|"
          r"(?:mon|tues?|wed(?:nes)?|thu(?:rs)?|fri|sat(?:ur)?|sun)(?:day)?|pazartesi|sali|[cç]ar[sş]amba|"
          r"per[sş]embe|cuma(?:rtesi)?|pazar)\b))|"
          r"(?:ocak|[sş]ubat|mart|nisan|mayis|haziran|temmuz|a[gğ]ustos|eyl[uü]l|ekim|kasim|aralik)(?:[dt][ae]n?)?\b)"
          r"(?!\s*\.?\s*\d{1,2}(?![\d:]|\.\d))")
# a midterm and its number: "Midterm 1", "Midterm Exam 2", "Midterm-1", "Midterm II", "1. vize", "ara sınavı 2"
# (a Roman numeral only where it ends the name, so "the midterm I will…" is no "Midterm 1" and the ı of "Ara Sınavı"
# no I; "2 hours", "2 saatlik", "3. şube" and a date such as "2.12.2026" give no number, "Midterm 1 study guide" does)
_MID = (r"(?:([1-3])\s*\.\s*)?(ara sinavi?|midterm|vize)(?:\s*(?:exam(?:ination)?|sinavi))?"
        r"(?:\s*[#-]?\s*([1-3]\b(?![./:]\d)(?!\s*\.?\s*(?:hafta|g[uü]n|oturum|[sş]ube|saat|(?:th|st|nd|rd|hours?)\b|"
        + _MONTH + r"))|(?<=[\s#-])i{1,3}\b(?=\s*(?:$|[:,./()–—-]|week|hafta))))?")
_A_NAME = re.compile(r"^(?:(quiz|kisa sinav|küçük sinav)\s*#?\s*(\d{1,2})\b|" + _MID + r"|(final)\b)")
_A_ANY = re.compile(r"\b(?:(quiz)\s*#?\s*(\d{1,2})\b|" + _MID + r"|(final)(?= exam| sinav))")
# "week 5" (not "weeks 1-4": that is what it covers, not when it is held)
_WEEK = re.compile(r"\b(\d{1,2})\s*\.\s*hafta|\bweek\s*(\d{1,2})\b|\bhafta\s*(\d{1,2})\b")
_LO_RANGE = re.compile(r"\blo\s*(\d{1,2})\s*[–—-]\s*(?:lo\s*)?(\d{1,2})\b")
_LO_ONE = re.compile(r"\b(?:los?|learning outcomes?|öğrenme çiktisi)\s*(\d{1,2})\b")
_QUIZ_WEEKS = re.compile(r"\bquiz\w*\b[^.]{0,200}?\bweeks?\s+((?:\d{1,2}\s*(?:,|and|&)\s*)+\d{1,2})\b")
_QUIZ_NOS = re.compile(r"\bquiz(?:zes)?\s*#?\s*(\d{1,2}(?:\s*(?:,|and|&|ve|-|–|to|through)\s*(?:quiz\s*)?#?\s*"
                       r"\d{1,2}\b(?![./:]\d)(?![^\S\n]*" + _MONTH + r"))*)\b")   # (a month in the next cell isn't)
# "… will cover the topics of weeks 2, 4, 6 and 8": what the quizzes cover, not when they are held
_COVERS = re.compile(r"\bcover\w*\s+(?:(?!(?:held|given|take|taken|place|scheduled|be|on|during)\b)\w+\s+){0,6}"
                     r"weeks?\s*$")
_ROMAN = {"i": 1, "ii": 2, "iii": 3}


def _quiz_numbers(text: str) -> list[int]:
    """The quiz numbers a sentence names: "Quiz 3 and Quiz 4" → [3, 4], "quizzes 2-4" and "Quizzes 2 to 4" →
    [2, 3, 4]; not the day of "Quiz 2 - 3 November"."""
    out: list[int] = []
    for q in _QUIZ_NOS.findall(text):
        for a, b in re.findall(r"(\d{1,2})(?:\s*(?:[-–]|to|through)\s*(?:quiz\s*)?#?\s*(\d{1,2}))?", q):
            lo, hi = int(a), int(b or a)
            out += list(range(lo, hi + 1)) if 0 <= hi - lo <= 12 else [lo]
    return out


def _fold(text: str) -> str:
    """Lower case for matching, with the Turkish i's folded to "i": "QUIZ", "Midterm" and "kısa sınav" all match."""
    return _tr_lower(text).replace("ı", "i")


def _los(text: str) -> set[int]:
    out: set[int] = set()
    for a, b in _LO_RANGE.findall(text):
        lo, hi = sorted((int(a), int(b)))
        if hi - lo <= 12:
            out.update(range(lo, hi + 1))
    out.update(int(n) for n in _LO_ONE.findall(text))
    return out


def _week(text: str) -> Optional[int]:
    m = _WEEK.search(text)
    n = int(next(g for g in m.groups() if g)) if m else 0
    return n if 1 <= n <= 20 else None


def _assessment(m: re.Match) -> tuple[str, int]:
    quiz, no, before, mid, after, _ = m.groups()
    if quiz:
        return "quiz", int(no)
    n = before or after or "0"
    return ("midterm", int(n) if n.isdigit() else _ROMAN[n]) if mid else ("final", 0)


def assessment_plan(anns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """[{kind, no, name, week, los, sources}] from the announcements (newer ones win on the week)."""
    plan: dict[tuple[str, int], dict[str, Any]] = {}

    def add(key: tuple[str, int], week: Optional[int], los: set[int], a: dict[str, Any]) -> None:
        e = plan.setdefault(key, {"week": None, "week_at": "", "los": set(), "sources": {}})
        date = a["posted_at"] or a["first_seen"] or ""
        if week:
            e["week"], e["week_at"] = week, date
        e["los"] |= los
        if week or los:
            e["sources"][a["id"]] = {"id": a["id"], "title": a["title"], "date": date}

    for a in sorted(anns, key=lambda x: x["posted_at"] or x["first_seen"] or ""):
        body = _fold(_tidy(a["body"] or ""))
        lines = [ln.strip() for ln in body.split("\n") if ln.strip()]
        for i, ln in enumerate(lines):                 # table rows and short list items
            m = _A_NAME.match(ln)
            if not m or len(ln) > 60:
                continue
            cells = [ln[m.end():]]
            for nxt in lines[i + 1:i + 4]:
                if _A_NAME.match(nxt) or len(nxt) > 60:
                    break
                cells.append(nxt)
            text = " ".join(cells)
            add(_assessment(m), _week(text), _los(text), a)
        named = list(_A_ANY.finditer(_fold(a["title"])))
        if len(named) == 1:                            # a note about one assessment
            add(_assessment(named[0]), None, _los(body), a)
        for m in _QUIZ_WEEKS.finditer(body):           # "quizzes … in weeks 3, 5, 10 and 12"
            weeks = [int(x) for x in re.findall(r"\d{1,2}", m.group(1))]
            before = m.group(0)[:m.start(1) - m.start(0)]
            if _COVERS.search(before):
                continue
            said = _quiz_numbers(before)               # "Quiz 3 and 4 … in weeks 10 and 12"
            if not said:
                nos: list[int] = list(range(1, len(weeks) + 1))
            elif len(said) >= len(weeks):
                nos = said[-len(weeks):]
            else:                                      # "Quiz 3 will cover weeks 8 and 9": what it covers
                continue
            for n, w in zip(nos, weeks):
                add(("quiz", n), w, set(), a)
    if ("midterm", 0) in plan and {no for kind, no in plan if kind == "midterm"} == {0, 1}:
        one, other = plan[("midterm", 1)], plan.pop(("midterm", 0))     # "Midterm" and "Midterm 1": the same exam
        if other["week"] and other["week_at"] >= one["week_at"]:
            one["week"], one["week_at"] = other["week"], other["week_at"]
        one["los"] |= other["los"]
        one["sources"].update(other["sources"])
    out = []
    for (kind, no), e in plan.items():
        if not e["sources"]:
            continue
        name = f"Quiz {no}" if kind == "quiz" else kind.title() + (f" {no}" if no else "")
        out.append({"kind": kind, "no": no, "name": name, "week": e["week"], "los": sorted(e["los"]),
                    "sources": sorted(e["sources"].values(), key=lambda s: s["date"], reverse=True)})
    out.sort(key=lambda e: (e["week"] or 99, e["kind"] != "quiz", e["no"]))
    return out
