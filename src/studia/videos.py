"""YouTube videos for video study plans: search, metadata, chapters and captions through yt-dlp (no API key).

A video is verified by what is *said* in it - its captions - not by its title. Only text is fetched, never
video or audio: a search is one results page; an inspection is the watch page and player response plus one
caption file (json3, else vtt / srv1 / ttml). The captions are a human track in a preferred language, else the
video's original automatic track; YouTube's machine translations (~150 languages) are never used.

Everything is cached under ``<app>/cache/videos``: searches for 7 days, inspections for 30 (3 when a video has
no captions yet: YouTube may still generate them). ``yt_dlp`` is imported lazily - it takes ~0.5 s to import
and only study plans need it.
"""

from __future__ import annotations

import bisect
import dataclasses as dc
import datetime as dt
import hashlib
import html
import json
import logging
import math
import os
import re
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit

import requests

from .i18n import N_, t
from .net import USER_AGENT
from .paths import app_dir

log = logging.getLogger("studia")

SEARCH_TTL_S = 7 * 86400
INFO_TTL_S = 30 * 86400
NO_CAPTIONS_TTL_S = 3 * 86400          # automatic captions can appear a while after upload
SOCKET_TIMEOUT_S = 20
MIN_DURATION_S = 90                    # shorter: Shorts, trailers, teasers
WINDOW_TEXT_CHARS = 2500
CAPTION_EXTS = ("json3", "vtt", "srv1", "ttml", "srv3", "srv2", "srt")   # preference order
BLOCKING_KINDS = frozenset({"bot", "rate_limit"})                       # YouTube refuses this connection

# Channels known for careful teaching get a small ranking boost (matched ignoring case, spaces and punctuation,
# as a prefix: "NPTEL" also covers "NPTEL-NOC IITM").
EDU_CHANNELS = (
    "3Blue1Brown", "Khan Academy", "MIT OpenCourseWare", "Professor Leonard", "The Organic Chemistry Tutor",
    "Organic Chemistry Tutor", "Dr. Trefor Bazett", "StatQuest with Josh Starmer", "patrickJMT", "Eddie Woo",
    "NPTEL", "Neso Academy", "ALL ABOUT ELECTRONICS", "Ben Eater", "Steve Brunton", "Stanford Online",
    "CrashCourse", "Computerphile", "Numberphile", "freeCodeCamp.org",
)

# rank(): weights of the heuristic
TITLE_WEIGHT, DESCRIPTION_WEIGHT, VIEWS_WEIGHT, EDU_BOOST = 3.0, 0.75, 0.3, 1.0
# windows(): bonuses, and when overlapping windows merge (at most MAX_REGION_WINDOWS x window_s long)
COVERAGE_BONUS, CHAPTER_BONUS, MERGE_FRACTION, MAX_REGION_WINDOWS = 1.0, 2.0, 0.5, 2.0
# cues: fragments shorter than TINY_CUE_CHARS join a neighbour less than MERGE_GAP_S away
MERGE_GAP_S, TINY_CUE_CHARS, MAX_CUE_CHARS = 1.0, 20, 200


@dc.dataclass
class VideoHit:
    """A search result: what the results page says (nothing verified yet)."""

    id: str
    title: str
    channel: str = ""
    duration_s: float = 0.0                # 0 if unknown
    url: str = ""
    view_count: Optional[int] = None
    upload_date: str = ""                  # "YYYYMMDD"; usually "" (results pages only say "3 years ago")
    description: str = ""                  # the results page's snippet
    query: str = ""                        # the search that found it

    def to_json(self) -> dict[str, Any]:
        return dc.asdict(self)

    @staticmethod
    def from_json(d: dict[str, Any]) -> "VideoHit":
        return VideoHit(**_known(VideoHit, d))


@dc.dataclass
class Cue:
    start: float                           # seconds
    end: float
    text: str


@dc.dataclass
class Chapter:
    start: float
    end: float
    title: str


@dc.dataclass
class VideoInfo:
    """What a video is, and what is said in it."""

    id: str
    title: str
    channel: str = ""
    channel_id: str = ""
    duration_s: float = 0.0
    url: str = ""
    upload_date: str = ""                  # "YYYYMMDD"
    view_count: Optional[int] = None
    language: str = ""                     # spoken language ("en", "en-US", "tr"); "" if unknown
    description: str = ""                  # the first ~1500 characters
    chapters: list[Chapter] = dc.field(default_factory=list)
    cues: list[Cue] = dc.field(default_factory=list)
    caption_lang: str = ""                 # language of the cues, e.g. "en"
    caption_kind: str = ""                 # "manual" | "auto" | "" (no usable captions)
    fetched_at: str = ""                   # when it was inspected (ISO)

    @property
    def has_captions(self) -> bool:
        return bool(self.cues)

    def to_json(self) -> dict[str, Any]:
        return dc.asdict(self)

    @staticmethod
    def from_json(d: dict[str, Any]) -> "VideoInfo":
        info = VideoInfo(**{k: v for k, v in _known(VideoInfo, d).items() if k not in ("chapters", "cues")})
        info.chapters = [Chapter(float(c["start"]), float(c["end"]), str(c["title"])) for c in d.get("chapters") or []]
        info.cues = [Cue(float(c["start"]), float(c["end"]), str(c["text"])) for c in d.get("cues") or []]
        return info


