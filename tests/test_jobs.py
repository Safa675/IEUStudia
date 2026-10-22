"""JobManager with fake Panopto / audio / engine: statuses, fallbacks, cancel, cleanup."""

import datetime as dt
import threading
import time

import numpy as np
import pytest

from studia import jobs as jobs_mod
from studia.config import Settings
from studia.domain import Course, Recording, Segment, Transcript
from studia.library import Library
from studia.panopto import MediaSource, NotReady
from studia.paths import audio_cache_dir
from studia.quality import NoSpeech


def wait_for(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


class FakeSync:
    def worker_session(self):
        return object()

    def absorb(self, s):
        pass

    def ensure_panopto(self, cid=None):
        pass


class FakePanopto:
    sources = []                   # list[MediaSource], set per test
    downloads: list = []
    gate = None                    # threading.Event to hold downloads
    not_ready = False

    def __init__(self, session):
        pass

    def delivery_info(self, did):
        return {"Delivery": {"Duration": 60.0, "SessionName": "x"}}

    def media_sources(self, did, info):
        if FakePanopto.not_ready:
            raise NotReady("Panopto is still processing this recording.")
        return list(FakePanopto.sources)

    def screen_source(self, info):
        return None

    def download(self, src, dest, progress, cancel=None, prefer="audio"):
        if FakePanopto.gate is not None:
            FakePanopto.gate.wait(10)
        FakePanopto.downloads.append(src.kind)
        progress(5, 10)
        dest.write_text("silent" if "silent" in src.url else "loud")
        progress(10, 10)
        return dest


class FakeEngine:
    loaded = True

    def __init__(self, fail=None):
        self.fail = fail
        self.audio_seen = []
        self.unloaded = False

    def ensure_model(self, progress, cancel=None):
        progress("model-load", 1.0, "ready")

    def unload(self):
        self.unloaded = True

    def transcribe(self, audio, progress, cancel, meta):
        self.audio_seen.append(float(np.max(np.abs(audio))))
        if self.fail:
            raise self.fail
        progress("transcribe", 0.5, "half")
        return Transcript(delivery_id=meta["delivery_id"], segments=[Segment(0, 1, "hello", "en", 0.9)],
                          duration_s=1, speech_s=1, language_seconds={"en": 1}, model="m", device="cpu",
                          engine_version="t", created_at="2026-10-20T10:00:00", meta=dict(meta, quality={}))


@pytest.fixture
def env(tmp_path, monkeypatch):
    lib = Library(tmp_path / "lib.db")
    lib.upsert_courses([Course(id="c", title="t", code="EEE 215", name="n")])
    lib.merge_recordings("c", [Recording(delivery_id=d, course_id="c", folder_id="f", folder_path="", name=d,
                                         start=dt.datetime(2026, 10, 13, 11, i), duration_s=2400, viewer_url="u")
                               for i, d in enumerate(["r1", "r2"])], first_sync=True)
    FakePanopto.sources = [MediaSource("audio_podcast", "https://x/loud")]
    FakePanopto.downloads = []
    FakePanopto.gate = None
    FakePanopto.not_ready = False
    monkeypatch.setattr(jobs_mod, "Panopto", FakePanopto)
    monkeypatch.setattr(jobs_mod, "decode_audio", lambda p, cancel=None: np.full(
        16000, 0.0 if open(p).read() == "silent" else 0.5, dtype=np.float32))
    events, written = [], []
    state = {"engine": FakeEngine()}

    def make(**kw):
        settings = Settings(output_dir=str(tmp_path / "out"), parallel_downloads=1, capture_slides=False, **kw)
        return jobs_mod.JobManager(lib, FakeSync(), lambda: settings, lambda s: state["engine"], events.append,
                                   lambda did: written.append(did) or {}, lambda *a: [])

    return {"lib": lib, "events": events, "written": written, "make": make, "state": state, "mp": monkeypatch}


def finals(events, did):
    return [e for e in events if e.get("type") == "job" and e.get("id") == did and e.get("final")]


def test_happy_path_writes_files_and_cleans_audio(env):
    jm = env["make"]()
    jm.enqueue(["r1"])
    assert wait_for(lambda: finals(env["events"], "r1"))
    assert finals(env["events"], "r1")[0]["stage"] == "done"
    assert env["lib"].get_recording("r1").status == "done"
    assert env["written"] == ["r1"]
    stages = {e["stage"] for e in env["events"] if e.get("type") == "job" and e.get("id") == "r1"}
    assert {"queued", "downloading", "transcribing", "done"} <= stages
    assert not list(audio_cache_dir().glob("r1.*"))           # keep_audio=False


def test_not_ready_marks_processing(env):
    FakePanopto.not_ready = True
    jm = env["make"]()
    jm.enqueue(["r1"])
    assert wait_for(lambda: finals(env["events"], "r1"))
    rec = env["lib"].get_recording("r1")
    assert rec.status == "processing" and "processing" in rec.error


def test_silent_podcast_falls_back_to_loud_camera_stream(env):
    FakePanopto.sources = [MediaSource("audio_podcast", "https://x/silent"),
                           MediaSource("video_podcast", "https://x/silent-video"),
                           MediaSource("hls", "https://x/dv.m3u8")]
    env["mp"].setattr(jobs_mod.hls, "audio_peak_dbfs", lambda s, url: -6.0)
    jm = env["make"]()
    jm.enqueue(["r1"])
    assert wait_for(lambda: finals(env["events"], "r1"))
    assert FakePanopto.downloads == ["audio_podcast", "hls"]   # the video podcast is never fetched
    assert env["state"]["engine"].audio_seen == [0.5]


def test_no_speech_is_reported_with_a_warning(env):
    env["state"]["engine"] = FakeEngine(fail=NoSpeech("This recording has no sound on Panopto."))
    jm = env["make"]()
    jm.enqueue(["r1"])
    assert wait_for(lambda: finals(env["events"], "r1"))
    assert env["lib"].get_recording("r1").status == "failed"
    assert env["lib"].warnings("r1")[0]["code"] == "no_speech"


def test_model_is_unloaded_when_idle(env):
    env["mp"].setattr(jobs_mod, "IDLE_UNLOAD_S", 0.3)
    jm = env["make"]()
    jm.enqueue(["r1"])
    assert wait_for(lambda: finals(env["events"], "r1"))
    assert wait_for(lambda: env["state"]["engine"].unloaded, timeout=5)
    assert not jm.engine_loaded()


def test_cancel_queued_then_requeue_keeps_tracking_new_job(env):
    FakePanopto.gate = threading.Event()          # hold r1 in "downloading"
    jm = env["make"]()
    jm.enqueue(["r1"])
    assert wait_for(lambda: jm.job("r1") is not None and jm.job("r1").stage == "downloading")
    jm.enqueue(["r2"])                            # waits behind r1 (one download worker)
    jm.cancel("r2")
    assert env["lib"].get_recording("r2").status == "available"
    jm.enqueue(["r2"])                            # re-queue before the worker reaches the stale entry
    FakePanopto.gate.set()
    assert wait_for(lambda: any(e["stage"] == "done" for e in finals(env["events"], "r2")))
    assert env["lib"].get_recording("r2").status == "done"
    assert wait_for(lambda: not jm.busy())


def test_batch_progress_counts_finished_lectures_and_estimates_time_left(env):
    FakePanopto.gate = threading.Event()          # hold r1 in "downloading"
    jm = env["make"]()
    jm.enqueue(["r1", "r2"])
    assert wait_for(lambda: jm.job("r1") is not None and jm.job("r1").stage == "downloading")
    b = jm.batch()
    assert b["finished"] == 0 and b["total"] == 2 and b["left_s"] is None     # no speed measured yet
    queue_events = [e for e in env["events"] if e.get("type") == "queue"]
    assert queue_events and queue_events[-1]["batch"]["total"] == 2
    jm._batch["started"] -= 120                   # pretend the first lecture took two minutes
    FakePanopto.gate.set()
    assert wait_for(lambda: finals(env["events"], "r1"))
    b = jm.batch()
    if b:                                         # r2 may already be finished as well
        assert b["finished"] == 1 and b["total"] == 2 and b["left_s"] and b["left_s"] > 0
    assert wait_for(lambda: not jm.busy())
    assert jm.batch() == {}                       # idle: no batch
    jm.enqueue(["r1"])                            # a new batch starts from zero
    assert jm.batch()["finished"] == 0 and jm.batch()["total"] == 1
    assert wait_for(lambda: not jm.busy())
