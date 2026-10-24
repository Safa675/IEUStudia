"""Transcription jobs: downloads run in parallel, transcription on one GPU worker, slides alongside.

Pipeline per recording:  Panopto details → audio download → decode (CPU) ──► GPU: transcribe
                                         └► screen keyframes → slides (parallel) ─┘──► write files
"""

from __future__ import annotations

import dataclasses as dc
import errno
import hashlib
import json
import logging
import queue
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

import requests

from . import hls, store
from .cancel import Cancelled
from .config import Settings
from .library import Library
from .net import plain_error
from .panopto import NeedPanoptoAuth, NotReady, Panopto
from .paths import audio_cache_dir
from .quality import SILENT_DBFS, NoSpeech, is_silent
from .winutil import KeepAwake

if TYPE_CHECKING:
    import numpy as np

log = logging.getLogger("studia")


def decode_audio(path: Path, cancel: Optional[threading.Event] = None) -> np.ndarray:
    """Decode with PyAV, loaded on first use: importing it takes seconds and the app starts without it."""
    from .audio import decode_audio as _decode

    return _decode(path, cancel)
Emit = Callable[[dict[str, Any]], None]
IDLE_UNLOAD_S = 600   # free the GPU memory 10 minutes after the last job
DECODED_MAX = 2       # decoded lectures in memory at once (~345 MB each for 90 minutes): one transcribing, one next
PENDING_KEY = "outputs_pending"     # library meta: recordings whose files in the output folder could not be written
_ETA = re.compile(r"\s*·\s*~(\d+)s left")

_STATUS = {"queued": "queued", "downloading": "downloading", "transcribing": "transcribing",
           "writing": "transcribing", "done": "done", "failed": "failed", "processing": "processing"}


@dc.dataclass
class Job:
    id: str
    title: str
    course_id: str = ""
    course_code: str = ""
    local_path: Optional[Path] = None
    stage: str = "queued"
    progress: float = 0.0
    detail: str = ""
    error: str = ""
    cancel: threading.Event = dc.field(default_factory=threading.Event)
    audio: Optional[np.ndarray] = None
    audio_path: Optional[Path] = None
    info: Optional[dict] = None
    slides: Optional[Future] = None
    attempts: int = 0
    holds_audio: bool = False        # one of the DECODED_MAX places for decoded audio
    prev_status: str = "available"
    started: float = dc.field(default_factory=time.monotonic)
    finished: bool = False
    duration_s: float = 0.0          # the recording's length (for the batch's time left)

    def view(self) -> dict[str, Any]:
        detail, eta = self.detail, None
        m = _ETA.search(detail)              # the engine's "… · ~120s left": the page shows it as "about 2 min left"
        if m:
            detail, eta = (detail[:m.start()] + detail[m.end():]).strip(), int(m.group(1))
        return {"id": self.id, "title": self.title, "course_code": self.course_code, "course_id": self.course_id,
                "stage": self.stage, "progress": round(self.progress, 3), "detail": detail, "eta_s": eta,
                "error": self.error, "local": self.local_path is not None}


