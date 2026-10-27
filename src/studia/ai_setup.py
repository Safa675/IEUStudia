"""Connecting ChatGPT or Claude, step by step: what is missing on this PC, installing it (without a console window),
the sign-in, the plan and a short test message.

ChatGPT is reached with "Continue with ChatGPT" (chatgpt.py), which needs nothing installed, or, as its own choice
for people who already use it, through OpenAI's Codex CLI, which this module can install: OpenAI's npm package for
Windows (@openai/codex, the win32 build), fetched from the npm registry and checked against its published SHA-512 -
the same files npm would install, so Node.js is not needed. Claude is reached only through Anthropic's own Claude
Code (not the Claude desktop app), installed with Anthropic's installer script (https://claude.ai/install.ps1, into
~\\.local\\bin); signing in happens in Claude Code's own flow (:func:`studia.ai.start_login`), as for the Codex CLI.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import tarfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .i18n import num, t

log = logging.getLogger("studia")

Emit = Callable[[dict[str, Any]], None]
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
NPM_REGISTRY = "https://registry.npmjs.org"
CLAUDE_INSTALLER = "https://claude.ai/install.ps1"
# The plans that include the tool; anything else is said plainly (Claude Free has no Claude Code).
CLAUDE_PLANS = {"pro": "Claude Pro", "max": "Claude Max", "team": "Claude Team", "enterprise": "Claude Enterprise"}
CODEX_PLANS = {"plus": "ChatGPT Plus", "pro": "ChatGPT Pro", "team": "ChatGPT Business", "business": "ChatGPT Business",
               "edu": "ChatGPT Edu", "enterprise": "ChatGPT Enterprise"}
TEST_SYSTEM = "This is a connection test. Reply with the single word OK and nothing else."

_lock = threading.Lock()
_jobs: dict[str, dict[str, Any]] = {}       # "claude" | "codex" -> {"state", "line", "error", "started"}
_tests: dict[str, dict[str, Any]] = {}      # "chatgpt" | "claude" | "codex" -> the last test's result


# ----------------------------------------------------------------------------- where things are
def tools_dir() -> Path:
    from .paths import app_dir

    return app_dir() / "tools"


def codex_home() -> Path:
    """Where IEUStudia keeps the Codex CLI it installs: ``<app data>\\tools\\codex`` (bin\\codex.exe inside)."""
    return tools_dir() / "codex"


def codex_exe() -> str:
    exe = codex_home() / "bin" / "codex.exe"
    return str(exe) if exe.is_file() else ""


def git_bash() -> str:
    """Git for Windows' bash.exe if it is installed; "" if not. Claude Code prefers it for shell commands and uses
    PowerShell without it - and IEUStudia runs Claude Code without any tools, so it is never required."""
    env = os.environ.get("CLAUDE_CODE_GIT_BASH_PATH", "")
    if env and os.path.isfile(env):
        return env
    roots = [os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramW6432", ""),
             os.environ.get("ProgramFiles(x86)", ""), os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")]
    for root in roots:
        cand = os.path.join(root, "Git", "bin", "bash.exe") if root else ""
        if cand and os.path.isfile(cand):
            return cand
    git = shutil.which("git.exe") or shutil.which("git")
    if git:                                        # ...\Git\cmd\git.exe -> ...\Git\bin\bash.exe
        cand = Path(git).resolve().parent.parent / "bin" / "bash.exe"
        if cand.is_file():
            return str(cand)
    return ""


def claude_env() -> dict[str, str]:
    """The environment for running Claude Code: tells it where Git's Bash is when Git was installed after IEUStudia
    started (so the PATH this process got does not have it yet). Optional: Claude Code works without Git."""
    env = dict(os.environ)
    bash = git_bash()
    if bash and not env.get("CLAUDE_CODE_GIT_BASH_PATH"):
        env["CLAUDE_CODE_GIT_BASH_PATH"] = bash
    return env


# ----------------------------------------------------------------------------- jobs (installs) and their progress
def job(which: str) -> dict[str, Any]:
    with _lock:
        return dict(_jobs.get(which) or {})


def last_test(which: str) -> dict[str, Any]:
    with _lock:
        return dict(_tests.get(which) or {})


def _update(which: str, emit: Emit, **change: Any) -> None:
    with _lock:
        j = _jobs.setdefault(which, {})
        j.update(change)
        evt = {"type": "ai_setup", "which": which, **j}
    emit(evt)


_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")


def _clean(line: str) -> str:
    return " ".join(_ANSI.sub("", line).replace("\r", " ").split())


def _run_logged(cmd: list[str], which: str, emit: Emit, timeout_s: float = 900) -> list[str]:
    """Run an installer hidden, passing each output line on as progress; its last lines if it fails."""
    lines: list[str] = []
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            creationflags=_NO_WINDOW)
    assert proc.stdout
    started, last_emit = time.monotonic(), 0.0

    def killer() -> None:
        while proc.poll() is None:
            if time.monotonic() - started > timeout_s:
                proc.kill()
                return
            time.sleep(1)

    threading.Thread(target=killer, daemon=True).start()
    for raw in proc.stdout:
        line = _clean(raw.decode("utf-8", "replace"))
        if not line:
            continue
        lines.append(line)
        if time.monotonic() - last_emit > 0.3:
            last_emit = time.monotonic()
            _update(which, emit, line=line[:160])
    code = proc.wait()
    if code != 0:
        tail = " ".join(lines[-3:]) or f"exit code {code}"
        raise RuntimeError(tail[:400])
    return lines


def start_install(which: str, emit: Emit) -> dict[str, Any]:
    """Install "claude" (Claude Code) or "codex" (the Codex CLI) in the background. Progress and the outcome go to
    ``emit`` as {"type": "ai_setup", "which", "state", "line", "error"} events."""
    if which not in ("claude", "codex"):
        raise ValueError(which)
    with _lock:
        if (_jobs.get(which) or {}).get("state") == "running":
            return dict(_jobs[which])
        _jobs[which] = {"state": "running", "line": t("Starting…"), "error": "", "started": time.time()}

    def run() -> None:
        from . import ai

        try:
            if which == "claude":
                _run_logged(_claude_install_command(), which, emit)
            else:
                _install_codex(emit)
            ai.claude_cli_info.cache_clear()
            ai.codex_cli_info.cache_clear()
            if not _installed(which):
                raise RuntimeError(t("The installer finished, but the program was not found afterwards."))
            _update(which, emit, state="done", line=t("Installed"), error="")
        except Exception as exc:  # noqa: BLE001 - shown in the set-up
            log.warning("Installing %s failed: %s", which, exc)
            _update(which, emit, state="error", error=_install_error(which, str(exc)))

    threading.Thread(target=run, name=f"install-{which}", daemon=True).start()
    return job(which)


def _installed(which: str) -> bool:
    from . import ai

    if which == "claude":
        return ai.claude_cli_info()["available"]
    return ai.codex_cli_info()["available"]


def _install_error(which: str, message: str) -> str:
    claude = {"claude": True, "codex": False}[which]
    low = message.lower()
    if any(k in low for k in ("could not resolve", "name or service", "getaddrinfo", "unable to connect", "timed out",
                              "no such host", "connection")):
        return (t("Could not download Claude Code: check your internet connection and try again.") if claude else
                t("Could not download the Codex CLI: check your internet connection and try again."))
    return (t("Installing Claude Code did not work: {error}", error=message) if claude else
            t("Installing the Codex CLI did not work: {error}", error=message))[:500]


def _claude_install_command() -> list[str]:
    return ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
            "[Console]::OutputEncoding = [Text.Encoding]::UTF8; $ProgressPreference = 'SilentlyContinue'; "
            f"Write-Output 'Downloading Claude Code from claude.ai...'; irm {CLAUDE_INSTALLER} | iex"]


# ----------------------------------------------------------------------------- the Codex CLI from the npm registry
def _session() -> Any:
    import requests

    s = requests.Session()
    s.headers["User-Agent"] = "IEUStudia (+https://github.com/mirzaisi/IEUStudia)"
    return s


def codex_package(session: Any = None) -> dict[str, Any]:
    """The latest Windows build of @openai/codex on the npm registry: {"version", "tarball", "integrity", "target"}."""
    session = session or _session()
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
    r = session.get(f"{NPM_REGISTRY}/@openai/codex/latest", timeout=30)
    r.raise_for_status()
    version = r.json()["version"]
    r = session.get(f"{NPM_REGISTRY}/@openai/codex/{version}-win32-{arch}", timeout=30)
    r.raise_for_status()
    dist = r.json()["dist"]
    return {"version": version, "tarball": dist["tarball"], "integrity": dist.get("integrity", ""),
            "shasum": dist.get("shasum", ""),
            "target": "aarch64-pc-windows-msvc" if arch == "arm64" else "x86_64-pc-windows-msvc"}


def _check_integrity(data: bytes, integrity: str, shasum: str) -> None:
    """npm's "sha512-<base64>" (or the older sha1 "shasum") must match, or the download is not used."""
    for item in (integrity or "").split():
        algo, _, digest = item.partition("-")
        if algo in ("sha512", "sha384", "sha256"):
            if base64.b64encode(hashlib.new(algo, data).digest()).decode() != digest:
                raise RuntimeError(t("The download was damaged (checksum mismatch). Try again."))
            return
    if shasum and hashlib.sha1(data).hexdigest() != shasum:  # noqa: S324 - npm's published checksum
        raise RuntimeError(t("The download was damaged (checksum mismatch). Try again."))
    if not integrity and not shasum:
        raise RuntimeError(t("The npm registry did not publish a checksum for this package."))


