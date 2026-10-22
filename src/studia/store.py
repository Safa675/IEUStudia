"""Canonical transcript / slide-index files in the app folder (the Markdown in the output folder is derived)."""

from __future__ import annotations

import json
import os
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Optional

from .domain import Transcript
from .paths import transcripts_dir
from .slides import Slide, load_index, save_index

_lock = threading.Lock()
_cache: "OrderedDict[str, Transcript]" = OrderedDict()
_MAX = 64


def transcript_path(delivery_id: str) -> Path:
    return transcripts_dir() / f"{delivery_id}.json"


def slides_path(delivery_id: str) -> Path:
    return transcripts_dir() / f"{delivery_id}.slides.json"


def save_transcript(t: Transcript) -> Path:
    path = transcript_path(t.delivery_id)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(t.to_json(), ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    with _lock:
        _cache[t.delivery_id] = t
        _cache.move_to_end(t.delivery_id)
    return path


def load_transcript(delivery_id: str) -> Optional[Transcript]:
    with _lock:
        if delivery_id in _cache:
            _cache.move_to_end(delivery_id)
            return _cache[delivery_id]
    path = transcript_path(delivery_id)
    if not path.exists():
        return None
    try:
        t = Transcript.from_json(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return None
    with _lock:
        _cache[delivery_id] = t
        while len(_cache) > _MAX:
            _cache.popitem(last=False)
    return t


def load_transcripts(ids) -> dict[str, Transcript]:
    out = {}
    for i in ids:
        t = load_transcript(i)
        if t is not None:
            out[i] = t
    return out


def save_slides(delivery_id: str, slides: list[Slide]) -> None:
    save_index(slides_path(delivery_id), slides)


def load_slides(delivery_id: str) -> list[Slide]:
    p = slides_path(delivery_id)
    return [s for s in load_index(p) if not s.path or os.path.isfile(s.path)] if p.exists() else []


def forget(delivery_id: str) -> None:
    with _lock:
        _cache.pop(delivery_id, None)