class JobManager:
    def __init__(self, library: Library, sync: Any, settings: Callable[[], Settings],
                 engine_factory: Callable[[Settings], Any], emit: Emit,
                 writer: Callable[[str], dict[str, Any]], slides_task: Callable[[Job, Any, Any, requests.Session], Any],
                 log_: Optional[logging.Logger] = None) -> None:
        self.lib = library
        self.sync = sync
        self.settings = settings
        self.engine_factory = engine_factory
        self.emit = emit
        self.write_outputs = writer
        self.slides_task = slides_task
        self.log = log_ or log
        self._jobs: dict[str, Job] = {}
        self._lock = threading.RLock()
        self._dl_q: "queue.Queue[Optional[Job]]" = queue.Queue()
        self._gpu_q: "queue.Queue[Optional[Job]]" = queue.Queue()
        self._audio_places = threading.Semaphore(DECODED_MAX)    # downloads still run ahead, as files on disk
        self._awake = KeepAwake("IEUStudia is transcribing lectures")
        self._started = False
        self._engine = None
        self._engine_key: Optional[tuple] = None
        self._slides_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="slides")
        self._last_emit: dict[str, float] = {}
        self._idle_gen = 0

    # ------------------------------------------------------------------ public
    def _start(self) -> None:
        if self._started:
            return
        self._started = True
        for i in range(self.settings().parallel_downloads):
            threading.Thread(target=self._dl_loop, name=f"download-{i + 1}", daemon=True).start()
        threading.Thread(target=self._gpu_loop, name="transcribe", daemon=True).start()

    def enqueue(self, ids: list[str]) -> int:
        n = 0
        with self._lock:
            self._idle_gen += 1          # cancels a pending idle unload
            self._start()
            self._new_batch()
            for did in ids:
                if did in self._jobs:
                    continue
                rec = self.lib.get_recording(did)
                if rec is None:
                    continue
                course = self.lib.get_course(rec.course_id)
                code = course.code if course else ""
                job = Job(id=did, title=f"{code} · {rec.start:%a %d %b} {rec.start:%H:%M}".strip(" ·"),
                          course_id=rec.course_id, course_code=code, prev_status=rec.status,
                          duration_s=max(0.0, rec.duration_s))
                self._jobs[did] = job
                self.lib.set_status(did, "queued")
                self._dl_q.put(job)
                self._update(job, "queued", 0.0, "Waiting", force=True)
                n += 1
            if self._jobs:
                self._awake.on()         # Windows' idle timer must not put the PC to sleep mid-batch
        self._emit_queue()
        return n

    def enqueue_local(self, path: Path) -> str:
        did = "local-" + hashlib.sha1(str(Path(path).resolve()).encode()).hexdigest()[:12]
        with self._lock:
            self._idle_gen += 1
            self._start()
            self._new_batch()
            if did not in self._jobs:
                job = Job(id=did, title=Path(path).name, local_path=Path(path))
                self._jobs[did] = job
                self._dl_q.put(job)
                self._update(job, "queued", 0.0, "Waiting", force=True)
            self._awake.on()
        self._emit_queue()
        return did

    def cancel(self, job_id: Optional[str] = None) -> None:
        with self._lock:
            jobs = [self._jobs[job_id]] if job_id and job_id in self._jobs else ([] if job_id else list(self._jobs.values()))
        for job in jobs:
            job.cancel.set()
            if job.stage == "queued":
                self._finish(job, "cancelled")

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [j.view() for j in self._jobs.values()]

    def _new_batch(self) -> None:
        """Start counting a new batch when work is added to an idle queue (call with the lock held)."""
        if not self._jobs:
            self._batch = {"done": 0, "ended": 0, "audio_done": 0.0, "started": time.monotonic()}

    def batch(self) -> dict[str, Any]:
        """How far the current batch is: finished / total jobs and, once one lecture is done, the time left at the
        speed measured so far (downloads, model load and both languages included)."""
        with self._lock:
            b = getattr(self, "_batch", None)
            jobs = list(self._jobs.values())
        if not b or not jobs:
            return {}
        inflight = sum(j.duration_s * j.progress for j in jobs if j.stage in ("transcribing", "writing"))
        left = sum(j.duration_s for j in jobs) - inflight
        elapsed = time.monotonic() - b["started"]
        work = b["audio_done"] + inflight
        eta = round(left * elapsed / work) if b["audio_done"] > 0 and elapsed > 30 else None
        return {"finished": b["ended"], "total": b["ended"] + len(jobs), "left_s": eta}

    def active_ids(self) -> set[str]:
        with self._lock:
            return set(self._jobs)

    def job(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def busy(self) -> bool:
        with self._lock:
            return bool(self._jobs)

    def note_unwritten(self, delivery_id: str, pending: bool) -> None:
        """Remember (or forget) a transcript whose files in the output folder are missing."""
        with self._lock:
            ids = set(self.unwritten())
            ids = ids | {delivery_id} if pending else ids - {delivery_id}
            self.lib.set_meta(PENDING_KEY, json.dumps(sorted(ids)))

    def unwritten(self) -> list[str]:
        try:
            ids = json.loads(self.lib.get_meta(PENDING_KEY) or "[]")
        except ValueError:
            return []
        return [str(i) for i in ids] if isinstance(ids, list) else []

    def engine_loaded(self) -> bool:
        return self._engine is not None and getattr(self._engine, "loaded", False)

    def unload_engine(self) -> None:
        with self._lock:
            if self._engine is not None and not self._jobs:
                self._engine.unload()
                self._engine = None
                self._engine_key = None
                self.log.info("Speech model unloaded (idle) to free GPU memory")

    def _schedule_idle_unload(self) -> None:
        with self._lock:
            self._idle_gen += 1
            gen = self._idle_gen

        def run() -> None:
            if gen == self._idle_gen:
                self.unload_engine()

        t = threading.Timer(IDLE_UNLOAD_S, run)
        t.daemon = True
        t.start()

    # ------------------------------------------------------------------ events
    def _emit_queue(self) -> None:
        self.emit({"type": "queue", "jobs": self.snapshot(), "batch": self.batch()})

    def _update(self, job: Job, stage: str, progress: float, detail: str, force: bool = False) -> None:
        changed = stage != job.stage
        job.stage, job.progress, job.detail = stage, max(0.0, min(1.0, progress)), detail
        now = time.monotonic()
        if not (force or changed) and now - self._last_emit.get(job.id, 0.0) < 0.25:
            return
        self._last_emit[job.id] = now
        if changed and job.local_path is None and stage in _STATUS:
            self.lib.set_status(job.id, _STATUS[stage])
        self.emit({"type": "job", **job.view()})
        if changed:
            self._emit_queue()

    def _take_audio_place(self, job: Job) -> None:
        """Wait until fewer than DECODED_MAX decoded lectures are in memory (a slow processor may take an hour per
        lecture while the downloads are long done). Cancel works while waiting."""
        if job.holds_audio:
            return
        if not self._audio_places.acquire(blocking=False):
            self._update(job, "queued", 0.0, "Downloaded — waiting for its turn", force=True)
            while not self._audio_places.acquire(timeout=0.5):
                if job.cancel.is_set():
                    raise Cancelled()
        job.holds_audio = True

    def _drop_audio(self, job: Job) -> None:
        job.audio = None
        with self._lock:
            held, job.holds_audio = job.holds_audio, False
        if held:
            self._audio_places.release()

    def _finish(self, job: Job, stage: str, error: str = "") -> None:
        with self._lock:
            if job.finished:          # e.g. cancelled while queued, then picked up by a worker
                return
            job.finished = True
            current = self._jobs.get(job.id) is job
        job.error = error
        self._drop_audio(job)
        with self._lock:
            b = getattr(self, "_batch", None)
            if b is not None and current:
                b["ended"] += 1
                if stage == "done":
                    b["done"] += 1
                    b["audio_done"] += job.duration_s
        status = ""
        if job.local_path is None and current:
            if stage == "cancelled":
                status = job.prev_status if job.prev_status in ("done", "available") else "available"
                self.lib.set_status(job.id, status)
            elif stage == "failed" and job.prev_status == "done":
                status = "done"               # transcribing it again failed: the earlier transcript is still there
                self.lib.set_status(job.id, status)
            elif stage in _STATUS:
                status = _STATUS[stage]
                self.lib.set_status(job.id, status, error)
        job.stage = stage
        if not current:   # a newer job for the same recording owns the UI row and the status
            return
        with self._lock:
            if self._jobs.get(job.id) is job:     # never drop a newer job queued under the same id
                self._jobs.pop(job.id, None)
                self._last_emit.pop(job.id, None)
            idle = not self._jobs
            if idle:
                self._awake.off()
        self.emit({"type": "job", **job.view(), "final": True, "batch_done": idle, "status": status})
        self._emit_queue()
        if idle and self._engine is not None:
            self._schedule_idle_unload()

    # ------------------------------------------------------------------ download side
    def _dl_loop(self) -> None:
        while True:
            job = self._dl_q.get()
            if job is None:
                return
            if job.cancel.is_set():
                self._finish(job, "cancelled")
                continue
            try:
                self._prepare(job)
                if job.cancel.is_set():
                    raise Cancelled()
                self._update(job, "queued", 0.0, "Downloaded — waiting for its turn")    # Cancel ends it at once
                self._gpu_q.put(job)
                continue
            except BaseException as exc:  # noqa: BLE001 - every way out gives back the place for its audio
                self._drop_audio(job)
                error = exc
            if isinstance(error, Cancelled):
                self._finish(job, "cancelled")
            elif isinstance(error, NotReady):
                self._finish(job, "processing", str(error))
            elif isinstance(error, (requests.RequestException, OSError)):
                lasting = isinstance(error, PermissionError) or getattr(error, "errno", None) == errno.ENOSPC
                job.attempts += 1
                if job.attempts <= 2 and not lasting and not job.cancel.is_set():
                    self.log.info("Download problem for %s (%s); retrying", job.id, error)
                    self._update(job, "queued", 0.0, "Connection problem — retrying shortly", force=True)
                    threading.Timer(4.0 * job.attempts, self._dl_q.put, args=(job,)).start()
                else:
                    self.log.warning("Download of %s failed: %s", job.id, error)
                    self._finish(job, "failed", plain_error(error) if lasting else
                                 f"The download failed. {plain_error(error)}")
            elif isinstance(error, Exception):
                self.log.error("Preparing %s failed", job.id, exc_info=error)
                self._finish(job, "failed", plain_error(error))
            else:
                raise error

    def _panopto(self, job: Job) -> tuple[Panopto, requests.Session, dict]:
        s = self.sync.worker_session()
        p = Panopto(s)
        try:
            return p, s, p.delivery_info(job.id)
        except NeedPanoptoAuth:
            self.sync.ensure_panopto(job.course_id or None)
            s = self.sync.worker_session()
            p = Panopto(s)
            return p, s, p.delivery_info(job.id)

    def _prepare(self, job: Job) -> None:
        if job.local_path is not None:
            self._take_audio_place(job)
            self._update(job, "downloading", 0.5, "Reading the file")
            job.audio = decode_audio(job.local_path, job.cancel)
            return
        self._update(job, "downloading", 0.0, "Contacting Panopto", force=True)
        p, s, info = self._panopto(job)
        job.info = info
        sources = p.media_sources(job.id, info)
        settings = self.settings()
        if settings.capture_slides and job.slides is None:
            screen = p.screen_source(info)
            if screen:
                job.slides = self._slides_pool.submit(self.slides_task, job, screen[0], screen[1], self.sync.worker_session())
        last_exc: Optional[BaseException] = None
        silent: Optional[np.ndarray] = None
        for i, src in enumerate(sources):
            if silent is not None:
                # the first audio was silent: only fetch a camera (HLS) stream that demonstrably has sound
                if src.kind != "hls":
                    continue
                self._update(job, "downloading", 0.0, "The audio is silent — checking Panopto's other streams")
                try:
                    peak = hls.audio_peak_dbfs(s, src.url)
                except Exception as exc:  # noqa: BLE001
                    self.log.info("Probe of %s failed: %s", src.kind, exc)
                    continue
                if peak is None or peak < SILENT_DBFS:
                    continue
            dest = audio_cache_dir() / f"{job.id}.{'m4a' if src.kind == 'audio_podcast' else 'mp4'}"

            def prog(done: int, total: int) -> None:
                frac = done / total if total else 0.0
                mb = f"{done / 1e6:.0f}/{total / 1e6:.0f} MB" if total > 1000 else ""
                self._update(job, "downloading", frac, f"Downloading audio {mb}".strip())

            try:
                p.download(src, dest, prog, job.cancel, prefer="audio")
            except Cancelled:
                raise
            except Exception as exc:  # noqa: BLE001 - try the next source
                self.log.info("Source %s failed for %s: %s", src.kind, job.id, exc)
                last_exc = exc
                continue
            job.audio_path = dest
            self._take_audio_place(job)
            self._update(job, "downloading", 1.0, "Decoding audio")
            audio = decode_audio(dest, job.cancel)
            if is_silent(audio) and i + 1 < len(sources):
                self.log.info("%s: %s audio is silent; looking for another stream", job.id, src.kind)
                silent = audio
                continue
            job.audio = audio
            break
        if job.audio is None:
            if silent is not None:
                job.audio = silent    # the engine reports "no sound" with a clear message
            else:
                raise last_exc or RuntimeError("No downloadable audio for this recording.")
        self.sync.absorb(s)

    # ------------------------------------------------------------------ GPU side
    def _get_engine(self, settings: Settings) -> Any:
        key = (settings.model, settings.device, tuple(settings.languages))
        if self._engine is None or self._engine_key != key:
            if self._engine is not None:
                self._engine.unload()
            self._engine = self.engine_factory(settings)
            self._engine_key = key
        return self._engine

    def _gpu_loop(self) -> None:
        while True:
            job = self._gpu_q.get()
            if job is None:
                return
            if job.cancel.is_set():
                self._finish(job, "cancelled")
                continue
            try:
                self._transcribe(job)
            except Cancelled:
                self._finish(job, "cancelled")
            except NoSpeech as exc:
                if job.local_path is None:
                    self.lib.set_warnings(job.id, [{"code": "no_speech", "level": "error", "message": str(exc)}])
                self._finish(job, "failed", str(exc))
            except Exception as exc:  # noqa: BLE001
                self.log.exception("Transcribing %s failed", job.id)
                self._finish(job, "failed", plain_error(exc))
            finally:
                self._drop_audio(job)

    def _engine_progress(self, job: Job, stage: str, frac: float, detail: str) -> None:
        phase = {"model-download": "download", "model-load": "load"}.get(stage)
        if phase:
            self.emit({"type": "engine", "phase": phase, "progress": round(frac, 3), "message": detail})
            self._update(job, "transcribing", 0.0, detail)
        else:
            self._update(job, "transcribing", frac, detail)

    def _transcribe(self, job: Job) -> None:
        settings = self.settings()
        engine = self._get_engine(settings)
        self._update(job, "transcribing", 0.0, "Preparing the speech model", force=True)
        engine.ensure_model(lambda st, f, d: self._engine_progress(job, st, f, d), job.cancel)
        self.emit({"type": "engine", "phase": "ready", "progress": 1.0, "message": "Model ready"})
        meta: dict[str, Any] = {"delivery_id": job.id}
        if job.info:
            d = job.info.get("Delivery") or {}
            meta.update(expected_duration=float(d.get("Duration") or 0.0), recording_name=d.get("SessionName", ""))
        if job.local_path is not None:
            meta["source_path"] = str(job.local_path)
        t = engine.transcribe(job.audio, lambda st, f, d: self._engine_progress(job, st, f, d), job.cancel, meta)
        self._drop_audio(job)
        t.delivery_id = job.id
        path = store.save_transcript(t)
        quality = (t.meta or {}).get("quality") or {}
        warnings = quality.get("warnings") or []
        if job.local_path is None:
            self.lib.save_transcript(t, path, quality=quality.get("stats"))
            self.lib.set_warnings(job.id, warnings)
        if job.slides is not None:
            self._update(job, "writing", 0.97, "Finishing slides", force=True)
            try:
                job.slides.result(timeout=900)
            except Exception as exc:  # noqa: BLE001 - slides are optional
                self.log.warning("Slides for %s failed: %s", job.id, exc)
        self._update(job, "writing", 0.99, "Writing files", force=True)
        try:
            self.write_outputs(job.id)
        except OSError as exc:            # the transcript is saved: its files are written again at the next start
            self.log.warning("Writing the files of %s failed: %s", job.id, exc)
            self.note_unwritten(job.id, True)
            if job.local_path is None:
                self.lib.set_warnings(job.id, warnings + [{
                    "code": "not_written", "level": "warning",
                    "message": f"Transcribed, but its files in your IEUStudia folder could not be written. "
                               f"{plain_error(exc)} IEUStudia tries again the next time it starts."}])
        if job.audio_path and not settings.keep_audio:
            for p in (job.audio_path,):
                try:
                    Path(p).unlink(missing_ok=True)
                except OSError:
                    pass
        self._finish(job, "done")