def unpack_codex(data: bytes, target: str, dest: Path) -> None:
    """The package's ``vendor/<target>/`` folder (codex.exe and its helpers) into ``dest``, replacing it."""
    prefix = f"package/vendor/{target}/"
    tmp = dest.with_name(dest.name + ".new")
    shutil.rmtree(tmp, ignore_errors=True)
    found = False
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for m in tar.getmembers():
            if not m.isfile() or not m.name.startswith(prefix):
                continue
            rel = Path(m.name[len(prefix):])
            if rel.is_absolute() or ".." in rel.parts:
                continue
            out = tmp / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(m)
            if src is None:
                continue
            with src, open(out, "wb") as f:
                shutil.copyfileobj(src, f)
            found = found or rel.as_posix() == "bin/codex.exe"
    if not found:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError(t("The package has no codex.exe for this PC."))
    old = dest.with_name(dest.name + ".old")
    shutil.rmtree(old, ignore_errors=True)
    if dest.exists():
        dest.rename(old)
    tmp.rename(dest)
    shutil.rmtree(old, ignore_errors=True)


def _install_codex(emit: Emit) -> None:
    session = _session()
    _update("codex", emit, line=t("Looking up the latest Codex CLI on npm…"))
    pkg = codex_package(session)
    buf = io.BytesIO()
    with session.get(pkg["tarball"], stream=True, timeout=60) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or 0)
        last = 0.0
        for chunk in r.iter_content(1 << 16):
            buf.write(chunk)
            if time.monotonic() - last > 0.3:
                last = time.monotonic()
                got = num(buf.tell() / 1048576)
                _update("codex", emit, line=t("Downloading Codex {version}… {got} of {total} MB", version=pkg["version"],
                                              got=got, total=num(total / 1048576)) if total else
                        t("Downloading Codex {version}… {got} MB", version=pkg["version"], got=got))
    data = buf.getvalue()
    _check_integrity(data, pkg["integrity"], pkg["shasum"])
    _update("codex", emit, line=t("Unpacking…"))
    home = codex_home()
    home.parent.mkdir(parents=True, exist_ok=True)
    unpack_codex(data, pkg["target"], home)
    (home / ".installed").write_text(json.dumps({"version": pkg["version"], "from": pkg["tarball"]}), encoding="utf-8")


