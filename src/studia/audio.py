"""Audio decoding with PyAV (bundled FFmpeg libraries, no ffmpeg.exe needed)."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional, Union

import av
import numpy as np

from .cancel import Cancelled, check
from .i18n import t

SAMPLE_RATE = 16000
log = logging.getLogger("studia")

__all__ = ["SAMPLE_RATE", "NoAudioTrack", "decode_audio", "media_duration", "Cancelled"]


class NoAudioTrack(ValueError):
    """A file with no sound track at all (a video-only stream, a screen recording)."""


def decode_audio(path: Union[str, Path], cancel: Optional[threading.Event] = None) -> np.ndarray:
    """Decode the first audio track of any media file to 16 kHz mono float32 in [-1, 1].

    Corrupt packets are skipped instead of aborting the whole decode, so a lecture with a
    damaged second or two still transcribes.
    """
    chunks: list[np.ndarray] = []
    skipped = 0
    with av.open(str(path), mode="r") as container:
        streams = [s for s in container.streams if s.type == "audio"]
        if not streams:
            raise NoAudioTrack(t("This file has no audio track."))
        stream = streams[0]
        stream.thread_type = "AUTO"
        resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        packets = 0
        for packet in container.demux(stream):
            packets += 1
            if packets % 500 == 0:
                check(cancel)
            try:
                frames = packet.decode()
            except av.error.FFmpegError:
                skipped += 1
                continue
            for frame in frames:
                frame.pts = None
                for out in resampler.resample(frame):
                    chunks.append(out.to_ndarray().reshape(-1).copy())
        for out in resampler.resample(None):
            chunks.append(out.to_ndarray().reshape(-1).copy())
    if skipped:
        log.warning("Skipped %d of %d corrupt audio packets in %s", skipped, packets, Path(path).name)
    if not chunks:
        if skipped:
            raise ValueError(t("The audio could not be decoded (the file seems to be damaged)."))
        return np.zeros(0, dtype=np.float32)
    pcm = np.concatenate(chunks)
    audio = pcm.astype(np.float32)
    audio *= 1.0 / 32768.0
    return audio


def media_duration(path: Union[str, Path]) -> float:
    """Duration of a media file in seconds (0.0 if unknown)."""
    try:
        with av.open(str(path), mode="r") as container:
            if container.duration:
                return float(container.duration) / av.time_base
            for s in container.streams:
                if s.type == "audio" and s.duration and s.time_base:
                    return float(s.duration * s.time_base)
    except Exception:  # noqa: BLE001 - best effort only
        log.debug("Could not read duration of %s", path, exc_info=True)
    return 0.0