class VideoError(Exception):
    """A search or video that failed. ``str()`` is a short message for the user; ``kind`` says why: bot,
    rate_limit, private, members, age, geo, upcoming, payment, unavailable, timeout, network, cancelled, error."""

    def __init__(self, message: str, kind: str = "error") -> None:
        super().__init__(message)
        self.kind = kind


def _known(cls: Any, d: dict[str, Any]) -> dict[str, Any]:
    return {f.name: d[f.name] for f in dc.fields(cls) if f.name in d}


# ------------------------------------------------------------------ small helpers
_ID = re.compile(r"[A-Za-z0-9_-]{11}")
_ID_IN_URL = re.compile(r"(?:[?&]v=|youtu\.be/|/shorts/|/embed/|/live/|/v/)([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])")


def watch_url(video_id: str, start: Optional[float] = None) -> str:
    """``https://www.youtube.com/watch?v=ID``, plus ``&t=123s`` to start at a moment."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    if start is not None and math.isfinite(start) and int(start) > 0:
        url += f"&t={int(start)}s"
    return url


def fmt_ts(seconds: Optional[float]) -> str:
    """75 -> "1:15", 3725 -> "1:02:05"."""
    try:
        s = max(0, int(seconds or 0))
    except (TypeError, ValueError, OverflowError):
        s = 0
    h, rest = divmod(s, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _video_id(value: str) -> str:
    """A video id, also from a watch / youtu.be / Shorts URL. Validated: it names a cache file."""
    value = (value or "").strip()
    if _ID.fullmatch(value):
        return value
    m = _ID_IN_URL.search(value)
    if m:
        return m.group(1)
    raise VideoError(t("That isn't a YouTube video."), "unavailable")


def _num(v: Any) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return f if math.isfinite(f) and f > 0 else 0.0


def _int(v: Any) -> Optional[int]:
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        return None


def _line(v: Any) -> str:
    return " ".join(str(v or "").split())


def _date(d: dict[str, Any]) -> str:
    """``YYYYMMDD`` from yt-dlp's upload_date, else from its timestamp."""
    value = str(d.get("upload_date") or "")
    if re.fullmatch(r"\d{8}", value):
        return value
    ts = d.get("timestamp") or d.get("release_timestamp")
    if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > 0:
        return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y%m%d")
    return ""


def _check(cancel: Optional[threading.Event]) -> None:
    if cancel is not None and cancel.is_set():
        raise VideoError(t("Cancelled"), "cancelled")