# ----------------------------------------------------------------------------- plan and test
def plan_status(which: str, plan: str, logged_in: bool) -> dict[str, Any]:
    """Whether the signed-in account's plan includes the tool: {"ok": True/False/None, "label", "note"}."""
    p = (plan or "").lower()
    if not logged_in:
        return {"ok": None, "label": "", "note": ""}
    if which == "claude":
        if p in CLAUDE_PLANS:
            return {"ok": True, "label": CLAUDE_PLANS[p], "note": ""}
        if p == "api":
            return {"ok": True, "label": "Anthropic Console (API)", "note": t("Signed in with an API account: every "
                    "request is billed per use, not covered by a Pro or Max subscription.")}
        if p == "free":
            return {"ok": False, "label": "Claude Free", "note": t("Claude's free plan does not include Claude Code. "
                    "You need Claude Pro or Max.")}
        return {"ok": None, "label": "", "note": t("Could not tell your plan; the test message below will show whether "
                "it works.")}
    if p in CODEX_PLANS:
        return {"ok": True, "label": CODEX_PLANS[p], "note": ""}
    if p == "api":
        return {"ok": True, "label": t("OpenAI API key"), "note": t("Signed in with an API key: every request is billed "
                "per use, not covered by a ChatGPT subscription.")}
    if p in ("free", "go"):              # OpenAI includes Codex in Free and Go too, with smaller limits
        return {"ok": None, "label": f"ChatGPT {p.capitalize()}", "note": t("Codex comes with ChatGPT Free and Go too, "
                "with smaller limits; the test message below shows whether it works.")}
    return {"ok": None, "label": "ChatGPT" if p == "chatgpt" else "", "note": t("Codex does not show your plan; the "
            "test message below shows whether it works.")}


def test(which: str) -> dict[str, Any]:
    """Send a tiny test message through the chosen tool (a few tokens of your plan): {"ok", "seconds", "error",
    "code"}. ``code`` is "plan_not_eligible" when OpenAI doesn't let apps use the ChatGPT plan."""
    from . import ai
    from .chatgpt import NotEligible

    started = time.monotonic()
    content = [{"type": "text", "text": "Reply with OK."}]
    try:
        if which == "claude":
            res = ai._claude_one_shot(TEST_SYSTEM, content, model="haiku", effort="low", timeout_s=180, think=False)
        elif which in ("chatgpt", "codex"):
            ask = ai._chatgpt_one_shot if which == "chatgpt" else ai._codex_one_shot
            res = ask(TEST_SYSTEM, content, "haiku", "low", None, None, 180)
        else:
            raise ValueError(which)
        ok = bool((res.get("text") or "").strip())
        out = {"ok": ok, "error": "" if ok else t("No answer came back."), "code": ""}
    except Exception as exc:  # noqa: BLE001 - shown in the set-up
        msg = str(exc) or exc.__class__.__name__
        out = {"ok": False, "error": (ai.friendly_error(msg) if which == "claude" else msg)[:400],
               "code": "plan_not_eligible" if isinstance(exc, NotEligible) else ""}
    out.update(seconds=round(time.monotonic() - started, 1), at=time.time())
    with _lock:
        _tests[which] = out
    return dict(out)


def forget_test(which: str) -> None:
    """After signing in again or installing, an old test result says nothing."""
    with _lock:
        _tests.pop(which, None)
