"""The flashcards the AI wrote for a lecture ("Q: … / A: … (Part n, mm:ss)", see prompts.py) as cards to practise
with in the app. ChatGPT and Claude vary the format: bold labels ("**Q:**", "**Q1**:", "**1. Q:**"), numbering,
"Question 1:", a question as a heading, the answer on the question's line or over several lines, rules or headings
between cards, or a table with a Question and an Answer column."""

from __future__ import annotations

import re
from typing import Any

_B = r"(?:\*\*|__)"                                        # bold
_LEAD = rf"^\s*(?:#{{1,6}}\s+)?(?:[-*+]\s+)?{_B}?\s*(?:\d+[.)]\s*)?{_B}?\s*"
_Q = re.compile(_LEAD + rf"(?:(?:Q|[Qq]uestion|QUESTION)\s*\d*\s*{_B}?\s*[:.)]|q\s*\d*\s*{_B}?\s*:)\s*{_B}?\s*(.*)$")
# an answer is "A:" or "A." ("a:" too), never "a)" or "A)": those are the options of a multiple-choice question
_A = re.compile(_LEAD + rf"(?:(?:A|[Aa]nswer|ANSWER)\s*\d*\s*{_B}?\s*[:.]|a\s*\d*\s*{_B}?\s*:)\s*{_B}?\s*(.*)$")
# "Q: What is X? A: Y": the answer after the question's end ("the matrix A: …" inside a question is not one)
_A_INLINE = re.compile(rf"(?<=[?.!]){_B}?\s+{_B}?(?:A|Answer)\s*\d*\s*{_B}?\s*:\s*{_B}?\s*(?=\S)")
_HEADING = re.compile(r"^\s*#{1,6}\s+(.*?)\s*#*\s*$")
_RULE = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$")
_CITE = re.compile(r"\(\s*Part\s+(\d+)\s*,\s*(?:(\d+):)?(\d{1,2}):(\d{2})\s*(?:[–-]\s*(?:\d+:)?\d{1,2}:\d{2}\s*)?\)", re.I)
_ROW = re.compile(r"^\s*\|(.*)\|\s*$")


def _unbold(text: str) -> str:
    """"What is X?**" (the rest of "**Q: What is X?**") → "What is X?"."""
    text = text.strip()
    for b in ("**", "__"):
        if text.endswith(b) and text.count(b) % 2 == 1:
            text = text[:-2].rstrip()
    return text


def _card(question: str, answer: str) -> dict[str, Any]:
    m = _CITE.search(answer) or _CITE.search(question)
    t = (int(m.group(2) or 0) * 3600 + int(m.group(3)) * 60 + int(m.group(4))) if m else None
    return {"q": question, "a": answer, "part": int(m.group(1)) if m else None, "t": t}


def _table(lines: list[str]) -> list[dict[str, Any]]:
    """A Markdown table with a Question and an Answer column."""
    cards: list[dict[str, Any]] = []
    cols: tuple[int, int] | None = None
    for line in lines:
        m = _ROW.match(line)
        if not m:
            cols = None
            continue
        cells = [c.strip() for c in m.group(1).split("|")]
        names = [re.sub(r"[*_]", "", c).strip().lower() for c in cells]
        if cols is None:
            q = next((i for i, n in enumerate(names) if n in ("q", "question", "soru")), None)
            a = next((i for i, n in enumerate(names) if n in ("a", "answer", "cevap")), None)
            if q is not None and a is not None:
                cols = (q, a)
            continue
        if all(set(c) <= set("-: ") for c in cells):           # the |---|---| line
            continue
        if max(cols) < len(cells) and cells[cols[0]] and cells[cols[1]]:
            cards.append(_card(cells[cols[0]], cells[cols[1]]))
    return cards


def parse_cards(md: str) -> list[dict[str, Any]]:
    """[{"q", "a", "part", "t"}]: the question and answer as Markdown, and the moment the answer cites (or None)."""
    cards: list[dict[str, Any]] = []
    q: list[str] = []
    a: list[str] = []
    state = ""                                   # "q": in a question, "a": in its answer
    heading = ""                                 # the last heading: the question when an answer follows it

    def close() -> None:
        question, answer = "\n".join(q).strip(), "\n".join(a).strip()
        if question and answer:
            cards.append(_card(question, answer))

    lines = (md or "").splitlines()
    for line in lines:
        mq = _Q.match(line)
        if mq:
            close()
            rest = _unbold(mq.group(1))
            inline = _A_INLINE.search(rest)
            if inline and inline.start() > 0:    # the answer on the same line
                q, a, state = [_unbold(rest[:inline.start()])], [_unbold(rest[inline.end():])], "a"
            else:
                q, a, state = [rest], [], "q"
            heading = ""
            continue
        ma = _A.match(line) if state == "q" or (state != "a" and heading) else None
        if ma:
            if state != "q":                     # "### What is X?" then "**A:** …"
                close()
                q = [heading]
            a, state, heading = [_unbold(ma.group(1))], "a", ""
            continue
        mh = _HEADING.match(line)
        if mh or _RULE.match(line):
            if state == "a":
                close()
                q, a, state = [], [], ""
            elif state == "q" and not "".join(q).strip():
                state = ""
            heading = _unbold(mh.group(1)) if mh else ""
            continue
        if state == "q":
            q.append(line)
        elif state == "a":
            a.append(line)
        elif line.strip():
            heading = ""                         # text between a heading and an answer: not a question
    if state == "a":
        close()
    return cards or _table(lines)
