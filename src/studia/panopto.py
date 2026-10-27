"""Panopto (panopto.ieu.edu.tr) client: session lists, delivery details and media download."""

from __future__ import annotations

import dataclasses as dc
import datetime as dt
import logging
import re
import threading
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urljoin

import requests

from . import hls, httpdl
from .cancel import check
from .domain import Recording
from .i18n import t
from .sections import parse_section_tag

log = logging.getLogger("studia")
BASE = "https://panopto.ieu.edu.tr"
_EPOCH = dt.datetime(1970, 1, 1)


class NeedPanoptoAuth(Exception):
    """The Panopto session is missing/expired (fix: run the LTI launch again)."""


class NotReady(Exception):
    """Panopto is still processing this recording."""


@dc.dataclass
class MediaSource:
    kind: str                 # "audio_podcast" | "video_podcast" | "hls"
    url: str
    est_bytes: Optional[int] = None


def parse_dotnet_date(value: str) -> Optional[dt.datetime]:
    """``"/Date(1792494951083)/"`` → naive local wall-clock time.

    Panopto's session list encodes the *local* (Istanbul) wall-clock time as if it were UTC,
    so the epoch offset is applied without any timezone conversion.
    """
    m = re.search(r"/Date\((-?\d+)", value or "")
    return _EPOCH + dt.timedelta(milliseconds=int(m.group(1))) if m else None


def _kind(name: str, folder_path: str) -> str:
    return "lab" if re.search(r"\blab", f"{folder_path} {name}", re.I) else "lecture"


def parse_sessions(d: dict[str, Any], course_id: str, folder_path: str) -> list[Recording]:
    out = []
    for row in d.get("Results") or []:
        did = (row.get("DeliveryID") or "").lower()
        start = parse_dotnet_date(row.get("StartTime") or "")
        if not did or start is None:
            continue
        name = (row.get("SessionName") or "").strip() or "Untitled recording"
        out.append(Recording(
            delivery_id=did, course_id=str(course_id), folder_id=(row.get("FolderID") or "").lower(),
            folder_path=folder_path, name=name, start=start.replace(microsecond=0),
            duration_s=float(row.get("Duration") or 0.0),
            viewer_url=row.get("ViewerUrl") or f"{BASE}/Panopto/Pages/Viewer.aspx?id={did}",
            session_id=(row.get("SessionID") or "").lower(), kind=_kind(name, folder_path),
            section_tag=parse_section_tag(name),
        ))
    return out


def _query(folder_id: str, page: int) -> dict[str, Any]:
    return {"queryParameters": {
        "query": None, "sortColumn": 1, "sortAscending": False, "maxResults": 250, "page": page,
        "startDate": None, "endDate": None, "folderID": folder_id, "bookmarked": False, "getFolderData": True,
        "isSharedWithMe": False, "isSubscriptionsPage": False, "includeArchived": True,
        "includeArchivedStateCount": False, "sessionListOnlyArchived": False, "includePlaylists": True}}


