"""Linux counterparts of :mod:`winutil` and :mod:`tray` (picked by :mod:`osutil`).

The desktop parts use what a GNOME/KDE desktop already has: ``flock`` for the single-instance lock,
``systemd-inhibit`` to keep the PC awake, ``notify-send`` for notifications, ``zenity`` for message boxes and
``xdg-open`` for files and links. Windows-only cosmetics (the taskbar button's progress, flashing it, window icons by
handle) do nothing here. There is no icon next to the clock: GNOME has no notification area, so the background
checks run without one and only their notifications show.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from .i18n import t

# the parts that are plain files and JSON work the same on Linux
from .winutil import (_instance_file, instance_name, mark_instance, read_instance,  # noqa: F401
                      write_instance)
from .winutil import take_open_request as _take_open_request

log = logging.getLogger("studia")
ICON = Path(__file__).with_name("assets") / "icon.png"

# Windows message box flags and answers (callers pass and compare these)
MB_OKCANCEL, MB_YESNO = 0x1, 0x4
MB_ICONERROR, MB_ICONWARNING = 0x10, 0x30
IDOK, IDCANCEL, IDYES, IDNO = 1, 2, 6, 7


def _detached(cmd: list[str]) -> bool:
    try:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True, start_new_session=True)
        return True
    except OSError:
        return False


# ----------------------------------------------------------------------------- single instance
_locks: dict[str, int] = {}


def _lock_path(name: str) -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()     # per user, gone after a reboot
    return Path(base) / (name.split("\\")[-1] + ".lock")


def acquire_single_instance(name: str = "Local\\Studia-SingleInstance", wait_s: float = 0.0) -> bool:
    """True if this is the only running instance. ``wait_s``: wait that long for a closing copy to exit (a restart)."""
    import fcntl

    if name in _locks:
        return True
    fd = os.open(_lock_path(name), os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + wait_s
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _locks[name] = fd                    # held until the process ends or release_single_instance
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(fd)
                return False
            time.sleep(0.2)


def release_single_instance() -> None:
    import fcntl

    for fd in _locks.values():
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
    _locks.clear()


def instance_held(name: str) -> bool:
    """Does a running process (this one too) hold the single-instance lock ``name``?"""
    import fcntl

    path = _lock_path(name)
    if name in _locks:
        return True
    if not path.exists():
        return False
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(fd)


def _alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


# ----------------------------------------------------------------------------- windows
# pywebview owns the window; "hwnd" 1 stands for it (0: not shown yet), the calls below act on webview.windows.
def _windows() -> list[Any]:
    try:
        import webview

        return list(webview.windows)
    except Exception:  # noqa: BLE001
        return []


def own_main_window() -> int:
    return 1 if _windows() else 0


def bring_to_front(hwnd: int) -> None:
    for w in _windows()[:1]:
        with contextlib.suppress(Exception):
            w.restore()
            w.show()


def focus_window(title: str) -> bool:
    """Another copy runs (it holds the lock): ask it to come forward, as on Windows. Wayland doesn't let one program
    raise another's window, so the running copy does it itself when it sees the request (take_open_request)."""
    name = instance_name()
    info = read_instance(name)
    if not info.get("pid") or not _alive(info["pid"]):
        return False
    pending = _instance_file(name).with_name(name.split("\\")[-1] + "-open.json")
    if not pending.exists():                  # a notification's page is already asked for: keep that one
        request_open(name, "home")
    return True


def request_open(name: str, target: str) -> None:
    """Ask the running app window to show a page ("course:<id>", "home"): the page goes in a file, which that window
    looks at once a second (Linux has no named events like Windows)."""
    import json

    try:
        _instance_file(name).with_name(name.split("\\")[-1] + "-open.json").write_text(
            json.dumps({"target": target, "at": time.time()}), encoding="utf-8")
    except OSError:
        pass


def open_request_event(name: str) -> int:
    return 0                                     # no event: the window polls the file (see api._watch_open_requests)


def wait_event(event: int, timeout_s: float | None = None) -> bool:
    return False


def take_open_request(name: str) -> str:
    target = _take_open_request(name)
    if target:
        bring_to_front(1)
    return target


def find_class_window(class_name: str) -> int:
    return 0                                     # no start-up window on Linux


def is_hung(hwnd: int) -> bool:
    return False


def is_foreground(hwnd: int) -> bool:
    return False                                 # unknown under Wayland: tell rather than stay quiet


def flash_taskbar(hwnd: int) -> None:
    pass


def set_window_icon(hwnd: int, ico: str | Path) -> bool:
    return False                                 # the window gets its icon from webview.start(icon=...)


def set_app_id(app_id: str = "Studia.App") -> None:
    pass


def work_area() -> tuple[float, float, float, float]:
    return 0.0, 0.0, 0.0, 0.0                    # unknown: the window opens at its default size


