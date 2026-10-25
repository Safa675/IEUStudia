"""Robust HTTP file download: parallel Range requests, per-part retries, resume, cancellation.

Used for Whisper model files. (Panopto media uses the same ideas in ``panopto.py``.)
"""

from __future__ import annotations

import errno
import json
import logging
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

import requests

from .cancel import Cancelled, check

log = logging.getLogger("studia")
ProgressFn = Callable[[int, int], None]  # (bytes_done, bytes_total)

CHUNK = 256 * 1024
MIN_PART = 8 * 1024 * 1024


def _probe(session: requests.Session, url: str, attempts: int = 3,
           cancel: Optional[threading.Event] = None) -> tuple[str, int, bool, str]:
    """Follow redirects with a 1-byte range request; return (final_url, total_size, ranges_ok, version), the version
    being the file's ETag or Last-Modified (to tell a changed file from the one whose parts are on disk)."""
    for attempt in range(1, attempts + 1):
        try:
            with session.get(url, headers={"Range": "bytes=0-0"}, stream=True, timeout=(15, 45),
                             allow_redirects=True) as r:
                r.raise_for_status()
                final = r.url
                version = r.headers.get("ETag") or r.headers.get("Last-Modified") or ""
                if r.status_code == 206 and "/" in r.headers.get("Content-Range", ""):
                    total = int(r.headers["Content-Range"].rsplit("/", 1)[1])
                    return final, total, True, version
                return final, int(r.headers.get("Content-Length") or 0), False, version
        except (requests.ConnectionError, requests.Timeout):
            if attempt == attempts:
                raise
            for _ in range(int(2.0 * attempt / 0.25)):
                check(cancel)
                time.sleep(0.25)
    raise IOError("unreachable")


def _fetch_range(session: requests.Session, url: str, part: Path, start: int, end: int,
                 counter: list[int], lock: threading.Lock, cancel: Optional[threading.Event],
                 attempts: int = 8) -> None:
    """Download bytes [start, end] into ``part``, resuming whatever is already there. A Wi-Fi drop of a minute or
    so is waited out."""
    want = end - start + 1
    for attempt in range(1, attempts + 1):
        have = part.stat().st_size if part.exists() else 0
        if have > want:  # corrupt leftover: start this part again
            part.unlink()
            with lock:
                counter[0] -= have
            have = 0
        if have == want:
            return
        try:
            with session.get(url, headers={"Range": f"bytes={start + have}-{end}"},
                             stream=True, timeout=(15, 60)) as r:
                whole_file = r.status_code == 200 and start + have == 0   # server ignored Range: full body
                if r.status_code != 206 and not whole_file:
                    raise requests.HTTPError(f"expected HTTP 206, got {r.status_code}")
                if whole_file and part.exists():
                    part.unlink()
                with open(part, "ab") as fh:
                    for chunk in r.iter_content(CHUNK):
                        check(cancel)
                        if chunk:
                            fh.write(chunk)
                            with lock:
                                counter[0] += len(chunk)
            if part.stat().st_size == want:
                return
        except Cancelled:
            raise
        except (requests.RequestException, OSError) as exc:
            if attempt == attempts:
                raise
            log.info("Download part retry %d/%d (%s)", attempt, attempts, exc)
            for _ in range(int(min(20.0, 0.8 * 2 ** attempt) / 0.25)):
                check(cancel)
                time.sleep(0.25)
    raise IOError("Download did not complete; please retry.")


def _join(part_files: list[Path], tmp: Path) -> None:
    """The parts, in order, as one file: the first part grows and each other one is deleted once appended, so the
    disk never holds the download twice (a 3 GB model needs about 3 GB, not 6), only one part more. Without room for
    that it stops before touching anything, so the next try only joins."""
    need = max((pf.stat().st_size for pf in part_files[1:]), default=0)
    if shutil.disk_usage(tmp.parent).free < need + 16 * 1024 * 1024:
        raise OSError(errno.ENOSPC, "No space left on device", str(tmp.parent))
    os.replace(part_files[0], tmp)
    with open(tmp, "ab") as out:
        for pf in part_files[1:]:
            with open(pf, "rb") as fh:
                while buf := fh.read(4 * 1024 * 1024):
                    out.write(buf)
            try:                                 # already appended: a part an antivirus still holds is only left over
                pf.unlink()
            except OSError as exc:
                log.info("Could not delete %s: %s", pf.name, exc)


