"""Local transcription engine (faster-whisper / CTranslate2) with English-Turkish code-switching."""

from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
from collections import Counter
from typing import Any, Callable, Optional, Sequence

import numpy as np

from .. import __version__
from ..cancel import Cancelled, check
from ..domain import Segment, Transcript
from ..quality import NoSpeech, analyze_audio, analyze_transcript
from .core import (SR, DecodeOptions, Decoded, Unit, UnitDecoder, choose, clean_text,
                   hallucination_score, segment_language, sort_by_duration, speech_units)
from .models import (MODEL_PRESETS, delete_model, detect_gpu, download_model, local_model_path,
                     model_is_downloaded, resolve_device, setup_cuda_dlls)

__all__ = ["Engine", "Cancelled", "NoSpeech", "MODEL_PRESETS", "detect_gpu", "model_is_downloaded",
           "delete_model", "ENGINE_VERSION"]

ENGINE_VERSION = "2.0"
UNCLEAR = "[unclear]"
Progress = Callable[[str, float, str], None]


def _work(u: Unit) -> float:
    """A passage's share of the transcription time (see _decode_units)."""
    return 1.0 + u.dur / 12.0
log = logging.getLogger("studia")


class Engine:
    """Load a Whisper model once and transcribe many recordings with it."""

    def __init__(self, model: str = "large-v3-turbo", device: str = "auto",
                 languages: Sequence[str] = ("en", "tr"), log: Optional[logging.Logger] = None) -> None:
        if model not in MODEL_PRESETS:
            raise ValueError(f"Unknown model '{model}'.")
        self.model_name = model
        self.device_pref = device
        self.languages = tuple(dict.fromkeys(l.strip().lower() for l in languages if l.strip())) or ("en",)
        self.log = log or logging.getLogger("studia")
        self._wm = None
        self._decoder: Optional[UnitDecoder] = None
        self._device = "cpu"
        self._compute = "int8"
        self._warning = ""
        self._lock = threading.Lock()
        self._last_dominant = "en"
        self._oom_batch = 0

    # ------------------------------------------------------------------ model lifecycle
    @property
    def loaded(self) -> bool:
        return self._wm is not None

    @property
    def device_info(self) -> dict[str, Any]:
        gpu = detect_gpu()
        return {"device": self._device, "compute_type": self._compute,
                "gpu": gpu.get("name", "") if self._device == "cuda" else "", "warning": self._warning}

    def ensure_model(self, progress: Progress, cancel: Optional[threading.Event] = None) -> None:
        with self._lock:
            if self._wm is not None:
                return
            path = local_model_path(self.model_name)
            if path is None:
                size = MODEL_PRESETS[self.model_name]["size_mb"]
                progress("model-download", 0.0, f"Downloading the {self.model_name} model (~{size} MB, one time)")

                def cb(done: int, total: int) -> None:
                    frac = done / total if total else 0.0
                    progress("model-download", frac,
                             f"Downloading model {done / 1e6:,.0f} / {total / 1e6:,.0f} MB")

                path = download_model(self.model_name, cb, cancel)
            check(cancel)
            progress("model-load", 0.0, "Loading the speech model")
            setup_cuda_dlls()
            from faster_whisper import WhisperModel

            device, compute = resolve_device(self.device_pref)
            threads = max(1, (os.cpu_count() or 4) - 2)
            t = time.monotonic()
            try:
                wm = WhisperModel(str(path), device=device, compute_type=compute, cpu_threads=threads)
                if device == "cuda":  # surface missing cuBLAS/cuDNN now rather than mid-job
                    wm.encode(np.zeros((1, wm.model.n_mels, 3000), dtype=np.float32))
            except Exception as exc:  # noqa: BLE001
                if device != "cuda":
                    raise
                self.log.warning("GPU initialisation failed, falling back to CPU: %s", exc)
                self._warning = "The GPU could not be used, so transcription runs on the CPU (slower)."
                device, compute = "cpu", "int8"
                wm = WhisperModel(str(path), device=device, compute_type=compute, cpu_threads=threads)
            self._wm, self._device, self._compute = wm, device, compute
            self._decoder = UnitDecoder(wm)
            supported = set(getattr(wm, "supported_languages", []) or [])
            if supported:
                self.languages = tuple(l for l in self.languages if l in supported) or ("en",)
            self.log.info("Model %s loaded on %s/%s in %.1fs", self.model_name, device, compute,
                          time.monotonic() - t)
            progress("model-load", 1.0, f"Model ready ({'GPU' if device == 'cuda' else 'CPU'})")

    def unload(self) -> None:
        """Release the model (and its GPU memory)."""
        with self._lock:
            if self._wm is not None:
                try:
                    self._wm.model.unload_model()
                except Exception:  # noqa: BLE001 - dropping the reference frees it anyway
                    pass
            self._wm = None
            self._decoder = None

    # ------------------------------------------------------------------ transcription
    def transcribe(self, audio: np.ndarray, progress: Progress, cancel: Optional[threading.Event],
                   meta: Optional[dict[str, Any]] = None) -> Transcript:
        if self._wm is None or self._decoder is None:
            self.ensure_model(progress, cancel)
        assert self._wm is not None and self._decoder is not None
        meta = dict(meta or {})
        timings: dict[str, float] = {}
        duration = len(audio) / SR

        t = time.monotonic()
        progress("vad", 0.02, "Finding speech")
        spans, units = self._units(audio, cancel)
        timings["vad"] = time.monotonic() - t
        speech_s = sum(u.dur for u in units)
        self.log.info("%d speech units, %.0fs of speech in %.0fs of audio", len(units), speech_s, duration)

        # safeguards: raises quality.NoSpeech for silent recordings, boosts quiet ones
        warnings, stats, gain = analyze_audio(audio, spans, float(meta.get("expected_duration") or 0.0))
        if gain != 1.0:
            audio = np.clip(audio * gain, -1.0, 1.0)

        t = time.monotonic()
        decoded, best_conf = self._decode_units(audio, units, progress, cancel)
        timings["decode"] = time.monotonic() - t

        t = time.monotonic()
        progress("finalize", 0.97, "Cleaning up")
        segments: list[Segment] = []
        lang_secs: Counter[str] = Counter()
        dominant = self._last_dominant

        def unclear(u: Unit, conf: float) -> None:
            if segments and segments[-1].text == UNCLEAR and u.start / SR - segments[-1].end < 3.0:
                segments[-1].end = round(u.end / SR, 2)
                return
            segments.append(Segment(start=round(u.start / SR, 2), end=round(u.end / SR, 2), text=UNCLEAR,
                                    lang=dominant, conf=round(conf, 3)))

        for u, d, top in zip(units, decoded, best_conf):
            if d is None:
                continue
            if hallucination_score(d, u.dur) >= 0.75:
                if u.dur >= 4.0 and top < 0.45:
                    unclear(u, top)
                continue
            if top < 0.38:  # neither language makes sense of it: say so instead of inventing words
                if u.dur >= 3.0:
                    unclear(u, top)
                continue
            text = clean_text(d.text)
            if not text:
                continue
            lang = segment_language(d)
            segments.append(Segment(start=round(u.start / SR, 2), end=round(u.end / SR, 2), text=text,
                                    lang=lang, conf=round(d.conf, 3)))
            lang_secs[lang] += u.dur
        post_warnings, post_stats = analyze_transcript(segments, speech_s)
        meta["quality"] = {"warnings": warnings + post_warnings, "stats": {**stats, **post_stats}}
        timings["finalize"] = time.monotonic() - t
        progress("finalize", 1.0, "Done")
        info = self.device_info
        return Transcript(
            delivery_id=str(meta.get("delivery_id", "")),
            segments=segments,
            duration_s=round(duration, 2),
            speech_s=round(speech_s, 2),
            language_seconds={k: round(v, 1) for k, v in lang_secs.most_common()},
            model=self.model_name,
            device=info["device"] + (f" ({info['gpu']})" if info.get("gpu") else ""),
            engine_version=ENGINE_VERSION,
            created_at=dt.datetime.now().isoformat(timespec="seconds"),
            timings={k: round(v, 2) for k, v in timings.items()},
            meta=meta,
        )

    # ------------------------------------------------------------------ internals
    def _units(self, audio: np.ndarray, cancel: Optional[threading.Event]) -> tuple[list[tuple[int, int]], list[Unit]]:
        from faster_whisper.vad import VadOptions, get_speech_timestamps

        if len(audio) < SR // 2:
            return [], []
        check(cancel)
        opts = VadOptions(threshold=0.5, min_speech_duration_ms=200, min_silence_duration_ms=300,
                          speech_pad_ms=200, max_speech_duration_s=14.0)
        speech = get_speech_timestamps(audio, opts)
        check(cancel)
        return [(int(s["start"]), int(s["end"])) for s in speech], speech_units(speech)

    def _batch_size(self) -> int:
        if self._device != "cuda":
            return 4
        big_decoder = self.model_name in ("large-v3", "medium")
        vram = detect_gpu().get("vram_mb") or 8000
        base = 4 if big_decoder else 16
        return max(1, base if vram >= 7500 else base // 2)

    def _encode_generate(self, audio: np.ndarray, batch: list[Unit], langs_per_pass: list[list[str]],
                         opts: DecodeOptions) -> list[list[Decoded]]:
        """Encode once, generate once per language pass. Halves the batch on CUDA out-of-memory."""
        dec = self._decoder
        assert dec is not None
        try:
            enc = dec.encode(dec.features(audio, batch))
            return [dec.generate(enc, batch, langs, opts) for langs in langs_per_pass]
        except RuntimeError as exc:
            if "out of memory" not in str(exc).lower() or len(batch) == 1:
                raise
            half = len(batch) // 2
            self.log.warning("GPU out of memory with %d passages per batch; retrying with %d", len(batch), half)
            self._oom_batch = half
            a = self._encode_generate(audio, batch[:half], [l[:half] for l in langs_per_pass], opts)
            b = self._encode_generate(audio, batch[half:], [l[half:] for l in langs_per_pass], opts)
            return [x + y for x, y in zip(a, b)]

    def _decode_units(self, audio: np.ndarray, units: list[Unit], progress: Progress,
                      cancel: Optional[threading.Event]) -> tuple[list[Optional[Decoded]], list[float]]:
        n = len(units)
        if n == 0:
            return [], []
        langs = list(self.languages)
        opts = DecodeOptions(batch_size=self._batch_size(), beam_size=5 if self._device == "cuda" else 2)
        self._oom_batch = 0
        cands: list[list[Decoded]] = [[] for _ in range(n)]
        order = sort_by_duration(units, range(n))
        done = 0
        started = time.monotonic()
        # what a passage costs: one encoder pass (always 30 s of audio, however short the passage), plus decoding
        # that grows with its length (measured on a laptop processor: encoding is about 12 s of decoding)
        work_total = sum(_work(u) for u in units) or 1.0
        work_done = 0.0
        i = 0
        while i < n:
            check(cancel)
            size = min(opts.batch_size, self._oom_batch or opts.batch_size)
            idx = order[i:i + size]
            batch = [units[j] for j in idx]
            passes = self._encode_generate(audio, batch, [[lang] * len(batch) for lang in langs], opts)
            for results in passes:
                for j, d in zip(idx, results):
                    cands[j].append(d)
            i += len(idx)
            done += len(idx)
            work_done += sum(_work(u) for u in batch)
            frac = work_done / work_total
            elapsed = time.monotonic() - started
            eta = elapsed / frac * (1 - frac) if frac > 0.05 else None
            progress("transcribe", 0.05 + 0.88 * frac,
                     f"Transcribing passage {done} of {n}" + (f" · ~{int(eta)}s left" if eta else ""))

        dominant = self._dominant(cands, units)
        self._last_dominant = dominant
        chosen: list[Optional[Decoded]] = [choose(c, units[k].dur, dominant) if c else None
                                           for k, c in enumerate(cands)]
        chosen = self._retry_bad(audio, units, chosen, opts, cancel, progress)
        best_conf = [max((d.conf for d in c if hallucination_score(d, units[k].dur) < 0.75), default=0.0)
                     for k, c in enumerate(cands)]
        return chosen, best_conf

    @staticmethod
    def _dominant(cands: list[list[Decoded]], units: list[Unit]) -> str:
        votes: Counter[str] = Counter()
        for c, u in zip(cands, units):
            if len(c) > 1:
                best = max(c, key=lambda d: d.avg_logprob)
                second = sorted(d.avg_logprob for d in c)[-2]
                if best.avg_logprob - second > 0.05:
                    votes[best.lang] += u.dur
            elif c:
                votes[c[0].lang] += u.dur
        return votes.most_common(1)[0][0] if votes else "en"

    def _retry_bad(self, audio: np.ndarray, units: list[Unit], chosen: list[Optional[Decoded]],
                   opts: DecodeOptions, cancel: Optional[threading.Event],
                   progress: Progress = lambda *a: None) -> list[Optional[Decoded]]:
        """Re-decode units with repetition loops / very low confidence using temperature fallback."""
        dec = self._decoder
        assert dec is not None
        bad = [k for k, d in enumerate(chosen)
               if d is not None and d.text and (d.compression_ratio > 2.4 or d.avg_logprob < -1.0)
               and hallucination_score(d, units[k].dur) < 0.75]
        if not bad:
            return chosen
        self.log.info("Retrying %d low-quality passages", len(bad))
        size = max(1, min(opts.batch_size, self._oom_batch or opts.batch_size))
        first = len(bad)
        for round_no, temp in enumerate((0.3, 0.6)):
            if not bad:
                break
            still = []
            by_lang: dict[str, list[int]] = {}
            for k in bad:
                by_lang.setdefault(chosen[k].lang, []).append(k)  # type: ignore[union-attr]
            groups = [ks[i:i + size] for ks in by_lang.values() for i in range(0, len(ks), size)]
            for g, idx in enumerate(groups):
                check(cancel)
                progress("transcribe", 0.93 + 0.02 * (round_no + g / len(groups)) / 2,
                         f"Checking {first} unclear passage{'s' if first != 1 else ''} again")
                batch = [units[k] for k in idx]
                langs = [chosen[k].lang for k in idx]  # type: ignore[union-attr]
                retry = self._encode_generate(audio, batch, [langs], DecodeOptions(temperature=temp, best_of=3))[0]
                for k, d in zip(idx, retry):
                    old = chosen[k]
                    assert old is not None
                    better = d.text and d.compression_ratio <= 2.4 and d.avg_logprob > old.avg_logprob
                    if better:
                        chosen[k] = d
                    if chosen[k].compression_ratio > 2.4 or chosen[k].avg_logprob < -1.0:  # type: ignore[union-attr]
                        still.append(k)
            bad = still
        return chosen