# ------------------------------------------------------------------ yt-dlp
class _YdlLog:
    """yt-dlp's messages go to our log, never to a console (the app usually has none)."""

    def debug(self, msg: str) -> None:
        pass

    info = debug

    def warning(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    error = warning


def _ydl(**extra: Any) -> Any:
    import yt_dlp  # lazily: slow to import, and only study plans need it

    return yt_dlp.YoutubeDL({
        "quiet": True, "no_warnings": True, "skip_download": True, "socket_timeout": SOCKET_TIMEOUT_S,
        "retries": 3, "extractor_retries": 2, "color": "never", "logger": _YdlLog(),
        "cachedir": str(app_dir() / "cache" / "yt-dlp"), **extra,
    })


# (kind, pattern in yt-dlp's error text, message for the user, in English: t() when raised): the first match wins
_ERRORS = (
    ("bot", r"not a bot|captcha|unusual traffic", N_("YouTube asked to confirm you're not a bot. Try again later.")),
    ("private", r"private video|video is private", N_("This video is private.")),
    ("members", r"members[- ]only|join this channel|channel members", N_("This video is for the channel's members only.")),
    ("age", r"confirm your age|age[- ]restricted|inappropriate for some users", N_("This video is age-restricted.")),
    ("geo", r"in your country|geo[- ]?restrict", N_("This video isn't available in your country.")),
    ("upcoming", r"premieres? in|will begin in|live event will begin", N_("This video hasn't been released yet.")),
    ("payment", r"requires payment|purchase this video|rent this video", N_("This video has to be bought or rented.")),
    ("rate_limit", r"http error 429|too many requests|\b429\b|try again later|rate[- ]?limit",
     N_("YouTube is limiting requests right now. Try again in a few minutes.")),
    ("timeout", r"timed? ?out", N_("YouTube took too long to answer. Try again.")),
    ("network", r"getaddrinfo|name resolution|resolve host|nodename|connection (?:refused|reset|aborted|error)|"
                r"network is unreachable|no route to host|remote end closed|failed to establish|\bssl\b|"
                r"certificate|errno 1100\d", N_("Can't reach YouTube. Check your internet connection.")),
    ("unavailable", r"unavailable|not available|no longer|removed|terminated|deleted|does not exist|"
                    r"incomplete youtube id|unsupported url|not a valid url", N_("This video is unavailable.")),
)


def _video_error(exc: BaseException, default: str) -> VideoError:
    """yt-dlp's long, technical error -> a short message for the user (``default``: English, marked with N_)."""
    if isinstance(exc, VideoError):
        return exc
    text = str(exc)
    inner = (getattr(exc, "exc_info", None) or (None, None))[1]    # DownloadError keeps the original error
    if inner is not None and str(inner) not in text:
        text += f" ({inner})"
    text = _line(re.sub(r"\x1b\[[\d;]*m", "", text))
    kind, message = next(((k, msg) for k, rx, msg in _ERRORS if re.search(rx, text, re.I)), ("error", default))
    (log.warning if kind in BLOCKING_KINDS else log.info)("YouTube: %s (%s)", message, text[:300])
    return VideoError(t(message), kind)


# ------------------------------------------------------------------ search
_UNWATCHABLE = {"private", "premium_only", "subscriber_only", "needs_auth"}


def search(query: str, limit: int = 8, *, cancel: Optional[threading.Event] = None) -> list[VideoHit]:
    """Up to ``limit`` videos for a query: one YouTube results page (~2-4 s), cached for 7 days.

    Live streams, upcoming premieres, Shorts (< 90 s) and members-only videos are dropped, so a search may
    return fewer than ``limit`` hits."""
    q = _line(query)
    if not q:
        return []
    limit = max(1, min(int(limit or 8), 50))
    path = _cache_dir() / "search" / f"{hashlib.sha1(q.casefold().encode('utf-8')).hexdigest()}.json"
    cached = _read_json(path) if _fresh(path, SEARCH_TTL_S) else None
    if isinstance(cached, dict) and isinstance(cached.get("hits"), list):
        try:
            if int(cached.get("limit") or 0) >= limit:               # a bigger search covers a smaller one
                return [dc.replace(VideoHit.from_json(h), query=q) for h in cached["hits"][:limit]]
        except (TypeError, ValueError):
            pass
    _check(cancel)
    t0 = time.monotonic()
    with _ydl(extract_flat=True) as ydl:
        try:
            result = ydl.extract_info(f"ytsearch{limit}:{q}", download=False)
        except Exception as exc:  # noqa: BLE001 - yt-dlp wraps every failure in DownloadError
            raise _video_error(exc, N_("YouTube search failed. Try again later.")) from exc
    hits: list[VideoHit] = []
    for entry in list((result or {}).get("entries") or []):
        hit = _hit(entry, q)
        if hit is not None and all(h.id != hit.id for h in hits):
            hits.append(hit)
    log.info("YouTube search %r: %d videos (%.1f s)", q, len(hits), time.monotonic() - t0)
    if hits:                          # an empty page may be a hiccup: don't remember it for a week
        _write_json(path, {"query": q, "limit": limit, "saved_at": _now(), "hits": [h.to_json() for h in hits]})
    return hits


def _hit(e: Any, query: str) -> Optional[VideoHit]:
    """A results-page entry -> VideoHit; None for what can't be studied (live, upcoming, Shorts, members-only)."""
    if not isinstance(e, dict):
        return None
    vid, url = str(e.get("id") or ""), str(e.get("url") or "")
    if not _ID.fullmatch(vid) or e.get("ie_key") not in (None, "Youtube"):
        return None                                    # no id, or a channel / playlist
    if e.get("live_status") in ("is_live", "is_upcoming", "post_live") or e.get("availability") in _UNWATCHABLE:
        return None
    duration = _num(e.get("duration"))
    if "/shorts/" in url or 0 < duration < MIN_DURATION_S:
        return None
    return VideoHit(vid, _line(e.get("title")), _line(e.get("channel") or e.get("uploader")), duration,
                    watch_url(vid), _int(e.get("view_count")), _date(e), _line(e.get("description"))[:300], query)


# ------------------------------------------------------------------ inspect
def inspect(video_id: str, *, langs: tuple[str, ...] = ("en",), cancel: Optional[threading.Event] = None) -> VideoInfo:
    """Metadata, chapters and captions of one video (~2-5 s; cached for 30 days - a cached video needs no network).

    ``langs`` are the preferred caption languages, best first. Raises :class:`VideoError` when YouTube refuses
    (private, unavailable, age-restricted, members-only, bot check, rate limit) or can't be reached, and
    ``VideoError("Cancelled")`` when ``cancel`` is set. No usable captions is not an error: then
    ``caption_kind`` is "" and ``has_captions`` False."""
    vid = _video_id(video_id)
    langs = tuple(str(lang).strip() for lang in langs if str(lang).strip()) or ("en",)
    cached = _cached_info(vid, langs)
    if cached is not None:
        return cached
    _check(cancel)
    t0 = time.monotonic()
    with _ydl() as ydl:
        try:
            # process=False skips format selection: formats don't matter here, and a video whose formats YouTube
            # withholds (PO tokens) would fail with "Requested format is not available" despite its captions
            raw = ydl.extract_info(watch_url(vid), download=False, process=False)
        except Exception as exc:  # noqa: BLE001 - yt-dlp wraps every failure in DownloadError
            raise _video_error(exc, N_("Couldn't read this video from YouTube.")) from exc
        if not isinstance(raw, dict) or raw.get("_type", "video") != "video":
            raise VideoError(t("This video is unavailable."), "unavailable")
        info = _video_info(vid, raw)
        for lang, kind, formats in _caption_tracks(raw, langs):
            _check(cancel)
            try:
                cues = _fetch_cues(ydl, formats, cancel)
            except _FetchError as exc:   # not "no captions": don't remember the video without them
                raise _video_error(exc, N_("Couldn't download this video's captions.")) from exc
            if cues:
                info.cues, info.caption_lang, info.caption_kind = cues, lang, kind
                break
    log.info("Video %s: %s captions (%s, %d cues), %d chapters (%.1f s)", vid, info.caption_kind or "no",
             info.caption_lang or "-", len(info.cues), len(info.chapters), time.monotonic() - t0)
    _write_json(_info_path(vid), {**info.to_json(), "langs": list(langs)})
    return info


def _video_info(vid: str, raw: dict[str, Any]) -> VideoInfo:
    duration = _num(raw.get("duration"))
    return VideoInfo(
        id=vid, title=_line(raw.get("title")), channel=_line(raw.get("channel") or raw.get("uploader")),
        channel_id=str(raw.get("channel_id") or ""), duration_s=duration, url=watch_url(vid), upload_date=_date(raw),
        view_count=_int(raw.get("view_count")), language=_language(raw),
        description=str(raw.get("description") or "").strip()[:1500],
        chapters=_chapters(raw.get("chapters"), duration), fetched_at=_now())


def _chapters(items: Any, duration: float) -> list[Chapter]:
    """yt-dlp's chapters; unprocessed ones have no end_time (the next chapter's start, or the video's end)."""
    items = [c for c in items or [] if isinstance(c, dict)]
    out = []
    for i, c in enumerate(items):
        start = _num(c.get("start_time"))
        end = _num(c.get("end_time")) or (_num(items[i + 1].get("start_time")) if i + 1 < len(items) else duration)
        out.append(Chapter(start, max(end, start), _line(c.get("title")) or f"Chapter {i + 1}"))
    return out


# ------------------------------------------------------------------ captions: which track
def _base(lang: str) -> str:
    """"en-US" -> "en", "en-orig" -> "en", "pt-BR" -> "pt"."""
    return re.split(r"[-_]", (lang or "").strip().lower(), maxsplit=1)[0]


def _translated(url: str) -> bool:
    """YouTube's machine translations carry ``tlang`` (inside HLS playlist URLs too, percent-encoded)."""
    return "tlang=" in unquote(url or "")


def _tracks(d: Any) -> dict[str, list[dict[str, Any]]]:
    """yt-dlp's {language: formats}, without live chat and machine translations."""
    out: dict[str, list[dict[str, Any]]] = {}
    for key, formats in (d.items() if isinstance(d, dict) else ()):
        if key == "live_chat" or not isinstance(formats, list):
            continue
        good = [f for f in formats if isinstance(f, dict) and f.get("url") and not _translated(str(f["url"]))]
        if good:
            out[str(key)] = good
    return out


def _find(tracks: dict[str, Any], lang: str) -> Optional[str]:
    """The track for a language: exact ("en"), else a variant ("en-GB", "en-US")."""
    keys = [k for k in tracks if not k.endswith("-orig")]
    want = lang.lower()
    return (next((k for k in keys if k.lower() == want), None)
            or next((k for k in keys if k.lower() == _base(want)), None)
            or next((k for k in keys if _base(k) == _base(want)), None))


def _language(raw: dict[str, Any]) -> str:
    """The spoken language: yt-dlp's, else the original audio track's, else the original captions'."""
    if raw.get("language"):
        return str(raw["language"])
    audio = [(f.get("language_preference") or 0, str(f["language"])) for f in raw.get("formats") or []
             if isinstance(f, dict) and f.get("language") and f.get("acodec") != "none"]
    original = [lang for pref, lang in audio if pref >= 10]          # 10 "original", 5 "default", -1 dubbed
    if original:
        return original[0]
    orig = next((str(k) for k in raw.get("automatic_captions") or {} if str(k).endswith("-orig")), "")
    if orig:
        return orig[:-5]
    default = [lang for pref, lang in audio if pref >= 5]
    return default[0] if default else ""


def _lang_label(key: str) -> str:
    """Track key -> language: "en-orig" -> "en", "pt-BR" stays, "en-nP7-2PuUl7o" (a named track) -> "en"."""
    key = key[:-5] if key.endswith("-orig") else key
    return key if re.fullmatch(r"[A-Za-z]{2,3}(?:-(?:[A-Za-z]{2,4}|\d{3}))?", key) else _base(key)


def _caption_tracks(raw: dict[str, Any], langs: tuple[str, ...]) -> list[tuple[str, str, list[dict[str, Any]]]]:
    """The caption tracks worth trying, best first, as (language, "manual" | "auto", formats):

    1. a human track in a preferred language (exact, then a variant: "en", then "en-GB");
    2. the original automatic track ("<lang>-orig", or the one in the video's language) in a preferred language;
    3. the video's own language - its human track, else its original automatic track - when no preferred
       language has one (the planner decides whether it can use such a video)."""
    manual, auto = _tracks(raw.get("subtitles")), _tracks(raw.get("automatic_captions"))
    spoken = _language(raw)
    origs = [k for k in auto if k.endswith("-orig")]
    orig = (next((k for k in origs if _base(k) == _base(spoken)), None) or (origs[0] if origs else None)
            or (_find(auto, spoken) if spoken else None))
    wanted = {_base(lang) for lang in langs}
    picks = [("manual", _find(manual, lang)) for lang in langs]
    picks += [("auto", orig if orig and _base(orig) in wanted else None)]
    picks += [("auto", _find(auto, lang)) for lang in langs]          # untranslated, e.g. ASR without "-orig"
    picks += [("manual", _find(manual, spoken) if spoken else None), ("auto", orig)]
    out, seen = [], set()
    for kind, key in picks:
        formats = (manual if kind == "manual" else auto).get(key or "")
        if formats and formats[0]["url"] not in seen:     # "en-orig" and "en" are often the same track
            seen.add(formats[0]["url"])
            out.append((_lang_label(str(key)), kind, formats))
    return out


# ------------------------------------------------------------------ captions: download and parse
class _FetchError(Exception):
    """A caption file could not be downloaded (network or HTTP error)."""


def _download(ydl: Any, url: str) -> str:
    """A small text file through yt-dlp's HTTP stack (YouTube's headers and cookies), else plain requests."""
    try:
        resp = ydl.urlopen(url)
        try:
            data = resp.read()
        finally:
            getattr(resp, "close", lambda: None)()
    except Exception as first:  # noqa: BLE001 - yt-dlp's network errors differ per backend
        log.debug("yt-dlp couldn't fetch captions (%s), trying requests", first)
        try:
            r = requests.get(url, timeout=SOCKET_TIMEOUT_S, headers={"User-Agent": USER_AGENT})
            r.raise_for_status()
            data = r.content
        except requests.RequestException as exc:
            raise _FetchError(f"{first} | {exc}") from exc
    return data.decode("utf-8", "replace") if isinstance(data, (bytes, bytearray)) else str(data)


def _fetch_cues(ydl: Any, formats: list[dict[str, Any]], cancel: Optional[threading.Event]) -> list[Cue]:
    """One track's cues: json3 first, then other text formats (at most 3 tries); an HLS-only track
    (segmented WebVTT) through its playlist. Raises _FetchError when the files can't be downloaded."""
    direct: dict[str, dict[str, Any]] = {}
    for f in formats:
        if not str(f.get("protocol") or "").startswith("m3u8"):
            direct.setdefault(str(f.get("ext") or ""), f)
    for ext in [e for e in CAPTION_EXTS if e in direct][:3]:
        _check(cancel)
        cues = _parse_captions(_download(ydl, str(direct[ext]["url"])))
        if cues:
            return cues
        log.debug("Empty or unreadable %s captions", ext)    # e.g. YouTube wants a PO token: an empty file
    hls = next((f for f in formats if str(f.get("protocol") or "").startswith("m3u8")), None)
    return _hls_cues(ydl, str(hls["url"]), cancel) if hls is not None and not direct else []


def _hls_cues(ydl: Any, url: str, cancel: Optional[threading.Event]) -> list[Cue]:
    """Segmented WebVTT (an HLS playlist). Its segments are timedtext URLs with a time range; without the
    range, one json3 file holds the whole track. Else the VTT segments (absolute times) are joined."""
    playlist = _download(ydl, url)
    segments = [urljoin(url, s.strip()) for s in playlist.splitlines() if s.strip() and not s.startswith("#")]
    if not segments:
        return []
    _check(cancel)
    try:
        cues = _parse_captions(_download(ydl, _with_query(segments[0], fmt="json3", range=None)))
    except _FetchError:
        cues = []
    if cues:
        return cues
    out: list[Cue] = []
    for seg in segments[:50]:
        _check(cancel)
        out += _parse_vtt(_download(ydl, seg))
    return _tidy(out)


def _with_query(url: str, **params: Optional[str]) -> str:
    """``url`` with query parameters replaced (None removes one)."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in params]
    query += [(k, v) for k, v in params.items() if v is not None]
    return urlunsplit(parts._replace(query=urlencode(query)))


def _parse_captions(text: str) -> list[Cue]:
    """Any of YouTube's caption formats (recognised by content) -> tidy cues; [] if unreadable."""
    head = text.lstrip("\ufeff \t\r\n")[:300]
    try:
        if head.startswith("{"):
            cues = _parse_json3(text)
        elif head.startswith("WEBVTT") or "-->" in head:
            cues = _parse_vtt(text)
        elif head.startswith("<"):
            cues = _parse_xml(text)
        else:
            return []
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        log.debug("Unreadable captions: %s", exc)
        return []
    return _tidy(cues)


def _parse_json3(text: str) -> list[Cue]:
    """json3: events with tStartMs / dDurationMs and text segments. Layout-only events (no segments, or just
    a newline - automatic captions have one per line break) are skipped."""
    cues = []
    for ev in json.loads(text).get("events") or []:
        if not isinstance(ev, dict) or not ev.get("segs") or "tStartMs" not in ev:
            continue
        body = _line("".join(str(s.get("utf8") or "") for s in ev["segs"] if isinstance(s, dict)))
        if body:
            start = float(ev["tStartMs"]) / 1000
            cues.append(Cue(start, start + float(ev.get("dDurationMs") or 0) / 1000, body))
    return cues


_TIMING = re.compile(r"((?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3})\s*-->\s*((?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3})")


def _clock(value: str) -> float:
    """"01:02:03.450", "02:03,450", "3.45s", "3450ms" -> seconds."""
    value = value.strip().replace(",", ".")
    if value.endswith("ms"):
        return float(value[:-2]) / 1000
    if value.endswith("s"):
        return float(value[:-1])
    secs = 0.0
    for part in value.split(":"):
        secs = secs * 60 + float(part)
    return secs


def _parse_vtt(text: str) -> list[Cue]:
    """WebVTT (and SRT). YouTube's automatic VTT shows two rolling lines - every cue repeats the previous
    line, plus 10 ms "transition" cues - so only lines the previous cue didn't show are kept. Cue text may
    contain lines with just a space, so cues are found by their timing lines, not by blank lines."""
    raw: list[tuple[float, float, list[str]]] = []
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for i, line in enumerate(lines):
        m = _TIMING.search(line) if "-->" in line else None
        if m:
            raw.append((_clock(m.group(1)), _clock(m.group(2)), []))
        elif raw and not (line.strip().isdigit() and i + 1 < len(lines) and "-->" in lines[i + 1]):   # SRT cue no.
            raw[-1][2].append(line)
    cues, prev = [], []
    for start, end, body in raw:
        shown = [s for s in (_line(html.unescape(re.sub(r"<[^>]*>", "", b))) for b in body) if s]
        fresh = [s for s in shown if s not in prev]
        prev = shown
        if fresh:
            cues.append(Cue(start, end, " ".join(fresh)))
    return cues


_XML_CUE = re.compile(r"<(text|p)\b([^>]*)(?<!/)>(.*?)</\1\s*>", re.S)     # not self-closing <p/>
_ATTR = re.compile(r'([\w:.-]+)\s*=\s*"([^"]*)"')


def _parse_xml(text: str) -> list[Cue]:
    """srv1 (``<text start dur>`` in seconds), srv3 (``<p t d>`` in ms) and TTML (``<p begin end>``)."""
    cues = []
    for m in _XML_CUE.finditer(text):
        a = dict(_ATTR.findall(m.group(2)))
        try:
            if "start" in a:
                start = float(a["start"])
                end = start + float(a.get("dur") or 0)
            elif "t" in a:
                start = int(a["t"]) / 1000
                end = start + int(a.get("d") or 0) / 1000
            elif "begin" in a:
                start = _clock(a["begin"])
                end = _clock(a["end"]) if a.get("end") else start
            else:
                continue                                 # a <p> without timing (e.g. an HTML error page)
        except ValueError:
            continue
        body = re.sub(r"<[^>]+>", "", re.sub(r"<br\s*/?>", " ", m.group(3)))
        body = _line(html.unescape(html.unescape(body)))  # srv1 is escaped twice ("&amp;#39;")
        if body:
            cues.append(Cue(start, max(end, start), body))
    return cues


def _tidy(cues: list[Cue]) -> list[Cue]:
    """Sorted cues without repeats or overlaps (automatic captions overlap the next line by ~2 s); tiny
    fragments ("so", "right?") joined to a neighbour."""
    ordered = sorted((Cue(c.start, max(c.end, c.start), c.text) for c in cues if c.text),
                     key=lambda c: (c.start, c.end))
    uniq: list[Cue] = []
    for c in ordered:
        if not (uniq and abs(c.start - uniq[-1].start) < 0.01 and c.text == uniq[-1].text):  # HLS segments overlap
            uniq.append(c)
    for a, b in zip(uniq, uniq[1:]):
        if a.end > b.start:
            a.end = max(a.start, b.start)
    out: list[Cue] = []
    for c in uniq:
        p = out[-1] if out else None
        if (p is not None and c.start - p.end <= MERGE_GAP_S and min(len(p.text), len(c.text)) < TINY_CUE_CHARS
                and len(p.text) + len(c.text) < MAX_CUE_CHARS):
            p.end, p.text = max(p.end, c.end), f"{p.text} {c.text}"
        else:
            out.append(c)
    for c in out:
        c.start, c.end = round(c.start, 3), round(c.end, 3)
    return out


# ------------------------------------------------------------------ many at once
Progress = Callable[[int, int, str], None]


def search_many(queries: list[str], limit: int = 8, *, workers: int = 4, cancel: Optional[threading.Event] = None,
                progress: Optional[Progress] = None) -> dict[str, list[VideoHit] | VideoError]:
    """:func:`search` for several queries in parallel: {query: hits, or the VideoError}."""
    return _run_many(queries, lambda q: search(q, limit, cancel=cancel), workers, cancel, progress, lambda q, r: q)


def inspect_many(ids: list[str], *, workers: int = 4, langs: tuple[str, ...] = ("en",),
                 cancel: Optional[threading.Event] = None,
                 progress: Optional[Progress] = None) -> dict[str, VideoInfo | VideoError]:
    """:func:`inspect` for several videos in parallel: {id: VideoInfo, or the VideoError}."""
    return _run_many(ids, lambda v: inspect(v, langs=langs, cancel=cancel), workers, cancel, progress,
                     lambda v, r: r.title if isinstance(r, VideoInfo) and r.title else v)


def _run_many(keys: list[str], fn: Callable[[str], Any], workers: int, cancel: Optional[threading.Event],
              progress: Optional[Progress], label: Callable[[str, Any], str]) -> dict[str, Any]:
    """``fn`` for every key in a thread pool; ``progress(done, total, label)`` after each. A failure is that
    key's result and never stops the others - except a bot check or rate limit: YouTube then refuses the whole
    connection, so the keys not started yet get the same error without asking it again."""
    keys = list(dict.fromkeys(keys))
    results: dict[str, Any] = {}
    if not keys:
        return results
    blocked: list[VideoError] = []

    def task(key: str) -> Any:
        _check(cancel)
        if blocked:
            raise VideoError(str(blocked[0]), blocked[0].kind)
        try:
            return fn(key)
        except VideoError as exc:
            if exc.kind in BLOCKING_KINDS:
                blocked.append(exc)
            raise

    workers = max(1, min(int(workers or 1), len(keys), 8))          # more only invites YouTube's rate limit
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="videos") as pool:
        futures = {pool.submit(task, key): key for key in keys}
        for done, fut in enumerate(as_completed(futures), 1):
            key = futures[fut]
            try:
                results[key] = fut.result()
            except VideoError as exc:
                results[key] = exc
            except Exception as exc:  # noqa: BLE001 - one failure never stops the others
                log.warning("Video task %r failed: %s", key, exc, exc_info=True)
                results[key] = VideoError(t("Something went wrong with this video."))
            if progress is not None:
                try:
                    progress(done, len(keys), label(key, results[key]))
                except Exception as exc:  # noqa: BLE001 - a broken progress callback must not lose results
                    log.debug("Progress callback failed: %s", exc)
    return {key: results[key] for key in keys}


