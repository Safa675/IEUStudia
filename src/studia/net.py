"""HTTP helpers: a tuned requests session, HTML form parsing and cookie (de)serialisation."""

from __future__ import annotations

import dataclasses as dc
import html
import logging
import time
from html.parser import HTMLParser
from typing import Any, Optional
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("studia")
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36")
DEFAULT_TIMEOUT = (15, 45)


class NetworkError(requests.ConnectionError):
    """A connection problem, with a message that can be shown to the user."""


class _TimeoutSession(requests.Session):
    def request(self, method, url, **kwargs):  # type: ignore[override]
        kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
        try:
            return super().request(method, url, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as exc:
            host = urlparse(url).hostname or url
            raise NetworkError(f"Can't reach {host}. Check your internet connection.") from exc


def make_session() -> requests.Session:
    s = _TimeoutSession()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9,tr;q=0.8",
    })
    retry = Retry(total=3, connect=3, read=2, backoff_factor=0.6, status_forcelist=[502, 503, 504],
                  allowed_methods=frozenset({"GET", "HEAD"}), raise_on_status=False)
    adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=16)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


@dc.dataclass
class Form:
    action: str
    method: str = "get"
    id: str = ""
    fields: dict[str, str] = dc.field(default_factory=dict)


class _Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[Form] = []
        self.iframes: list[str] = []
        self._form: Optional[Form] = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        if tag == "form":
            self._form = Form(action=a.get("action", ""), method=(a.get("method") or "get").lower(), id=a.get("id", ""))
            self.forms.append(self._form)
        elif tag == "input" and self._form is not None and a.get("name"):
            if a.get("type", "").lower() in ("checkbox", "radio") and "checked" not in a:
                return
            self._form.fields[a["name"]] = a.get("value", "")
        elif tag == "iframe" and a.get("src"):
            self.iframes.append(a["src"])

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._form = None


def parse_forms(page: str) -> list[Form]:
    p = _Parser()
    p.feed(page or "")
    return p.forms


def parse_iframes(page: str) -> list[str]:
    p = _Parser()
    p.feed(page or "")
    return [html.unescape(src) for src in p.iframes]


def export_cookies(session: requests.Session) -> list[dict[str, Any]]:
    out = []
    for c in session.cookies:
        out.append({"name": c.name, "value": c.value, "domain": c.domain, "path": c.path,
                    "expires": c.expires, "secure": bool(c.secure)})
    return out


def import_cookies(session: requests.Session, cookies: list[dict[str, Any]]) -> None:
    now = time.time()
    for c in cookies:
        exp = c.get("expires")
        if exp and exp < now:
            continue
        session.cookies.set(c["name"], c.get("value", ""), domain=c.get("domain", ""), path=c.get("path", "/"),
                            secure=bool(c.get("secure")), expires=exp)


def clone_session(session: requests.Session) -> requests.Session:
    """A new session (for another thread) carrying the same cookies and headers."""
    s = make_session()
    s.headers.update(session.headers)
    import_cookies(s, export_cookies(session))
    return s


def merge_cookies(into: requests.Session, src: requests.Session) -> None:
    import_cookies(into, export_cookies(src))
