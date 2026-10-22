"""SubRip subtitles (for watching a downloaded video with captions, or any subtitle-aware tool)."""

from __future__ import annotations

from typing import Iterable

from ..domain import Segment


def _ts(sec: float) -> str:
    ms = int(round(max(0.0, sec) * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _wrap(text: str, width: int = 42) -> str:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    if len(lines) > 2:  # keep it to two lines
        half = (len(words) + 1) // 2
        lines = [" ".join(words[:half]), " ".join(words[half:])]
    return "\n".join(lines)


def _split(seg: Segment, max_s: float = 7.0, max_chars: int = 84) -> list[tuple[float, float, str]]:
    words = seg.text.split()
    dur = max(0.01, seg.end - seg.start)
    if dur <= max_s and len(seg.text) <= max_chars:
        return [(seg.start, seg.end, seg.text)]
    n = max(int(-(-dur // max_s)), int(-(-len(seg.text) // max_chars)))
    per = -(-len(words) // n)
    out = []
    for i in range(0, len(words), per):
        chunk = words[i:i + per]
        a = seg.start + dur * i / len(words)
        b = seg.start + dur * min(len(words), i + per) / len(words)
        out.append((a, b, " ".join(chunk)))
    return out


def render_srt(segments: Iterable[Segment]) -> str:
    blocks = []
    n = 0
    for seg in segments:
        if not seg.text.strip():
            continue
        text = seg.text if seg.lang in ("en", "") else f"[{seg.lang.upper()}] {seg.text}"
        for a, b, t in _split(Segment(seg.start, seg.end, text, seg.lang, seg.conf)):
            n += 1
            blocks.append(f"{n}\n{_ts(a)} --> {_ts(max(b, a + 0.5))}\n{_wrap(t)}\n")
    return "\n".join(blocks)
