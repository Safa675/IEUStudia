"""Wires settings, library, sync, jobs, outputs, slides, export and AI together (used by the GUI and the CLI)."""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable, Optional

import requests

from . import store
from .config import Settings, apply_patch, load_settings, save_settings
from .domain import Course, Recording
from .library import Library
from .log import setup_logging
from .outputs.writer import course_dir, day_file_name, day_stem, safe_name, write_day, write_local, write_pack
from .paths import db_path
from .sync import SyncService

log = logging.getLogger("studia")


class Services:
    def __init__(self, debug: bool = False) -> None:
        self.log = setup_logging(debug)
        self._lock = threading.RLock()
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self.settings: Settings = load_settings()
        self.lib = Library(db_path())
        self.lib.reset_interrupted()
        self._migrate_output_root()
        self.sync = SyncService(self.lib, lambda: self.settings, self.log)
        from .chat import ChatService
        from .jobs import JobManager
        from .materials import MaterialStore

        self.jobs = JobManager(self.lib, self.sync, lambda: self.settings, self._make_engine, self.emit,
                               self.write_outputs, self.slides_task, self.log)
        self.chat = ChatService(self)
        self.materials = MaterialStore(self)
        from .plans import PlanRunner

        self.plans = PlanRunner(self)

    def shutdown(self) -> None:
        self.plans.shutdown()
        self.chat.shutdown()

    def _migrate_output_root(self) -> None:
        """An output folder that still has the app's earlier name (Documents\\Studia or Documents\\LectureScribe, a
        profile's Documents\\Studia - <name>) gets the IEUStudia name once: it is renamed (same drive, so instant)
        unless the new name is taken, and the paths stored for its files follow. Folders the user chose keep theirs."""
        from .paths import _home_override, renamed_output_path

        old = Path(self.settings.output_dir)
        new = renamed_output_path(old)
        if _home_override() is not None or new is None or not old.parent.is_dir():
            return
        try:
            if old.is_dir():
                if new.is_dir() and not any(new.iterdir()):
                    new.rmdir()                                # empty, e.g. made by an earlier start
                if new.exists():
                    self.log.info("Keeping the output folder %s: %s exists too", old, new)
                    return
                os.replace(old, new)
        except OSError:                                        # e.g. a file in it is open: try again next start
            self.log.warning("Could not rename %s to %s", old, new, exc_info=True)
            return
        self.rebase_output_paths(old, new)                     # also after a start that stopped right after renaming
        self.update_settings({"output_dir": str(new)})
        self.log.info("Output folder %s -> %s", old, new)

    def rebase_output_paths(self, old_root: Path, new_root: Path) -> None:
        """Point the paths stored for files in ``old_root`` (slide images, downloaded materials, plan and digest
        files) at the same files in ``new_root``, where they exist."""
        from .paths import transcripts_dir
        from .slides import load_index, save_index

        old_root, new_root = Path(old_root), Path(new_root)

        def moved(value: str) -> Optional[str]:
            try:
                path = new_root / Path(value).relative_to(old_root)
            except ValueError:                                 # not in the old folder (or not a path)
                return None
            return str(path) if path.exists() else None

        self.lib.rebase_paths(moved)
        for f in transcripts_dir().glob("*.slides.json"):
            slides, changed = load_index(f), False
            for s in slides:
                new = moved(s.path)
                if new:
                    s.path, changed = new, True
            if changed:
                save_index(f, slides)
                store.forget(f.name.split(".")[0])

    # ------------------------------------------------------------------ events / settings
    def subscribe(self, fn: Callable[[dict[str, Any]], None]) -> None:
        self._listeners.append(fn)

    def emit(self, evt: dict[str, Any]) -> None:
        for fn in list(self._listeners):
            try:
                fn(evt)
            except Exception:  # noqa: BLE001 - a broken listener must not break jobs
                self.log.debug("listener failed", exc_info=True)

    def update_settings(self, patch: dict[str, Any]) -> Settings:
        with self._lock:
            self.settings = apply_patch(self.settings, patch)
            save_settings(self.settings)
        return self.settings

    @staticmethod
    def _make_engine(settings: Settings) -> Any:
        if os.environ.get("STUDIA_FAKE_ENGINE") == "1":
            from .engine.stub import StubEngine

            return StubEngine()
        from .engine import Engine

        return Engine(settings.model, settings.device, settings.languages)

    # ------------------------------------------------------------------ lookups
    @property
    def output_root(self) -> Path:
        p = Path(self.settings.output_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def day(self, day_key: str) -> tuple[Course, list[Recording]]:
        course_id = day_key.split("|", 1)[0]
        course = self.lib.get_course(course_id)
        if course is None:
            raise KeyError("Unknown course")
        recs = sorted((r for r in self.lib.recordings(course_id) if r.day_key == day_key), key=lambda r: r.start)
        if not recs:
            raise KeyError("Unknown class day")
        return course, recs

    def day_files(self, day_key: str) -> dict[str, str]:
        course, recs = self.day(day_key)
        all_courses = self.lib.courses(True)
        folder = course_dir(self.output_root, course, all_courses)
        md = folder / day_file_name(course, recs[0].start.date(), recs[0].kind)
        stem = day_stem(course, recs[0].start.date(), recs[0].kind)
        pack = folder / safe_name(f"{course.code or course.name} - Study pack.md")
        out = {"folder": str(folder), "md": str(md) if md.exists() else "", "pack": str(pack) if pack.exists() else ""}
        pdf = folder / "AI packs" / safe_name(f"{stem} - AI pack.pdf")
        zp = folder / "AI packs" / safe_name(f"{stem} - AI pack.zip")
        notes = folder / "AI notes" / safe_name(f"{stem} - Study notes.md")
        out.update(pdf=str(pdf) if pdf.exists() else "", zip=str(zp) if zp.exists() else "",
                   notes=str(notes) if notes.exists() else "")
        return out

    # ------------------------------------------------------------------ outputs
    def write_outputs(self, delivery_id: str) -> dict[str, Any]:
        if delivery_id.startswith("local-"):
            t = store.load_transcript(delivery_id)
            if t is None:
                return {}
            name = (t.meta or {}).get("source_path", delivery_id)
            return {"md": write_local(self.output_root, name, t)}
        rec = self.lib.get_recording(delivery_id)
        if rec is None:
            return {}
        course, day_recs = self.day(rec.day_key)
        all_courses = self.lib.courses(True)
        course_recs = self.lib.recordings(course.id)
        done_ids = self.lib.transcribed_ids()
        trans_all = store.load_transcripts([r.delivery_id for r in course_recs if r.delivery_id in done_ids])
        ids = [r.delivery_id for r in day_recs]
        slides = {i: store.load_slides(i) for i in ids}
        warnings = {r.delivery_id: self.lib.warnings(r.delivery_id) for r in course_recs}
        return write_day(self.output_root, course, all_courses, day_recs, {i: trans_all[i] for i in ids if i in trans_all},
                         course_recs, trans_all, self.settings.write_srt, slides, warnings)

    def relocate_outputs(self, old_root: Path, new_root: Path) -> int:
        """Copy the app's folders (course folders with transcripts, slides, materials, packs and notes; weekly
        digests) to a new output folder and point the stored paths there. The old folder is left untouched. Returns
        the number of folders copied."""
        import shutil

        from .outputs.writer import course_folder_name

        old_root, new_root = Path(old_root), Path(new_root)
        if not old_root.exists() or old_root.resolve() == new_root.resolve():
            return 0
        courses = self.lib.courses(True)
        names = {course_folder_name(c, courses) for c in courses} | {"Local files", "Weekly digests"}
        copied = 0
        for name in sorted(names):
            src = old_root / name
            if src.is_dir() and not new_root.resolve().is_relative_to(src.resolve()):
                shutil.copytree(src, new_root / name, dirs_exist_ok=True)
                copied += 1
        self.rebase_output_paths(old_root, new_root)
        return copied

    def rewrite_course(self, course_id: str) -> None:
        course = self.lib.get_course(course_id)
        if course is None:
            return
        done = self.lib.transcribed_ids()
        seen = set()
        for r in self.lib.recordings(course_id):
            if r.delivery_id in done and r.day_key not in seen:
                seen.add(r.day_key)
                self.write_outputs(r.delivery_id)

    def slides_task(self, job: Any, src: Any, offset: float, session: requests.Session) -> list[Any]:
        from .slides import extract_slides

        rec = self.lib.get_recording(job.id)
        course = self.lib.get_course(rec.course_id) if rec else None
        if rec is None or course is None:
            return []
        stem = day_stem(course, rec.start.date(), rec.kind)
        folder = course_dir(self.output_root, course, self.lib.courses(True)) / "slides" / stem
        slides = extract_slides(session, src.url, folder, f"slides/{stem}", rec.start.strftime("%H%M"), offset,
                                cancel=job.cancel)
        store.save_slides(job.id, slides)
        self.log.info("%d slides for %s", len({s.index for s in slides}), job.id)
        return slides

    def export_pack(self, day_key: str) -> dict[str, Path]:
        from .export import build_ai_pack

        course, recs = self.day(day_key)
        ids = [r.delivery_id for r in recs]
        trans = store.load_transcripts(ids)
        if not trans:
            raise RuntimeError("Transcribe this class day first.")
        slides = {i: store.load_slides(i) for i in ids}
        return build_ai_pack(self.output_root, course, self.lib.courses(True), recs, trans, slides)

    def ai_inputs(self, day_key: str, include_slides: bool) -> tuple[str, list[Path], Course, list[Recording]]:
        from .outputs.ai_copy import compact_day

        course, recs = self.day(day_key)
        ids = [r.delivery_id for r in recs]
        trans = store.load_transcripts(ids)
        slides = {i: store.load_slides(i) for i in ids}
        text = compact_day(course, recs, trans, slides if include_slides else None)
        images: list[Path] = []
        if include_slides:
            seen = set()
            for i in ids:
                for s in slides.get(i, []):
                    if s.path and s.path not in seen and os.path.isfile(s.path):
                        seen.add(s.path)
                        images.append(Path(s.path))
        return text, images, course, recs

    def ai_notes(self, day_key: str, template: str, include_slides: bool, model: str,
                 cancel: Optional[threading.Event] = None, on_text: Optional[Callable[[str], None]] = None) -> Path:
        from .ai import run_claude
        from .outputs.prompts import ai_prompt

        text, images, course, recs = self.ai_inputs(day_key, include_slides)
        if not text:
            raise RuntimeError("Transcribe this class day first.")
        prompt = ai_prompt(template)
        if include_slides and images:
            prompt += ("\n\nThe slide images are attached in order; the transcript marks each paragraph with "
                       "(slide N), the slide that was on screen.")
        res = run_claude(prompt, text, images[:60], model=model, cancel=cancel, on_text=on_text)
        stem = day_stem(course, recs[0].start.date(), recs[0].kind)
        folder = course_dir(self.output_root, course, self.lib.courses(True)) / "AI notes"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / safe_name(f"{stem} - Study notes.md")
        usage = res.get("usage") or {}
        tokens_in = sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens",
                                                         "cache_read_input_tokens"))
        footer = (f"\n\n---\n*Generated by Claude ({res.get('model')}) via IEUStudia · "
                  f"{tokens_in:,} input / {usage.get('output_tokens', 0):,} output tokens.*\n")
        path.write_text(res["text"].rstrip() + footer, encoding="utf-8")
        return path
