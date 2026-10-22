"""Group transcript segments into readable paragraphs."""

from __future__ import annotations

import dataclasses as dc
import re
from typing import Iterable, Optional

from ..domain import Segment
from .highlights import detect

UNCLEAR = "[unclear]"
_SENT_END = re.compile(r"[.!?…][\"')\]]*$")


@dc.dataclass
class Paragraph:
    start: float
    end: float
    text: str
    lang: str
    star: bool = False
    tags: list[str] = dc.field(default_factory=list)
    uncertain: bool = False
    slide: Optional[int] = None       # index of the slide shown when the paragraph starts (if slides exist)


def clean_join(parts: Iterable[str]) -> str:
    t = " ".join(p.strip() for p in parts if p and p.strip())
    t = re.sub(r"\s+([,.;:!?…])", r"\1", t)
    t = re.sub(r"\s{2,}", " ", t)
    t = re.sub(r"(\[unclear\])(\s+\[unclear\])+", r"\1", t)
    return t.strip()


def build_paragraphs(segments: Iterable[Segment], max_gap: float = 2.5, max_len_s: float = 75.0,
                     max_words: int = 140) -> list[Paragraph]:
    paras: list[Paragraph] = []
    cur: list[Segment] = []

    def flush() -> None:
        if not cur:
            return
        text = clean_join(s.text for s in cur)
        if not text:
            cur.clear()
            return
        real = [s for s in cur if s.text != UNCLEAR]
        lang = max({s.lang for s in real} or {cur[0].lang},
                   key=lambda l: sum(s.end - s.start for s in real if s.lang == l))
        dur = sum(max(0.01, s.end - s.start) for s in real) or 1.0
        conf = sum(s.conf * max(0.01, s.end - s.start) for s in real) / dur if real else 0.0
        tags = detect(text, lang)
        paras.append(Paragraph(start=cur[0].start, end=cur[-1].end, text=text, lang=lang, star=bool(tags),
                               tags=tags, uncertain=bool(real) and conf < 0.5))
        cur.clear()

    for seg in segments:
        if not (seg.text or "").strip():
            continue
        if cur:
            last = cur[-1]
            gap = seg.start - last.end
            p_lang = next((s.lang for s in reversed(cur) if s.text != UNCLEAR), seg.lang)
            lang_change = seg.text != UNCLEAR and last.text != UNCLEAR and seg.lang != p_lang
            length = seg.end - cur[0].start
            words = sum(len(s.text.split()) for s in cur)
            soft_full = (length >= 0.6 * max_len_s or words >= 0.6 * max_words) and _SENT_END.search(last.text.strip())
            if lang_change or gap >= max_gap or length > max_len_s or words >= max_words or soft_full:
                flush()
        cur.append(seg)
    flush()
    return paras