def _stream_plain(session: requests.Session, url: str, tmp: Path, counter: list[int],
                  lock: threading.Lock, cancel: Optional[threading.Event]) -> None:
    with session.get(url, stream=True, timeout=(15, 60)) as r:
        r.raise_for_status()
        with open(tmp, "wb") as fh:
            for chunk in r.iter_content(CHUNK):
                check(cancel)
                if chunk:
                    fh.write(chunk)
                    with lock:
                        counter[0] += len(chunk)


def download(url: str, dest: Path, *, session: Optional[requests.Session] = None,
             progress: Optional[ProgressFn] = None, cancel: Optional[threading.Event] = None,
             parts: int = 6) -> Path:
    """Download ``url`` to ``dest`` atomically (``.partN`` files make it resumable)."""
    session = session or requests.Session()
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    final_url, total, ranges_ok, version = _probe(session, url, cancel=cancel)
    if dest.exists() and total and dest.stat().st_size == total:
        if progress:
            progress(total, total)
        return dest
    stamp = {"total": total, "version": version}
    marker = dest.with_name(dest.name + ".parts.json")
    try:
        before = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        before = None
    if before is not None and before != stamp:      # the file changed (e.g. re-encoded): its old parts don't fit
        log.info("%s changed since its partial download; starting again", dest.name)
        for old in dest.parent.glob(dest.name + ".part*"):
            old.unlink(missing_ok=True)
    marker.write_text(json.dumps(stamp), encoding="utf-8")

    if not ranges_ok or total < MIN_PART:
        parts = 1
    bounds: list[tuple[int, int]] = []
    if ranges_ok and total:
        size = -(-total // parts)
        bounds = [(i * size, min(total, (i + 1) * size) - 1) for i in range(parts) if i * size < total]
    part_files = [dest.with_name(f"{dest.name}.part{i}") for i in range(len(bounds))]
    lock = threading.Lock()
    counter = [sum(min(p.stat().st_size, e - s + 1) for p, (s, e) in zip(part_files, bounds) if p.exists())]
    stop = threading.Event()

    def reporter() -> None:
        while not stop.wait(0.25):
            with lock:
                done = counter[0]
            if progress:
                progress(min(done, total) if total else done, total)

    rep = threading.Thread(target=reporter, name="dl-progress", daemon=True)
    rep.start()
    tmp = dest.with_name(dest.name + ".part")
    try:
        if not bounds:
            _stream_plain(session, final_url, tmp, counter, lock, cancel)
        else:
            with ThreadPoolExecutor(max_workers=len(bounds), thread_name_prefix="dl") as pool:
                futures = [pool.submit(_fetch_range, session, final_url, pf, s, e, counter, lock, cancel)
                           for pf, (s, e) in zip(part_files, bounds)]
                for f in futures:
                    f.result()
            _join(part_files, tmp)
    finally:
        stop.set()
        rep.join(timeout=1)
    if total and tmp.stat().st_size != total:
        tmp.unlink(missing_ok=True)
        for pf in part_files:
            pf.unlink(missing_ok=True)
        raise IOError(f"Download size mismatch for {dest.name}; please retry.")
    os.replace(tmp, dest)
    for pf in part_files:                            # the download is complete: a part left over doesn't matter now
        try:
            pf.unlink(missing_ok=True)
        except OSError:
            pass
    if not any(pf.exists() for pf in part_files):    # one left over keeps the stamp, so a changed file won't reuse it
        marker.unlink(missing_ok=True)
    if progress:
        size = dest.stat().st_size
        progress(size, total or size)
    return dest
