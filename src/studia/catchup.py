"""Full sync / course sync: what a course (or every course) still needs after a sync - the lectures of your own
section that are not transcribed yet (newest first, so the lecturer's latest heads-ups show up first) and the
materials not downloaded yet. Nothing here uses Claude: notes, plans and chats stay on demand."""

from __future__ import annotations

import dataclasses as dc
from pathlib import Path
from typing import Any, Iterable, Optional

from .config import Settings
from .sections import available_sections, classify, guess_my_section

READY = ("available", "failed")      # Panopto has finished processing it ("processing" ones are not ready yet)
# rough real-time factors (minutes of audio per minute of work), English + Turkish decoding included; large-v3 on an
# RTX 4070 measured ~13x
SPEED = {"gpu": {"large-v3-turbo": 22.0, "large-v3": 12.0, "medium": 16.0, "small": 25.0},
         "cpu": {"large-v3-turbo": 1.0, "large-v3": 0.4, "medium": 0.2, "small": 2.0}}     # measured on a laptop CPU


@dc.dataclass
class CourseNeeds:
    course_id: str
    code: str
    name: str
    lectures: list[tuple[str, str]]  # (delivery id, ISO start), newest first
    audio_s: float
    materials: list[str]             # resource ids
    other_sections: int              # skipped: lectures of the other sections
    needs_section: bool              # several sections publish here and yours is unknown: nothing is queued
    waiting: int                     # lectures held back until you choose your section

    def view(self) -> dict[str, Any]:
        return {"course_id": self.course_id, "code": self.code, "name": self.name, "lectures": len(self.lectures),
                "audio_s": round(self.audio_s), "materials": len(self.materials), "other_sections": self.other_sections,
                "needs_section": self.needs_section, "waiting": self.waiting}


def _downloaded(m: dict[str, Any]) -> bool:
    return bool(m.get("local_path")) and Path(m["local_path"]).is_file()


def course_needs(lib: Any, settings: Settings, course_ids: Optional[Iterable[str]] = None,
                 busy: Iterable[str] = ()) -> list[CourseNeeds]:
    """Per course (all active ones, or ``course_ids``): the work a full sync would queue. ``busy``: ids already queued."""
    wanted = set(course_ids) if course_ids is not None else None
    busy = set(busy)
    out: list[CourseNeeds] = []
    for c in lib.courses():
        if wanted is not None and c.id not in wanted:
            continue
        recs = lib.recordings(c.id)
        prefs = settings.prefs(c.id)
        my_section = prefs.my_section or guess_my_section(c)
        sections = available_sections(recs)
        several = len(sections) > 1
        mine = classify(c, recs, my_section) if sections else {}
        todo, other, waiting = [], 0, 0
        for r in recs:
            if (r.duplicate_of or r.hidden or r.status not in READY or r.delivery_id in busy
                    or 0 < r.duration_s < settings.min_duration_s):
                continue
            if mine.get(r.delivery_id) is False:
                other += 1
            elif several and not my_section:
                waiting += 1
            else:
                todo.append(r)
        todo.sort(key=lambda r: r.start, reverse=True)
        mats = [m["resource_id"] for m in lib.materials(c.id) if not m.get("gone") and not _downloaded(m)]
        out.append(CourseNeeds(c.id, c.code, prefs.alias or c.name or c.code,
                               [(r.delivery_id, r.start.isoformat()) for r in todo],
                               sum(max(0.0, r.duration_s) for r in todo), mats, other, several and not my_section,
                               waiting))
    return out


def speed(gpu: bool, model: str) -> float:
    return SPEED["gpu" if gpu else "cpu"].get(model, 12.0 if gpu else 0.6)


def summary(needs: list[CourseNeeds], gpu: bool, model: str = "large-v3-turbo") -> dict[str, Any]:
    audio = sum(n.audio_s for n in needs)
    return {"courses": [n.view() for n in needs], "lectures": sum(len(n.lectures) for n in needs),
            "audio_s": round(audio), "materials": sum(len(n.materials) for n in needs),
            "work_s": round(audio / speed(gpu, model)), "speed": speed(gpu, model), "gpu": gpu,
            "audio_mb": round(audio / 60 * 0.75),            # audio-only streams: ~0.75 MB a minute, deleted afterwards
            "needs_section": [n.code for n in needs if n.needs_section and n.waiting]}


def queue_order(needs: list[CourseNeeds]) -> list[str]:
    """Every course's lectures in one list, newest first (the latest heads-ups across courses come first)."""
    return [did for did, _ in sorted((x for n in needs for x in n.lectures), key=lambda x: x[1], reverse=True)]
