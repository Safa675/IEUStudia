"""Settings of IEUStudia on this PC (not per account): the first-start setup, which AI the app uses, running in
the background (tray), starting with Windows, notifications and looking for new versions. Stored in
``<app data>\\app.json``."""

from __future__ import annotations

import dataclasses as dc
import json
import os
import threading
from typing import Any

AI_PROVIDERS = ("chatgpt", "claude", "codex", "local", "none")


@dc.dataclass
class AppConfig:
    onboarded: bool = False            # the first-start setup was completed
    terms_accepted: str = ""           # when the user accepted the notice (ISO date)
    ai_provider: str = ""              # "" (not chosen yet) | chatgpt | claude | codex (ChatGPT through the CLI) | local | none
    codex_model: str = ""              # the ChatGPT model, also through the Codex CLI ("" = the default)
    local_model: str = "qwen3-1.7b"    # components.LLM_PRESETS
    tray: bool = False                 # keep a small helper in the tray: checks IEULearn, shows notifications
    start_with_windows: bool = False   # start that helper when you sign in to Windows
    notify_recordings: bool = True     # new lecture recordings (your section)
    notify_announcements: bool = True  # new announcements (assessment news first)
    notify_materials: bool = False     # new files on IEULearn
    notify_reminders: bool = True      # the evening before an announced quiz, and before an exam week
    sync_minutes: int = 30             # how often the helper checks
    check_updates: bool = True         # look for a new version of IEUStudia on GitHub about once a day
    version: int = 0                   # 1: "codex" is only the Codex CLI (see migrate)

    def to_json(self) -> dict[str, Any]:
        return dc.asdict(self)


_lock = threading.Lock()


def _path():
    from .paths import app_dir

    return app_dir() / "app.json"


def _from_dict(raw: dict[str, Any]) -> AppConfig:
    c = AppConfig()
    for f in dc.fields(AppConfig):
        if f.name not in raw:
            continue
        default, value = getattr(c, f.name), raw[f.name]
        try:
            if isinstance(default, bool):
                value = bool(value)
            elif isinstance(default, int):
                value = int(value)
            elif isinstance(default, str):
                value = str(value)
        except (TypeError, ValueError):
            continue
        setattr(c, f.name, value)
    if c.ai_provider not in AI_PROVIDERS:
        c.ai_provider = ""
    c.sync_minutes = max(10, min(240, c.sync_minutes))
    return c


def load() -> AppConfig:
    p = _path()
    with _lock:
        try:
            raw = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        except (OSError, ValueError):
            raw = {}
    return _from_dict(raw if isinstance(raw, dict) else {})


def save(cfg: AppConfig) -> None:
    p = _path()
    tmp = p.with_name(p.name + ".tmp")
    with _lock:
        tmp.write_text(json.dumps(cfg.to_json(), indent=2), encoding="utf-8")
        os.replace(tmp, p)


def update(patch: dict[str, Any]) -> AppConfig:
    merged = load().to_json()
    merged.update({k: v for k, v in (patch or {}).items() if k in merged})
    cfg = _from_dict(merged)
    save(cfg)
    return cfg


def migrate() -> None:
    """Once, at start-up: "codex" used to mean ChatGPT either way, through "Continue with ChatGPT" (the main way, and
    the card's own sign-in) or through the Codex CLI. Now that sign-in is "chatgpt" and "codex" only the CLI, which
    stays the choice only for someone who has the CLI and never signed in the other way."""
    cfg = load()
    if cfg.version >= 1:
        return
    patch: dict[str, Any] = {"version": 1}
    if cfg.ai_provider == "codex":
        from . import ai, chatgpt

        if chatgpt.status()["registered"] or not ai._codex_candidates():
            patch["ai_provider"] = "chatgpt"
    update(patch)