class Panopto:
    BASE = BASE

    def __init__(self, session: requests.Session) -> None:
        self.s = session

    def _headers(self) -> dict[str, str]:
        token = self.s.cookies.get("csrfToken", domain="panopto.ieu.edu.tr") or ""
        return {"x-csrf-token": token, "x-requested-with": "XMLHttpRequest",
                "referer": f"{BASE}/Panopto/Pages/Sessions/List.aspx"}

    def is_authenticated(self) -> bool:
        try:
            r = self.s.get(f"{BASE}/Panopto/Api/v1/auth/legacyLogin", allow_redirects=False,
                           headers={"x-requested-with": "XMLHttpRequest"})
        except requests.RequestException:
            raise
        return r.status_code == 200

    # ------------------------------------------------------------------ listing
    def list_recordings(self, course_id: str, folder_id: str) -> list[Recording]:
        recs: list[Recording] = []
        todo = [(folder_id, "")]
        seen: set[str] = set()
        while todo:
            fid, path = todo.pop(0)
            if fid in seen:
                continue
            seen.add(fid)
            page = 0
            while True:
                r = self.s.post(f"{BASE}/Panopto/Services/Data.svc/GetSessions", json=_query(fid, page),
                                headers=self._headers())
                r.raise_for_status()
                d = (r.json() or {}).get("d") or {}
                batch = parse_sessions(d, course_id, path)
                recs.extend(batch)
                if page == 0:
                    for sub in d.get("Subfolders") or []:
                        if sub.get("ID"):
                            todo.append((sub["ID"].lower(), f"{path}{sub.get('Name', '').strip()}/".lstrip("/")))
                total = int(d.get("TotalNumber") or 0)
                page += 1
                if not batch or page * 250 >= total or page > 20:
                    break
        for r_ in recs:
            r_.folder_path = r_.folder_path.rstrip("/")
        return recs

    # ------------------------------------------------------------------ details
    def delivery_info(self, delivery_id: str) -> dict[str, Any]:
        r = self.s.post(f"{BASE}/Panopto/Pages/Viewer/DeliveryInfo.aspx", data={
            "deliveryId": delivery_id, "isLiveNotes": "false", "refreshAuthCookie": "true",
            "isActiveBroadcast": "false", "isEditing": "false", "isKollectiveAgentInstalled": "false",
            "isEmbed": "false", "responseType": "json"}, headers=self._headers())
        r.raise_for_status()
        data = r.json()
        if "Delivery" not in data:
            if data.get("ErrorCode") == 2 or data.get("LoginRedirect"):
                raise NeedPanoptoAuth(t("Panopto session expired."))
            raise RuntimeError(re.sub(r"<[^>]+>", "", data.get("ErrorMessage") or t("Panopto refused the request.")))
        return data

    def media_sources(self, delivery_id: str, info: Optional[dict[str, Any]] = None) -> list[MediaSource]:
        """Best-first list of sources that contain the lecture audio."""
        info = info or self.delivery_info(delivery_id)
        d = info["Delivery"]
        out: list[MediaSource] = []
        if d.get("IsAudioPodcastEncodeComplete"):
            out.append(MediaSource("audio_podcast",
                                   f"{BASE}/Panopto/Podcast/Stream/{delivery_id}.mp4?mediaTargetType=audioPodcast"))
        for ps in d.get("PodcastStreams") or []:
            if ps.get("StreamUrl"):
                out.append(MediaSource("video_podcast", ps["StreamUrl"]))
        for st in d.get("Streams") or []:
            if st.get("StreamUrl") and (st.get("Tag") or "").upper() != "SCREEN" and ".m3u8" in st["StreamUrl"]:
                out.append(MediaSource("hls", st["StreamUrl"]))
        if not out:
            raise NotReady(t("Panopto is still processing this recording. Try again later."))
        return out

    def screen_source(self, info: dict[str, Any]) -> Optional[tuple[MediaSource, float]]:
        """The screen/slides video (HLS) and its start offset in seconds, if the recording has one."""
        for st in info["Delivery"].get("Streams") or []:
            if (st.get("Tag") or "").upper() == "SCREEN" and st.get("StreamUrl"):
                return MediaSource("hls", st["StreamUrl"]), float(st.get("RelativeStart") or 0.0)
        return None

    # ------------------------------------------------------------------ download
    def download(self, src: MediaSource, dest: Path, progress: Callable[[int, int], None],
                 cancel: Optional[threading.Event] = None, prefer: str = "audio") -> Path:
        """Download a source to ``dest`` (resumable). ``prefer`` picks the HLS variant: "audio" = smallest
        variant that has an audio codec, "video" = highest resolution."""
        if src.kind != "hls":
            return httpdl.download(src.url, dest, session=self.s, progress=progress, cancel=cancel, parts=6)
        return self._download_hls(src.url, dest, progress, cancel, prefer)

    def _download_hls(self, master_url: str, dest: Path, progress: Callable[[int, int], None],
                      cancel: Optional[threading.Event], prefer: str) -> Path:
        r = hls.media_playlist(self.s, master_url, prefer)
        playlist, base = r.text, r.url
        uris = [urljoin(base, ln.strip()) for ln in playlist.splitlines() if ln.strip() and not ln.startswith("#")]
        map_m = re.search(r'#EXT-X-MAP:URI="([^"]+)"', playlist)
        files = list(dict.fromkeys(([urljoin(base, map_m.group(1))] if map_m else []) + uris))
        if len(files) == 1:  # single fragmented MP4 addressed with byte ranges: just fetch the file
            return httpdl.download(files[0], dest, session=self.s, progress=progress, cancel=cancel, parts=6)
        tmp = dest.with_name(dest.name + ".part")
        with open(tmp, "wb") as out:
            for i, u in enumerate(files):
                check(cancel)
                for attempt in range(4):
                    try:
                        seg = self.s.get(u)
                        seg.raise_for_status()
                        out.write(seg.content)
                        break
                    except requests.RequestException:
                        if attempt == 3:
                            raise
                progress(i + 1, len(files))
        tmp.replace(dest)
        return dest
