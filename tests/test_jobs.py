"""JobManager with fake Panopto / audio / engine: statuses, fallbacks, cancel, cleanup."""

import datetime as dt
import threading
import time
import types

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
        dest.write_text("silent" if "silent" in src.url else "mute" if "mute" in src.url else "loud")
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
        settings = Settings(**{"output_dir": str(tmp_path / "out"), "parallel_downloads": 1, "capture_slides": False,
                               **kw})
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


def test_a_stream_without_any_sound_track_falls_back_to_the_next_one(env):
    from studia.audio import NoAudioTrack

    fake = jobs_mod.decode_audio

    def decode(path, cancel=None):
        if open(path).read() == "mute":              # a video-only stream (made from the screen recording)
            raise NoAudioTrack("This file has no audio track.")
        return fake(path, cancel)

    env["mp"].setattr(jobs_mod, "decode_audio", decode)
    FakePanopto.sources = [MediaSource("video_podcast", "https://x/mute"), MediaSource("hls", "https://x/dv.m3u8")]
    jm = env["make"]()
    jm.enqueue(["r1"])
    assert wait_for(lambda: finals(env["events"], "r1"))
    assert finals(env["events"], "r1")[0]["stage"] == "done" and FakePanopto.downloads == ["video_podcast", "hls"]
    FakePanopto.sources = [MediaSource("video_podcast", "https://x/mute")]   # no stream with sound: said plainly
    jm.enqueue(["r2"])
    assert wait_for(lambda: finals(env["events"], "r2"))
    rec = env["lib"].get_recording("r2")
    assert rec.status == "failed" and "no sound for this recording" in rec.error
    assert not list(audio_cache_dir().glob("r2.*"))


def test_a_stream_without_sound_hides_no_download_error_and_isnt_fetched_again(env):
    import requests

    from studia.audio import NoAudioTrack

    fake, tried = jobs_mod.decode_audio, []

    def decode(path, cancel=None):
        if open(path).read() == "mute":
            raise NoAudioTrack("This file has no audio track.")
        return fake(path, cancel)

    def download(self, src, dest, progress, cancel=None, prefer="audio"):
        tried.append(src.kind)
        if "broken" in src.url:
            raise requests.ConnectionError("Can't reach panopto.ieu.edu.tr.")
        dest.write_text("mute")
        return dest

    env["mp"].setattr(jobs_mod, "decode_audio", decode)
    env["mp"].setattr(FakePanopto, "download", download)
    env["mp"].setattr(jobs_mod.threading, "Timer", lambda s, fn, args=(): types.SimpleNamespace(start=lambda: fn(*args)))
    FakePanopto.sources = [MediaSource("audio_podcast", "https://x/broken"), MediaSource("video_podcast", "https://x/mute")]
    jm = env["make"]()
    jm.enqueue(["r1"])
    assert wait_for(lambda: finals(env["events"], "r1"))
    rec = env["lib"].get_recording("r1")
    assert rec.status == "failed" and rec.error.startswith("The download failed")     # the real problem, retried
    assert tried == ["audio_podcast", "video_podcast", "audio_podcast", "audio_podcast"]  # the mute one only once


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


class BlockingEngine(FakeEngine):
    """A slow processor: each lecture takes until the test lets it go."""

    def __init__(self):
        super().__init__()
        self.go = threading.Event()

    def transcribe(self, audio, progress, cancel, meta):
        while not self.go.wait(0.05):
            if cancel.is_set():
                from studia.cancel import Cancelled
                raise Cancelled()
        return super().transcribe(audio, progress, cancel, meta)


def test_downloads_run_ahead_but_few_lectures_wait_decoded_and_cancel_ends_a_waiting_one(env):
    ids = [f"r{i}" for i in range(1, 7)]
    env["lib"].merge_recordings("c", [Recording(delivery_id=d, course_id="c", folder_id="f", folder_path="", name=d,
                                                start=dt.datetime(2026, 10, 14, 9, i), duration_s=2400, viewer_url="u")
                                      for i, d in enumerate(ids[2:])], first_sync=False)
    env["state"]["engine"] = engine = BlockingEngine()
    awake = []
    env["mp"].setattr(jobs_mod, "KeepAwake", lambda reason: type("K", (), {"on": lambda s: awake.append(True),
                                                                            "off": lambda s: awake.append(False)})())
    jm = env["make"](parallel_downloads=3)
    jm.enqueue(ids)
    assert awake == [True]
    assert wait_for(lambda: sum(1 for i in ids if (jm.job(i) and jm.job(i).detail.startswith("Downloaded"))) >= 2)
    time.sleep(0.3)
    holding = [i for i in ids if jm.job(i) is not None and jm.job(i).audio is not None]
    assert len(holding) <= jobs_mod.DECODED_MAX                     # one transcribing, one ready
    waiting = next(i for i in ids if jm.job(i) and jm.job(i).stage == "queued" and jm.job(i).detail.startswith("Downloaded"))
    jm.cancel(waiting)
    assert jm.job(waiting) is None and env["lib"].get_recording(waiting).status == "available"   # at once
    engine.go.set()
    assert wait_for(lambda: not jm.busy(), timeout=20)
    assert {e["stage"] for i in ids if i != waiting for e in finals(env["events"], i)} == {"done"}
    assert awake[-1] is False                                       # the PC may sleep again


