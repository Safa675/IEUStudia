"""What Claude should know about a course besides the lectures it reads: the learning outcomes, the weekly plan, the
assessments the lecturer announced and the latest announcements (``course_brief``, a few thousand characters, part of
a chat's fixed context), and, per question, the passages of the course files (slides, PDFs) and of other lectures
that match it (``retrieve``).
"""

from __future__ import annotations

import bisect
import datetime as dt
import re
import unicodedata
from typing import Any, Iterable

from . import store
from .alerts import _tidy

BRIEF_CHARS = 7_000
EXCERPT_CHARS = 900
CANDIDATES = 300              # lecture segments the search finds, grouped into their paragraphs
_TURKISH_LETTERS = re.compile("[çğıöşü]")
_TURKISH_WORDS = {"ne", "nedir", "nelerdir", "nasil", "neden", "niye", "hangi", "hangisi", "midir", "ve", "bir", "bu",
                  "icin", "ile", "gibi", "kac"}          # a Turkish question typed without its letters


def course_brief(lib: Any, settings: Any, course_id: str, max_chars: int = BRIEF_CHARS) -> str:
    """Learning outcomes, weekly plan, announced assessments and recent announcements of a course, as plain text."""
    from .views import course_detail

    d = course_detail(lib, settings, course_id)
    if d is None:
        return ""
    out = [f"Course: {d['code']} — {d['syllabus'].get('name') or d['display_name']}"
           + (f" · lecturer {d['instructor']}" if d.get("instructor") else "") + (f" · {d['term']}" if d.get("term") else "")]
    if d["outcomes_en"]:
        out.append("Learning outcomes (English syllabus):")
        out += [f"  LO{i}: {t}" for i, t in enumerate(d["outcomes_en"], 1)]
    if d["outcomes_tr"] and d["outcomes_tr"] != d["outcomes_en"]:
        out.append("Learning outcomes on IEULearn (Turkish; numbering may differ):")
        out += [f"  LO{i}: {t}" for i, t in enumerate(d["outcomes_tr"], 1)]
    if d["grading"]:
        parts = []
        for g in d["grading"]:
            times = f" ×{g['number']}" if (g.get("number") or 1) > 1 else ""
            parts.append(f"{g['activity']}{times} {float(g['weight']):g}%")
        out.append("Grading: " + ", ".join(parts))
    weeks = [w for w in d["weeks"] if w["topic_en"] or w["topic_tr"] or w["units"]]
    if weeks:
        today = dt.date.today().isoformat()
        out.append(f"Weekly plan (today is {today}):")
        for w in weeks:
            topic = " / ".join(t for t in (w["topic_en"], w["topic_tr"]) if t) or ", ".join(u["title"] for u in w["units"])
            when = f" ({w['start']} to {w['end']})" if w["start"] else ""
            out.append(f"  Week {w['no']}{when}{' ← this week' if w['current'] else ''}: {topic}")
    plan = (d.get("alerts") or {}).get("plan") or []
    if plan:
        out.append("Assessments the lecturer announced:")
        out += [f"  {e['name']}: week {e['week'] or '?'}" + (f", covers LO{', LO'.join(map(str, e['los']))}" if e["los"] else "")
                for e in plan]
    anns = d.get("announcements") or []
    if anns:
        out.append("Latest announcements (newest first):")
        for a in anns[:6]:
            body = " ".join(_tidy(a.get("body") or "").split())
            out.append(f"  [{(a.get('posted_at') or '')[:10]}] {a['title']}: {body[:500]}" + ("…" if len(body) > 500 else ""))
    text = "\n".join(out)
    return text if len(text) <= max_chars else text[:max_chars].rsplit("\n", 1)[0] + "\n  …"


