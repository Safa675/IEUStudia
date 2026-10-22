"""Minimal HLS helpers for Panopto streams (single fragmented-MP4 files addressed with byte ranges)."""

from __future__ import annotations

import dataclasses as dc
import io
import re
from typing import Any, Optional
from urllib.parse import urljoin

import requests


@dc.dataclass
class Segment:
    t: float
    dur: float
    url: str
    offset: int
    length: int


def variants(text: str, base: str) -> list[dict[str, Any]]:
    out = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF:") and i + 1 < len(lines):
            attrs = dict(re.findall(r'([A-Z-]+)=("[^"]*"|[^,]*)', line.split(":", 1)[1]))
            bw = int(attrs.get("AVERAGE-BANDWIDTH", attrs.get("BANDWIDTH", "0")).strip('"') or 0)
            w, _, h = attrs.get("RESOLUTION", "0x0").strip('"').partition("x")
            out.append({"url": urljoin(base, lines[i + 1].strip()), "bandwidth": bw,
                        "codecs": attrs.get("CODECS", "").strip('"'), "width": int(w or 0), "height": int(h or 0)})
    return out


def parse_playlist(text: str, base: str) -> tuple[Optional[tuple[str, int, int]], list[Segment]]:
    init = None
    m = re.search(r'#EXT-X-MAP:URI="([^"]+)"(?:,BYTERANGE="(\d+)@(\d+)")?', text)
    if m:
        init = (urljoin(base, m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0))
    segs: list[Segment] = []
    t, dur, rng = 0.0, None, None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            dur = float(line.split(":", 1)[1].split(",")[0])
        elif line.startswith("#EXT-X-BYTERANGE:"):
            n, _, off = line.split(":", 1)[1].partition("@")
            rng = (int(n), int(off) if off else None)
        elif line and not line.startswith("#") and dur is not None:
            length, off = rng if rng else (0, 0)
            if off is None:
                off = segs[-1].offset + segs[-1].length if segs else 0
            segs.append(Segment(t, dur, urljoin(base, line), off, length))
            t += dur
            dur, rng = None, None
    return init, segs


def get_range(session: requests.Session, url: str, start: int, end: int) -> bytes:
    for attempt in range(4):
        try:
            r = session.get(url, headers={"Range": f"bytes={start}-{end}"}, timeout=(15, 45))
            if r.status_code in (200, 206):
                return r.content if r.status_code == 206 else r.content[start:end + 1]
            r.raise_for_status()
        except requests.RequestException:
            if attempt == 3:
                raise
    return b""


def media_playlist(session: requests.Session, master_url: str, prefer: str = "audio") -> requests.Response:
    r = session.get(master_url)
    r.raise_for_status()
    vs = variants(r.text, r.url)
    if vs:
        if prefer == "audio":
            cands = [v for v in vs if "mp4a" in v["codecs"]] or vs
            v = min(cands, key=lambda v: v["bandwidth"] or 1 << 40)
        else:
            v = max(vs, key=lambda v: (v["height"], v["bandwidth"]))
        r = session.get(v["url"])
        r.raise_for_status()
    return r


def audio_peak_dbfs(session: requests.Session, master_url: str, n_segments: int = 12) -> Optional[float]:
    """Loudest sample (dBFS) in ~2 minutes from the middle of an HLS stream; None if it has no audio."""
    import av
    import numpy as np

    pl = media_playlist(session, master_url, "audio")
    init, segs = parse_playlist(pl.text, pl.url)
    if not init or not segs:
        return None
    mid = max(0, len(segs) // 2 - n_segments // 2)
    data = get_range(session, init[0], init[2], init[2] + init[1] - 1)
    for s in segs[mid:mid + n_segments]:
        data += get_range(session, s.url, s.offset, s.offset + s.length - 1)
    with av.open(io.BytesIO(data)) as c:
        if not c.streams.audio:
            return None
        res = av.AudioResampler(format="s16", layout="mono", rate=16000)
        chunks = [o.to_ndarray().reshape(-1) for f in c.decode(c.streams.audio[0]) for o in res.resample(f)]
    if not chunks:
        return None
    peak = float(np.abs(np.concatenate(chunks)).max()) / 32768.0
    return 20 * float(np.log10(max(peak, 1e-9)))