# ------------------------------------------------------------------ what is said: text, windows, ranking
_APOSTROPHES = str.maketrans({"\u2019": "'", "\u2018": "'", "`": "'", "\u00b4": "'", "\u02bc": "'"})


def _stem(word: str) -> str:
    """Light plural folding: "theorems" -> "theorem", "probabilities" -> "probability", "classes" -> "class"."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith(("sses", "shes", "ches", "xes", "zes")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def _tokens(text: str) -> list[str]:
    """Words for matching: case-, accent- and punctuation-insensitive, possessives and plurals folded
    ("Bayes'", "Bayes's" and "bayes" are one word)."""
    folded = unicodedata.normalize("NFKD", (text or "").translate(_APOSTROPHES).casefold())
    folded = re.sub(r"'s\b", "", "".join(ch for ch in folded if not unicodedata.combining(ch))).replace("'", "")
    return [_stem(w) for w in re.findall(r"[^\W_]+", folded)]


@dc.dataclass
class _Term:
    phrase: str                            # normalised words joined by spaces
    weight: float                          # phrases are more specific than single words
    rx: re.Pattern[str]


def _terms(terms: list[str]) -> list[_Term]:
    out: list[_Term] = []
    for term in terms or []:
        words = _tokens(str(term))
        phrase = " ".join(words)
        if len(phrase) >= 2 and all(x.phrase != phrase for x in out):
            out.append(_Term(phrase, 1.0 + 0.5 * (len(words) - 1), re.compile(rf"(?<!\S){re.escape(phrase)}(?!\S)")))
    return out


def caption_text(info: VideoInfo, start: Optional[float] = None, end: Optional[float] = None) -> str:
    """The captions said between ``start`` and ``end`` (seconds; None: from the beginning / to the end)."""
    lo = -math.inf if start is None else start
    hi = math.inf if end is None else end
    return " ".join(c.text for c in info.cues if c.start < hi and (c.end > lo or c.start >= lo))


def stamped_part(info: VideoInfo, start: Optional[float] = None, end: Optional[float] = None, *,
                  every_s: float = 30, limit: int = WINDOW_TEXT_CHARS) -> dict[str, Any]:
    """Like :func:`caption_text`, with the time as "[m:ss]" before the first cue and then about every ``every_s``
    seconds, so a reader (or a model quoting it) can tell where something was said: {"start", "end", "text"}.
    At most ``limit`` characters - whole cues; ``end`` is when the last one included ends."""
    lo = -math.inf if start is None else start
    hi = math.inf if end is None else end
    out: list[str] = []
    size, last, first_s, end_s = 0, -math.inf, None, None
    for c in info.cues:
        if not (c.start < hi and (c.end > lo or c.start >= lo)):
            continue
        piece = (f"[{fmt_ts(c.start)}] " if c.start - last >= every_s else "") + c.text
        if out and size + len(piece) + 1 > limit:
            break
        if c.start - last >= every_s:
            last = c.start
        out.append(piece)
        size += len(piece) + 1
        first_s = c.start if first_s is None else first_s
        end_s = c.end
    text = " ".join(out)
    return {"start": round(first_s if first_s is not None else (start or 0.0), 2),
            "end": round(end_s if end_s is not None else (end or 0.0), 2), "text": _trim(text, limit)}


def stamped_text(info: VideoInfo, start: Optional[float] = None, end: Optional[float] = None, *,
                 every_s: float = 30, limit: int = WINDOW_TEXT_CHARS) -> str:
    """The text of :func:`stamped_part`."""
    return stamped_part(info, start, end, every_s=every_s, limit=limit)["text"]


def _trim(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text.rfind(" ", 0, limit)
    return text[:cut if cut > limit * 0.8 else limit].rstrip() + "…"


def windows(info: VideoInfo, terms: list[str], *, window_s: float = 180, top: int = 3) -> list[dict[str, Any]]:
    """The parts of a video that talk most about ``terms``, best first: [{"start", "end", "score", "text"}].

    A window starts at a cue that mentions a term (or at a chapter whose title does) and lasts ``window_s``.
    Its score is the weighted number of mentions (a phrase counts more than a word), +1 for every further
    term it covers and +2 for each term in the title of a chapter it overlaps. Overlapping windows that score
    at least half as well as the best of them merge into one part (at most 2 x ``window_s``). ``text`` is that
    part's captions (~2500 characters at most). [] without captions or mentions."""
    tl, cues = _terms(terms), info.cues
    if not tl or not cues or top <= 0:
        return []
    window_s = max(10.0, float(window_s))
    parts = [" ".join(_tokens(c.text)) for c in cues]
    offsets, pos = [], 0
    for p in parts:
        offsets.append(pos)
        pos += len(p) + 1
    text = " ".join(parts)                     # matched as a whole: a phrase may run across two cues
    counts = [[0] * len(tl) for _ in cues]
    for j, term in enumerate(tl):
        for m in term.rx.finditer(text):
            counts[bisect.bisect_right(offsets, m.start()) - 1][j] += 1
    starts = [c.start for c in cues]
    titled = [(ch, {j for j, term in enumerate(tl) if term.rx.search(" ".join(_tokens(ch.title)))})
              for ch in info.chapters]
    anchors = {i for i, row in enumerate(counts) if any(row)}
    anchors |= {bisect.bisect_left(starts, ch.start) for ch, found in titled if found} - {len(cues)}
    candidates = []
    for a in sorted(anchors):
        ws = cues[a].start
        b = max(a + 1, bisect.bisect_left(starts, ws + window_s))      # cues a..b-1 start in the window
        we = max(c.end for c in cues[a:b])
        found = [sum(row[j] for row in counts[a:b]) for j in range(len(tl))]
        score = sum(term.weight * n for term, n in zip(tl, found)) + COVERAGE_BONUS * max(0, sum(1 for n in found if n) - 1)
        in_titles = set().union(*[f for ch, f in titled if ch.start < we and ch.end > ws])
        score += CHAPTER_BONUS * sum(tl[j].weight for j in in_titles)
        if score > 0:
            candidates.append((ws, we, score))
    regions: list[list[float]] = []                                    # [start, end, score], never overlapping
    for ws, we, score in sorted(candidates, key=lambda w: (-w[2], w[0])):
        hit = next((r for r in regions if ws < r[1] and r[0] < we), None)
        if hit is None:
            regions.append([ws, we, score])
        elif score >= MERGE_FRACTION * hit[2]:
            lo, hi = min(hit[0], ws), max(hit[1], we)
            if hi - lo <= MAX_REGION_WINDOWS * window_s and not any(
                    r is not hit and lo < r[1] and r[0] < hi for r in regions):
                hit[0], hit[1] = lo, hi
    regions.sort(key=lambda r: (-r[2], r[0]))
    return [{"start": round(s, 2), "end": round(e, 2), "score": round(sc, 2),
             "text": _trim(caption_text(info, s, e), WINDOW_TEXT_CHARS)} for s, e, sc in regions[:top]]