def _window(text: str, words: list[str], limit: int = EXCERPT_CHARS) -> str:
    """About ``limit`` characters of ``text`` around its first match of ``words``."""
    t = " ".join((text or "").split())
    if len(t) <= limit:
        return t
    low = t.lower()
    pos = min((p for p in (low.find(w) for w in words) if p >= 0), default=0)
    start = max(0, pos - limit // 3)
    piece = t[start:start + limit]
    return ("…" if start else "") + piece + ("…" if start + limit < len(t) else "")


def _fold(text: str) -> str:
    """Lower case without accents and with one kind of i, as the search index sees words."""
    low = unicodedata.normalize("NFKD", (text or "").replace("İ", "i").replace("I", "i").lower().replace("ı", "i"))
    return "".join(ch for ch in low if not unicodedata.combining(ch))


def _search_words(lib: Any, question: str) -> list[str]:
    """The words of the question the search looks for. In a Turkish question a long word is cut to its first five
    letters, before its endings: "sınavda" then finds "sınav", "sınavı" and "sınavlar" too."""
    words = [w for w in re.findall(r"[\w']+", (question or "").replace("İ", "i").lower())
             if len(w) >= 3 and w not in lib._STOP and not w.isdigit()]
    if any(_TURKISH_LETTERS.search(w) or w in _TURKISH_WORDS for w in re.findall(r"[\w']+", (question or "").lower())):
        words = [w[:5] if len(w) >= 6 else w for w in words if w not in _TURKISH_WORDS]
    return list(dict.fromkeys(words))[:10]


def _found(words: list[str], text: str) -> int:
    """How many of the search words a passage contains (a word of five letters or more also as the start of one)."""
    have = set(re.findall(r"\w+", _fold(text)))
    return sum(1 for w in map(_fold, words) if w in have or len(w) >= 5 and any(x.startswith(w) for x in have))


def _paragraphs(delivery_id: str) -> list[Any]:
    from .outputs.markdown import _paragraphs as built          # built once per transcript, like the day page's

    t = store.load_transcript(delivery_id)
    return built(t) if t else []


def retrieve(lib: Any, course_id: str, question: str, skip_days: Iterable[str] = (), max_chars: int = 7_000,
             limit: int = 6) -> tuple[str, list[dict[str, Any]]]:
    """Passages of the course's files and of lectures NOT already in the chat that match the question: the text to
    put before the question, and the sources (for the UI). Empty when nothing matches.

    A lecture is indexed per speech segment (a few words each), so many matching segments are looked up in their
    paragraphs, and the paragraphs that contain the most different words of the question come first."""
    from .outputs.common import format_ts

    words = _search_words(lib, question)
    if not words:
        return "", []
    hits = lib.search_any(" ".join(words), course_id, limit=CANDIDATES // 3)
    skip = set(skip_days)
    parts, sources, size = [], [], 0
    for h in hits["materials"][:limit]:
        m = lib.get_material(h["resource_id"])
        if not m:
            continue
        label = f"[{m['file_name'] or m['title']} p.{h['page']}]"
        piece = f"{label} {_window(h['text'], words)}"
        if size + len(piece) > max_chars:
            break
        parts.append(piece)
        size += len(piece)
        sources.append({"kind": "file", "resource_id": m["resource_id"], "title": m["title"], "page": h["page"]})
    recs: dict[str, Any] = {}
    paras: dict[str, tuple[list[float], list[Any]]] = {}
    found: dict[tuple[str, float], dict[str, Any]] = {}       # (recording, paragraph start) → the passage
    for rank, h in enumerate(hits["transcripts"]):
        did = h["delivery_id"]
        if did not in recs:
            recs[did] = lib.get_recording(did)
        rec = recs[did]
        if rec is None or rec.day_key in skip:
            continue
        if did not in paras:
            ps = _paragraphs(did)
            paras[did] = ([p.start for p in ps], ps)
        starts, ps = paras[did]
        k = bisect.bisect_right(starts, h["t"] + 0.01) - 1
        p = ps[k] if k >= 0 and h["t"] <= ps[k].end + 0.01 else None
        start, text = (p.start, p.text) if p else (h["t"], h["text"])     # no paragraph: the segment itself
        if (did, start) in found:
            found[(did, start)]["hits"] += 1
        else:
            found[(did, start)] = {"rec": rec, "t": start, "text": text, "rank": rank, "hits": 1,
                                   "words": _found(words, text)}
    seen_days: dict[str, int] = {}
    for x in sorted(found.values(), key=lambda x: (-x["words"], -x["hits"], x["rank"])):   # then the search's order
        rec = x["rec"]
        if seen_days.get(rec.day_key, 0) >= 2:
            continue
        seen_days[rec.day_key] = seen_days.get(rec.day_key, 0) + 1
        label = f"[lecture {rec.start:%a %d %b} {rec.start:%H:%M} recording, {format_ts(x['t'])}]"
        piece = f"{label} {_window(x['text'], words, 600)}"
        if size + len(piece) > max_chars:
            break
        parts.append(piece)
        size += len(piece)
        sources.append({"kind": "lecture", "day_key": rec.day_key, "delivery_id": rec.delivery_id, "t": x["t"]})
        if len(sources) >= limit * 2:
            break
    if not parts:
        return "", []
    text = ("Passages from the course files (slides, PDFs) and from lectures not loaded above that match my question "
            "(found by keyword search, so some may be irrelevant; cite the ones you use as given):\n\n" + "\n\n".join(parts))
    return text, sources
