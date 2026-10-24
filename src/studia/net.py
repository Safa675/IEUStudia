"""HTTP helpers: a tuned requests session, HTML form parsing and cookie (de)serialisation."""

from __future__ import annotations

import dataclasses as dc
import errno
import html
import logging
import os
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


def use_windows_certificates() -> None:
    """HTTPS trusts the certificates Windows trusts, as the browser does. An antivirus that scans HTTPS, or a company
    proxy, adds its own to Windows; Python's built-in list doesn't have them, so every connection would fail."""
    try:
        import truststore

        truststore.inject_into_ssl()
    except Exception:  # noqa: BLE001 - the built-in list still works everywhere else
        log.debug("Windows certificates not used", exc_info=True)


def _host(exc: BaseException, url: str = "") -> str:
    req = getattr(exc, "request", None)
    return urlparse(url or getattr(req, "url", "") or "").hostname or "the server"


def plain_error(exc: BaseException, url: str = "") -> str:
    """What went wrong, in words for anyone: the connection, a full disk, memory, a file Windows would not let us
    write. Other errors keep their own message (most are already written for people)."""
    if isinstance(exc, NetworkError):
        return str(exc)
    if isinstance(exc, requests.exceptions.SSLError):          # before ConnectionError: it is one
        return (f"A secure connection to {_host(exc, url)} was blocked. Antivirus web or HTTPS scanning, or a Wi-Fi "
                "sign-in page, can do this: open any website in your browser, then try again.")
    if isinstance(exc, requests.Timeout):
        return f"{_host(exc, url)} took too long to answer. Check your internet connection and try again."
    if isinstance(exc, requests.ConnectionError):
        return (f"Can't reach {_host(exc, url)}. Check your internet connection (on campus Wi-Fi, make sure you are "
                "signed in to it), then try again.")
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return f"{_host(exc, url)} answered with an error ({exc.response.status_code}). Try again in a while."
    if isinstance(exc, MemoryError):
        return "Not enough memory. Close other programs and try again."
    if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
        return "Your disk is full. Free up some space, then try again."
    if isinstance(exc, PermissionError):
        name = os.path.basename(str(exc.filename or "")) or "a file"
        return (f"Windows did not let IEUStudia write {name}: it may be open in another program (or syncing), or "
                "your antivirus or Windows' ransomware protection blocked it. You can also choose another output folder "
                "in Settings → Files.")
    return str(exc) or exc.__class__.__name__


class _TimeoutSession(requests.Session):
    def request(self, method, url, **kwargs):  # type: ignore[override]
        kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
        try:
            return super().request(method, url, **kwargs)
        except requests.exceptions.SSLError as exc:
            raise NetworkError(plain_error(exc, url)) from exc
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