def test_a_transcript_whose_files_cannot_be_written_is_still_done(env):
    def locked(did):
        raise PermissionError(13, "Access is denied", r"C:\Users\x\OneDrive\EEE 215 - Study pack.md")

    lib = env["lib"]
    jm = jobs_mod.JobManager(lib, FakeSync(), lambda: Settings(parallel_downloads=1, capture_slides=False),
                             lambda s: env["state"]["engine"], env["events"].append, locked, lambda *a: [])
    jm.enqueue(["r1"])
    assert wait_for(lambda: finals(env["events"], "r1"))
    assert finals(env["events"], "r1")[0]["stage"] == "done" and lib.get_recording("r1").status == "done"
    (w,) = lib.warnings("r1")
    assert w["code"] == "not_written" and "Study pack.md" in w["message"] and "tries again" in w["message"]
    assert jm.unwritten() == ["r1"]


def test_transcribing_again_that_fails_keeps_the_earlier_transcript(env):
    env["lib"].set_status("r1", "done")
    env["state"]["engine"] = FakeEngine(fail=RuntimeError("boom"))
    jm = env["make"]()
    jm.enqueue(["r1"])
    assert wait_for(lambda: finals(env["events"], "r1"))
    f = finals(env["events"], "r1")[0]
    assert f["stage"] == "failed" and f["status"] == "done" and env["lib"].get_recording("r1").status == "done"


def test_a_full_disk_is_said_plainly_and_not_retried_as_a_connection_problem(env):
    def full(src, dest, progress, cancel=None, prefer="audio"):
        raise OSError(28, "No space left on device")

    env["mp"].setattr(FakePanopto, "download", lambda self, *a, **k: full(*a, **k))
    FakePanopto.sources = [MediaSource("audio_podcast", "https://x/loud")]
    jm = env["make"]()
    jm.enqueue(["r1"])
    assert wait_for(lambda: finals(env["events"], "r1"))
    rec = env["lib"].get_recording("r1")
    assert rec.status == "failed" and rec.error.startswith("Your disk is full")
    assert not any("retrying" in (e.get("detail") or "") for e in env["events"] if e.get("id") == "r1")


def test_the_time_left_is_given_apart_from_the_detail():
    j = jobs_mod.Job(id="x", title="t", detail="Transcribing passage 40 of 600 · ~1500s left")
    v = j.view()
    assert v["detail"] == "Transcribing passage 40 of 600" and v["eta_s"] == 1500


def test_files_that_could_not_be_written_are_written_at_the_next_start(env):
    import types

    from studia.services import Services

    lib = env["lib"]
    lib.set_warnings("r1", [{"code": "not_written", "level": "warning", "message": "x"}, {"code": "gaps", "level": "info"}])
    jm = env["make"]()
    jm.note_unwritten("r1", True)
    jm.note_unwritten("r2", True)
    calls = []

    def write(did):
        calls.append(did)
        if did == "r2":
            raise PermissionError(13, "Access is denied")
        return {}

    Services._write_unwritten(types.SimpleNamespace(jobs=jm, lib=lib, write_outputs=write))
    assert calls == ["r1", "r2"] and jm.unwritten() == ["r2"]           # r2 is tried again next time
    assert [w["code"] for w in lib.warnings("r1")] == ["gaps"]


def test_a_lecture_cancelled_while_it_waits_stays_cancelled_when_its_turn_comes(env):
    env["mp"].setattr(jobs_mod, "DECODED_MAX", 1)
    env["state"]["engine"] = engine = BlockingEngine()
    jm = env["make"](parallel_downloads=2)
    jm.enqueue(["r1", "r2"])

    def waiting():                                     # whichever download finished second waits for the place
        return next((i for i in ("r1", "r2") if jm.job(i) and jm.job(i).detail.startswith("Downloaded")), None)

    assert wait_for(lambda: waiting() is not None)
    cancelled = waiting()
    jm.cancel(cancelled)
    cancelled_at = len(env["events"])
    engine.go.set()                                    # the other finishes: the place comes free, but this one is cancelled
    assert wait_for(lambda: not jm.busy())
    time.sleep(0.3)
    later = [e for e in env["events"][cancelled_at:] if e.get("type") == "job" and e.get("id") == cancelled]
    assert not later and env["lib"].get_recording(cancelled).status == "available"
    assert jm._audio_places.acquire(blocking=False)    # the place was given back


def test_cancel_while_panopto_cant_be_reached_is_a_cancel_not_a_failure(env):
    import requests

    started = threading.Event()

    def unreachable(self, src, dest, progress, cancel=None, prefer="audio"):
        started.set()
        cancel.wait(5)                                        # Cancel is pressed while it tries
        raise requests.ConnectionError("Can't reach panopto.ieu.edu.tr.")

    env["mp"].setattr(FakePanopto, "download", unreachable)
    jm = env["make"]()
    jm.enqueue(["r1"])
    assert started.wait(5)
    jm.cancel("r1")
    assert wait_for(lambda: finals(env["events"], "r1"))
    assert finals(env["events"], "r1")[0]["stage"] == "cancelled" and env["lib"].get_recording("r1").status == "available"