def _channel_key(name: str) -> str:
    return re.sub(r"[\W_]+", "", (name or "").casefold())


_EDU_KEYS = tuple(_channel_key(c) for c in EDU_CHANNELS)


def _length_fit(minutes: float, lo: float, hi: float) -> float:
    """+1 inside the preferred length, fading to 0 towards 2 and 90 minutes; -2 shorter or longer than those."""
    if minutes <= 0:
        return 0.0                                                    # unknown
    if lo <= minutes <= hi:
        return 1.0
    if minutes < 2 or minutes > 90:
        return -2.0
    if minutes < lo:
        return (minutes - 2) / max(lo - 2, 1e-9)
    return max(0.0, 1 - (minutes - hi) / max(90 - hi, 1e-9))


def rank(hits: list[VideoHit], terms: list[str], *, prefer_minutes: tuple[float, float] = (3, 25)) -> list[VideoHit]:
    """Search hits best first by a quick heuristic, before any captions are read: the terms in the title
    (strong; a phrase's words in another order count half) and the description (weak), a length within
    ``prefer_minutes`` (< 2 and > 90 minutes are penalised), views (log scale) and a small boost for the
    :data:`EDU_CHANNELS`. Ties keep their order."""
    tl = _terms(terms)
    lo, hi = sorted(float(m) for m in prefer_minutes)

    def score(h: VideoHit) -> float:
        title, desc = " ".join(_tokens(h.title)), " ".join(_tokens(h.description))
        words = set(title.split())
        s = 0.0
        for term in tl:
            if term.rx.search(title):
                s += TITLE_WEIGHT * term.weight
            elif " " in term.phrase:
                part = term.phrase.split()
                s += 0.5 * TITLE_WEIGHT * term.weight * sum(w in words for w in part) / len(part)
            if term.rx.search(desc):
                s += DESCRIPTION_WEIGHT * term.weight
        s += _length_fit(h.duration_s / 60, lo, hi)
        s += VIEWS_WEIGHT * math.log10(1 + h.view_count) if h.view_count and h.view_count > 0 else 0.0
        key = _channel_key(h.channel)
        return s + (EDU_BOOST if key and key.startswith(_EDU_KEYS) else 0.0)

    return sorted(hits, key=lambda h: -score(h))                     # sorted() is stable: ties keep their order


