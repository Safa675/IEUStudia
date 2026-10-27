"""Wires settings, library, sync, jobs, outputs, slides, export and AI together (used by the GUI and the CLI)."""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable, Optional

import requests

from . import __version__, store
from .config import Settings, apply_patch, load_settings, save_settings
from .domain import Course, Recording
from .i18n import N_, t
from .library import Library
from .log import setup_logging
from .outputs.writer import course_dir, day_file_name, day_stem, safe_name, write_day, write_local, write_pack
from .paths import db_path
from .sync import SyncService

log = logging.getLogger("studia")
RENAME_KEY = "output_renamed"          # library meta: an output folder rename that is not finished yet
# AI notes: the end of the file name (English), and the label shown (t() it there)
NOTE_KINDS = {"notes": N_("Study notes"), "exam": N_("Exam focus"), "flashcards": N_("Flashcards")}


class Services:
    def __init__(self, debug: bool = False, migrate_output: bool = True) -> None:
        """``migrate_output``: rename an output folder of the app's earlier name (only the app does that, never a
        command-line run next to it)."""
        self.log = setup_logging(debug)
        self._lock = threading.RLock()
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self.settings: Settings = load_settings()
        self.lib = Library(db_path(), app_version=__version__)    # a new version keeps a copy first
        self.lib.reset_interrupted()
        if migrate_output:
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
        self._write_unwritten()

    def _write_unwritten(self) -> None:
        """Write the files of transcripts whose files could not be written before (a file was open in another
        program, the folder was blocked or offline)."""
        for did in self.jobs.unwritten():
            try:
                self.write_outputs(did)
            except OSError as exc:
                log.info("The files of %s still can't be written: %s", did, exc)
                continue
            except Exception:  # noqa: BLE001 - e.g. the recording is gone; never stop the start for it
                log.exception("Writing the files of %s failed", did)
            self.jobs.note_unwritten(did, False)
            if not did.startswith("local-"):
                self.lib.set_warnings(did, [w for w in self.lib.warnings(did) if w.get("code") != "not_written"])

    def shutdown(self) -> None:
        self.plans.shutdown()
        self.chat.shutdown()

    def _migrate_output_root(self) -> None:
        """An output folder that still has the app's earlier name (Documents\\Studia or Documents\\LectureScribe, a
        profile's Documents\\Studia - <name>) gets the IEUStudia name once: it is renamed (same drive, so instant)
        unless the new name is taken, and the paths stored for its files follow. Folders the user chose keep theirs.

        The rename is noted in the library right away, so a start that stops before the paths and the setting follow
        (they can take seconds) is finished by the next one, even if something put a folder of the old name back."""
        import json

        from .paths import _home_override, renamed_output_path

        if _home_override() is not None:
            return
        try:
            begun = json.loads(self.lib.get_meta(RENAME_KEY) or "{}")
        except ValueError:
            begun = {}
        if begun.get("to") and Path(begun["to"]).is_dir():
            self._finish_rename(Path(begun["from"]), Path(begun["to"]))
            return
        old = Path(self.settings.output_dir)
        new = renamed_output_path(old)
        if new is None or not old.parent.is_dir():
            return
        try:
            if old.is_dir():
                if new.is_dir() and not any(new.iterdir()):
                    new.rmdir()                                # empty, e.g. made by an earlier start
                if not new.exists():
                    os.replace(old, new)
                    self.lib.set_meta(RENAME_KEY, json.dumps({"from": str(old), "to": str(new)}, ensure_ascii=False))
                elif any((old / name).is_dir() for name in self._app_folders()):
                    self.log.info("Keeping the output folder %s: %s exists too", old, new)
                    return
                # else another profile sharing the folder renamed it, and something put the old name back
            elif not new.is_dir():
                return                                         # neither is there (a drive not connected?): wait
        except OSError:                                        # e.g. a file in it is open: try again next start
            self.log.warning("Could not rename %s to %s", old, new, exc_info=True)
            return
        self._finish_rename(old, new)

    def _finish_rename(self, old: Path, new: Path) -> None:
        self.rebase_output_paths(old, new)
        self.update_settings({"output_dir": str(new)})
        self.lib.set_meta(RENAME_KEY, "")
        self.log.info("Output folder %s -> %s", old, new)

    def _app_folders(self) -> set[str]:
        """The folders the app writes in the output folder: one per course, local files, weekly digests."""
        from .outputs.writer import course_folder_name

        courses = self.lib.courses(True)
        return {course_folder_name(c, courses) for c in courses} | {"Local files", "Weekly digests"}

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
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError as exc:     # e.g. Windows' ransomware protection guards Documents: writing a file says so
            log.warning("The output folder %s can't be created: %s", p, exc)
        return p

    def day(self, day_key: str) -> tuple[Course, list[Recording]]:
        course_id = day_key.split("|", 1)[0]
        course = self.lib.get_course(course_id)
        if course is None:
            raise KeyError(t("Unknown course"))
        recs = sorted((r for r in self.lib.recordings(course_id) if r.day_key == day_key), key=lambda r: r.start)
        if not recs:
            raise KeyError(t("Unknown class day"))
        return course, recs

    def day_files(self, day_key: str) -> dict[str, Any]:
        """The day's files that exist: "md", "pack", "pdf", "zip", "notes:<kind>" for each kind of AI notes,
        "notes" (the newest of those) and "notes_kinds" (newest first)."""
        course, recs = self.day(day_key)
        all_courses = self.lib.courses(True)
        folder = course_dir(self.output_root, course, all_courses)
        md = folder / day_file_name(course, recs[0].start.date(), recs[0].kind)
        stem = day_stem(course, recs[0].start.date(), recs[0].kind)
        pack = folder / safe_name(f"{course.code or course.name} - Study pack.md")
        out: dict[str, Any] = {"folder": str(folder), "md": str(md) if md.exists() else "",
                               "pack": str(pack) if pack.exists() else ""}
        pdf = folder / "AI packs" / safe_name(f"{stem} - AI pack.pdf")
        zp = folder / "AI packs" / safe_name(f"{stem} - AI pack.zip")
        out.update(pdf=str(pdf) if pdf.exists() else "", zip=str(zp) if zp.exists() else "")
        notes = []
        for kind in NOTE_KINDS:
            p = self._notes_path(folder, stem, kind)
            if p.exists():
                out[f"notes:{kind}"] = str(p)
                notes.append((p.stat().st_mtime, kind))
        notes.sort(reverse=True)
        out["notes_kinds"] = [k for _, k in notes]
        out["notes"] = out[f"notes:{notes[0][1]}"] if notes else ""
        return out

    @staticmethod
    def _notes_path(folder: Path, stem: str, kind: str) -> Path:
        return folder / "AI notes" / safe_name(f"{stem} - {NOTE_KINDS.get(kind, NOTE_KINDS['notes'])}.md")

    # ------------------------------------------------------------------ outputs
    def write_outputs(self, delivery_id: str) -> dict[str, Any]:
        if delivery_id.startswith("local-"):
            transcript = store.load_transcript(delivery_id)
            if transcript is None:
                return {}
            name = (transcript.meta or {}).get("source_path", delivery_id)
            return {"md": write_local(self.output_root, name, transcript)}
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
        warnings = {r.delivery_id: [w for w in self.lib.warnings(r.delivery_id) if w.get("code") != "not_written"]
                    for r in course_recs}           # "could not be written" never goes into the files themselves
        return write_day(self.output_root, course, all_courses, day_recs, {i: trans_all[i] for i in ids if i in trans_all},
                         course_recs, trans_all, self.settings.write_srt, slides, warnings)

    def relocate_outputs(self, old_root: Path, new_root: Path) -> int:
        """Copy the app's folders (course folders with transcripts, slides, materials, packs and notes; weekly
        digests) to a new output folder and point the stored paths there. The old folder is left untouched. Returns
        the number of folders copied."""
        import shutil

        old_root, new_root = Path(old_root), Path(new_root)
        if not old_root.exists() or old_root.resolve() == new_root.resolve():
            return 0
        copied = 0
        for name in sorted(self._app_folders()):
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
            raise RuntimeError(t("Transcribe this class day first."))
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
        from .ai import PLAN_AIS, PROVIDER_LABELS, provider, run_claude
        from .outputs.prompts import ai_prompt

        reads_images = provider() in PLAN_AIS
        text, images, course, recs = self.ai_inputs(day_key, include_slides and reads_images)
        if not text:
            raise RuntimeError(t("Transcribe this class day first."))
        prompt = ai_prompt(template)
        if include_slides and images:
            prompt += ("\n\nThe slide images are attached in order; the transcript marks each paragraph with "
                       "(slide N), the slide that was on screen.")
        res = run_claude(prompt, text, images[:60], model=model, cancel=cancel, on_text=on_text)
        stem = day_stem(course, recs[0].start.date(), recs[0].kind)
        path = self._notes_path(course_dir(self.output_root, course, self.lib.courses(True)), stem, template)
        path.parent.mkdir(parents=True, exist_ok=True)
        usage = res.get("usage") or {}
        tokens_in = sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens",
                                                         "cache_read_input_tokens"))
        who = PROVIDER_LABELS.get(provider()) or "AI"
        footer = (f"\n\n---\n*Generated by {who} ({res.get('model')}) via IEUStudia · "
                  f"{tokens_in:,} input / {usage.get('output_tokens', 0):,} output tokens.*\n")
        path.write_text(res["text"].rstrip() + footer, encoding="utf-8")
        return path