class TaskbarProgress:
    OFF, INDETERMINATE, NORMAL, ERROR, PAUSED = 0x0, 0x1, 0x2, 0x4, 0x8

    def set(self, hwnd: int, state: int, fraction: float = 0.0) -> None:
        pass


def kill_tree(pid: int) -> None:
    """End a process and its children."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return
    children: dict[int, list[int]] = {}
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = stat.read_text().rsplit(")", 1)[1].split()
            children.setdefault(int(fields[1]), []).append(int(stat.parent.name))
        except (OSError, IndexError, ValueError):
            continue
    todo, tree = [pid], []
    while todo:
        p = todo.pop()
        tree.append(p)
        todo += children.get(p, [])
    for p in reversed(tree):
        with contextlib.suppress(OSError):
            os.kill(p, signal.SIGKILL)


def message_box(text: str, title: str = "IEUStudia", flags: int = 0x40) -> int:
    """A Windows-style MessageBox through zenity: returns IDYES/IDNO for MB_YESNO, IDOK/IDCANCEL otherwise."""
    buttons = flags & 0xF
    zenity = shutil.which("zenity")
    if zenity:
        if buttons in (MB_YESNO, MB_OKCANCEL):
            kind = ["--question"] + ([f"--ok-label={t('Yes')}", f"--cancel-label={t('No')}"] if buttons == MB_YESNO else [])
        else:
            kind = ["--error" if flags & 0xF0 == MB_ICONERROR else
                    "--warning" if flags & 0xF0 == MB_ICONWARNING else "--info"]
        try:
            r = subprocess.run([zenity, *kind, f"--title={title}", f"--text={text}", "--no-markup", "--width=420"],
                               capture_output=True, timeout=3600)
            ok = r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
    else:
        log.warning("%s: %s", title, text)
        ok = False                               # nobody could answer: the safe choice
    if buttons == MB_YESNO:
        return IDYES if ok else IDNO
    if buttons == MB_OKCANCEL:
        return IDOK if ok else IDCANCEL
    return IDOK


def windows_dark_mode() -> bool:
    """The desktop prefers dark (GNOME's colour scheme, or a dark GTK theme)."""
    try:
        r = subprocess.run(["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
                           capture_output=True, text=True, timeout=5)
        if "dark" in r.stdout:
            return True
        r = subprocess.run(["gsettings", "get", "org.gnome.desktop.interface", "gtk-theme"],
                           capture_output=True, text=True, timeout=5)
        return "dark" in r.stdout.lower()
    except Exception:  # noqa: BLE001 - cosmetic: no gsettings (KDE, a server) means light
        return False


def process_age() -> float:
    """Seconds since this process was created; 0.0 if unknown."""
    try:
        start_ticks = int(Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()[19])
        uptime = float(Path("/proc/uptime").read_text().split()[0])
        return max(0.0, uptime - start_ticks / os.sysconf("SC_CLK_TCK"))
    except (OSError, IndexError, ValueError):
        return 0.0


# ----------------------------------------------------------------------------- files, links, clipboard
def open_in_browser(url: str) -> None:
    import webbrowser

    webbrowser.open(url)


def open_path(path: str | Path) -> None:
    _detached(["xdg-open", str(path)])


def reveal(path: str | Path) -> None:
    """Show a file selected in the file manager (the FileManager1 D-Bus interface), else open its folder."""
    p = Path(path)
    if p.is_file():
        try:
            r = subprocess.run(["gdbus", "call", "--session", "--dest", "org.freedesktop.FileManager1",
                                "--object-path", "/org/freedesktop/FileManager1",
                                "--method", "org.freedesktop.FileManager1.ShowItems", f"['{p.as_uri()}']", ""],
                               capture_output=True, timeout=10)
            if r.returncode == 0:
                return
        except (OSError, subprocess.SubprocessError):
            pass
        p = p.parent
    open_path(p)


def set_clipboard(text: str) -> bool:
    for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
        if shutil.which(cmd[0]):
            try:
                return subprocess.run(cmd, input=(text or "").encode("utf-8"), timeout=10).returncode == 0
            except (OSError, subprocess.SubprocessError):
                continue
    for w in _windows()[:1]:                     # no clipboard tool: the page's own clipboard
        try:
            w.evaluate_js(f"navigator.clipboard.writeText({json.dumps(text or '')})")
            return True
        except Exception:  # noqa: BLE001
            pass
    return False


def run_in_terminal(cmd: list[str], env: Optional[dict[str, str]] = None) -> bool:
    """Run a command (a CLI's sign-in) in a terminal window the user can type in, like a new console on Windows."""
    for term in ("x-terminal-emulator", "ptyxis", "gnome-terminal", "kgx", "konsole", "xterm"):
        exe = shutil.which(term)
        if not exe:
            continue
        sep = ["--"] if term in ("ptyxis", "gnome-terminal", "kgx") else ["-e"]
        try:
            subprocess.Popen([exe, *sep, *cmd], env=env, close_fds=True, start_new_session=True)
            return True
        except OSError:
            continue
    return False


# ----------------------------------------------------------------------------- priority and sleep
def set_low_priority(on: bool) -> None:
    """Nothing on Linux: a process can lower its priority but not raise it back without privileges, and the
    scheduler already shares the processor fairly with the programs you use."""


@contextlib.contextmanager
def background_work() -> Iterator[None]:
    """The tray helper's checks run as they are: the scheduler already puts the programs you use first."""
    yield


@contextlib.contextmanager
def normal_priority() -> Iterator[None]:
    yield


class KeepAwake:
    """While on, the desktop doesn't suspend for being idle (a ``systemd-inhibit`` lock, listed by
    ``systemd-inhibit --list``). On and off from any thread."""

    def __init__(self, reason: str) -> None:
        self._reason = reason
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()

    def on(self) -> None:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return
            exe = shutil.which("systemd-inhibit")
            if not exe:
                return
            try:
                self._proc = subprocess.Popen([exe, "--what=idle:sleep", "--who=IEUStudia", f"--why={self._reason}",
                                               "--mode=block", "sleep", "infinity"],
                                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
            except OSError:
                self._proc = None

    def off(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(5)


# ----------------------------------------------------------------------------- notifications (tray.py's interface)
def _notify(title: str, text: str, on_click: Callable[[], None]) -> None:
    """A desktop notification; clicking it (its "Open" action) calls ``on_click``."""
    exe = shutil.which("notify-send")
    if not exe:
        log.info("Notification (no notify-send): %s: %s", title, text)
        return

    def run() -> None:
        try:
            r = subprocess.run([exe, "--app-name=IEUStudia", f"--icon={ICON}", f"--action=default={t('Open')}",
                                title[:63], text[:255]], capture_output=True, text=True, timeout=24 * 3600)
            if r.stdout.strip() == "default":
                on_click()
        except (OSError, subprocess.SubprocessError):
            log.debug("notification failed", exc_info=True)

    threading.Thread(target=run, name="notification", daemon=True).start()


MenuItem = tuple[int, str, bool, bool]


class Tray:
    """The tray helper's side of :class:`tray.Tray` without an icon: ``run()`` waits until ``stop()``; notifications
    go to the desktop, and clicking one calls ``on_open`` with its payload."""

    def __init__(self, tooltip: str, icon: Path, menu: Callable[[], list[MenuItem]],
                 on_command: Callable[[int], None], on_open: Callable[[Optional[Any]], None],
                 class_name: str = "IEUStudiaTray", title: str = "IEUStudia helper",
                 on_power: Optional[Callable[[str], None]] = None) -> None:
        self.on_power = on_power                 # never called: the screen and sleep events are Windows messages
        self.tooltip = tooltip
        self.icon_path = Path(icon)
        self.menu = menu
        self.on_command = on_command
        self.on_open = on_open
        self.ready = threading.Event()
        self._stopped = threading.Event()

    def notify(self, title: str, text: str, payload: Any = None) -> None:
        _notify(title, text, lambda: self.on_open(payload))

    def set_tooltip(self, text: str) -> None:
        self.tooltip = text[:127]

    def stop(self) -> None:
        self._stopped.set()

    def run(self) -> None:
        self.ready.set()
        self._stopped.wait()


class Notifier:
    """Notifications from the app itself (:class:`tray.Notifier`'s interface)."""

    def __init__(self, icon: Path, on_click: Callable[[Optional[Any]], None]) -> None:
        self.icon = Path(icon)
        self.on_click = on_click

    def notify(self, title: str, text: str, payload: Any = None) -> None:
        _notify(title, text, lambda: self.on_click(payload))

    def close(self) -> None:
        pass


# ----------------------------------------------------------------------------- start at login (XDG autostart)
def _autostart_file() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "autostart" / "ieustudia-helper.desktop"


def _desktop_quote(arg: str) -> str:
    """An Exec= argument (Desktop Entry spec): quoted when it has spaces or reserved characters."""
    if arg and not any(c in arg for c in ' \t\n"\'\\><~|&;$*?#()`'):
        return arg
    return '"' + "".join("\\" + c if c in '"`$\\' else c for c in arg) + '"'


def startup_enabled() -> bool:
    return _autostart_file().is_file()


def set_startup(on: bool, cmd: Optional[list[str]] = None) -> None:
    """Start the background checks when you log in (``~/.config/autostart``: no admin rights needed)."""
    path = _autostart_file()
    if not on:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    exec_line = " ".join(_desktop_quote(a) for a in (cmd or []))
    path.write_text("[Desktop Entry]\nType=Application\nName=IEUStudia background checks\n"
                    f"Exec={exec_line}\nIcon={ICON}\nNoDisplay=true\nX-GNOME-Autostart-enabled=true\n",
                    encoding="utf-8")