# ------------------------------------------------------------------ cache
def _cache_dir() -> Path:
    return app_dir() / "cache" / "videos"


def _info_path(vid: str) -> Path:
    return _cache_dir() / f"{vid}.json"


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _fresh(path: Path, ttl_s: float) -> bool:
    try:
        return time.time() - path.stat().st_mtime <= ttl_s
    except OSError:
        return False


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(path: Path, data: Any) -> None:
    """Atomic (a temporary file, then a rename): a reader never sees half a file. Caching is best effort."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}-{threading.get_ident()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(json.dumps(data, ensure_ascii=False).encode("utf-8"))
        os.replace(tmp, path)
    except OSError as exc:
        log.debug("Couldn't cache %s: %s", path.name, exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _cached_info(vid: str, langs: tuple[str, ...]) -> Optional[VideoInfo]:
    """The cached inspection, if fresh and resolved for these languages - or its captions are already in the
    most wanted one (nothing better to find)."""
    path = _info_path(vid)
    data = _read_json(path)
    if not isinstance(data, dict) or not _fresh(path, INFO_TTL_S if data.get("cues") else NO_CAPTIONS_TTL_S):
        return None
    try:
        info = VideoInfo.from_json(data)
    except (TypeError, ValueError, KeyError):
        return None
    if list(data.get("langs") or []) != list(langs) and (not info.cues or _base(info.caption_lang) != _base(langs[0])):
        return None
    return info
