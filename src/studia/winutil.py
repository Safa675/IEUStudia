"""Small Windows helpers (clipboard, single instance, focus) via ctypes."""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import subprocess
from pathlib import Path

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
ERROR_ALREADY_EXISTS = 183


def set_clipboard(text: str) -> bool:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GlobalAlloc.restype = wt.HGLOBAL
    kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [wt.HGLOBAL]
    kernel32.GlobalUnlock.argtypes = [wt.HGLOBAL]
    user32.SetClipboardData.argtypes = [wt.UINT, wt.HANDLE]
    data = (text or "").encode("utf-16-le") + b"\x00\x00"
    for _ in range(10):
        if user32.OpenClipboard(None):
            break
        kernel32.Sleep(50)
    else:
        return False
    try:
        user32.EmptyClipboard()
        h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        p = kernel32.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        kernel32.GlobalUnlock(h)
        return bool(user32.SetClipboardData(CF_UNICODETEXT, h))
    finally:
        user32.CloseClipboard()


_mutex = None


def instance_name() -> str:
    """The single-instance lock's name; a separate data folder (``STUDIA_HOME``) is a separate instance."""
    import hashlib

    home = os.environ.get("STUDIA_HOME", "").strip()
    return "Local\\Studia-SingleInstance" + ("-" + hashlib.sha1(home.lower().encode()).hexdigest()[:8] if home else "")


def _instance_file(name: str) -> Path:
    import tempfile

    return Path(tempfile.gettempdir()) / (name.split("\\")[-1] + ".json")


def write_instance(name: str) -> None:
    """Remember which process holds the lock (so a later click can replace it if it got stuck)."""
    import json
    import time

    try:
        _instance_file(name).write_text(json.dumps({"pid": os.getpid(), "started": time.time()}), encoding="utf-8")
    except OSError:
        pass


def read_instance(name: str) -> dict:
    import json

    try:
        d = json.loads(_instance_file(name).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def request_open(name: str, target: str) -> None:
    """Ask the running app window to show a page ("course:<id>", "home")."""
    import json
    import time

    try:
        _instance_file(name).with_name(name.split("\\")[-1] + "-open.json").write_text(
            json.dumps({"target": target, "at": time.time()}), encoding="utf-8")
    except OSError:
        pass


def take_open_request(name: str) -> str:
    import json
    import time

    f = _instance_file(name).with_name(name.split("\\")[-1] + "-open.json")
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        f.unlink()
    except (OSError, ValueError):
        return ""
    return str(d.get("target") or "") if time.time() - float(d.get("at") or 0) < 60 else ""


def mark_instance(name: str, phase: str) -> None:
    """Note how far start-up got ("window": the WinForms window is being created, "shown"). The start-up window
    (splash.py) reads it to tell a window that got stuck being created from a merely slow start."""
    import json
    import time

    d = read_instance(name)
    if d.get("pid") != os.getpid():
        d = {"pid": os.getpid(), "started": time.time()}
    d.update(phase=phase, phase_at=time.time())
    f = _instance_file(name)
    tmp = f.with_name(f.name + ".tmp")
    try:
        tmp.write_text(json.dumps(d), encoding="utf-8")
        os.replace(tmp, f)                              # the splash never reads half a file
    except OSError:
        pass


def find_class_window(class_name: str) -> int:
    user32 = ctypes.WinDLL("user32")
    user32.FindWindowW.restype = wt.HWND
    user32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
    return user32.FindWindowW(class_name, None) or 0


def is_hung(hwnd: int) -> bool:
    user32 = ctypes.WinDLL("user32")
    user32.IsHungAppWindow.argtypes = [wt.HWND]
    return bool(hwnd and user32.IsHungAppWindow(hwnd))


def bring_to_front(hwnd: int) -> None:
    user32 = ctypes.WinDLL("user32")
    user32.ShowWindow(hwnd, 9 if user32.IsIconic(hwnd) else 5)
    user32.SetForegroundWindow(hwnd)


def kill_tree(pid: int) -> None:
    """End a process and its children (WebView2 helpers)."""
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(int(pid))], capture_output=True, timeout=15,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError, ValueError):
        pass


def message_box(text: str, title: str = "IEUStudia", flags: int = 0x40) -> int:
    return ctypes.windll.user32.MessageBoxW(None, text, title, flags)


def acquire_single_instance(name: str = "Local\\Studia-SingleInstance", wait_s: float = 0.0) -> bool:
    """True if this is the only running instance. ``wait_s``: wait that long for a closing copy to exit (a restart)."""
    global _mutex
    import time

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wt.HANDLE
    kernel32.CloseHandle.argtypes = [wt.HANDLE]
    deadline = time.monotonic() + wait_s
    while True:
        _mutex = kernel32.CreateMutexW(None, False, name)
        if ctypes.get_last_error() != ERROR_ALREADY_EXISTS:
            return True
        if time.monotonic() >= deadline:
            return False
        kernel32.CloseHandle(_mutex)      # our own handle would keep the mutex alive after the other copy exits
        _mutex = None
        time.sleep(0.2)


def _main_window(title: str) -> int:
    """The app's WinForms main window (pywebview also creates small helper windows with the same title)."""
    user32 = ctypes.WinDLL("user32")
    found: list[int] = []
    proc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(hwnd, _):
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        if buf.value == title and user32.IsWindowVisible(hwnd):
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            if "WindowsForms" in cls.value:
                found.append(hwnd)
        return True

    user32.EnumWindows(proc(cb), 0)
    return found[0] if found else 0


def own_main_window() -> int:
    """This process's main window (the largest visible WinForms top-level window), 0 if not shown yet.

    Sends no window messages, so it is safe to call from any thread."""
    user32 = ctypes.WinDLL("user32")
    pid = os.getpid()
    best = [0, 0]                                   # hwnd, area
    proc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(hwnd, _):
        owner = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            r = wt.RECT()
            if "WindowsForms" in cls.value and user32.GetWindowRect(hwnd, ctypes.byref(r)):
                area = (r.right - r.left) * (r.bottom - r.top)
                if area > best[1]:
                    best[:] = [hwnd, area]
        return True

    user32.EnumWindows(proc(cb), 0)
    return best[0]


def set_window_icon(hwnd: int, ico: str | Path) -> bool:
    """Title-bar / taskbar icon via posted WM_SETICON messages: never waits, safe from any thread.

    (Setting the WinForms ``Form.Icon`` property from a background thread can deadlock with the UI
    thread, which once froze the window at start-up.)"""
    user32 = ctypes.WinDLL("user32")
    user32.LoadImageW.restype = wt.HANDLE
    user32.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT, ctypes.c_int, ctypes.c_int, wt.UINT]
    user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    ok = False
    for which, cx, cy in ((1, 11, 12), (0, 49, 50)):     # ICON_BIG: SM_CXICON/SM_CYICON, ICON_SMALL: SM_CXSMICON/…
        icon = user32.LoadImageW(None, str(ico), 1, user32.GetSystemMetrics(cx), user32.GetSystemMetrics(cy), 0x10)
        if icon:                                         # IMAGE_ICON, LR_LOADFROMFILE
            ok = bool(user32.PostMessageW(hwnd, 0x0080, which, icon)) or ok      # WM_SETICON
    return ok


def process_age() -> float:
    """Seconds since this process was created (includes Python's own start-up); 0.0 if unknown."""
    try:
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.GetCurrentProcess.restype = wt.HANDLE
        ft = ctypes.POINTER(wt.FILETIME)
        kernel32.GetProcessTimes.argtypes = [wt.HANDLE, ft, ft, ft, ft]
        kernel32.GetSystemTimeAsFileTime.argtypes = [ft]
        created, exited, kernel, user, now = (wt.FILETIME() for _ in range(5))
        if not kernel32.GetProcessTimes(kernel32.GetCurrentProcess(), ctypes.byref(created), ctypes.byref(exited),
                                        ctypes.byref(kernel), ctypes.byref(user)):
            return 0.0
        kernel32.GetSystemTimeAsFileTime(ctypes.byref(now))
    except (OSError, ctypes.ArgumentError):
        return 0.0
    ticks = lambda f: (f.dwHighDateTime << 32) | f.dwLowDateTime   # noqa: E731 - 100 ns units
    return max(0.0, (ticks(now) - ticks(created)) / 1e7)


def windows_dark_mode() -> bool:
    """Windows is set to dark mode for apps."""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def focus_window(title: str) -> bool:
    user32 = ctypes.WinDLL("user32")
    hwnd = _main_window(title)
    if not hwnd:
        return False
    user32.ShowWindow(hwnd, 9 if user32.IsIconic(hwnd) else 5)  # SW_RESTORE / SW_SHOW
    user32.SetForegroundWindow(hwnd)
    return True


class _FlashInfo(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("hwnd", wt.HWND), ("dwFlags", wt.DWORD), ("uCount", wt.UINT),
                ("dwTimeout", wt.DWORD)]


def flash_taskbar(hwnd: int) -> None:
    """Flash the taskbar button until the user switches to the window (only when it is in the background)."""
    if not hwnd:
        return
    user32 = ctypes.WinDLL("user32")
    if user32.GetForegroundWindow() == hwnd:
        return
    info = _FlashInfo(ctypes.sizeof(_FlashInfo), hwnd, 0x3 | 0xC, 0, 0)   # FLASHW_ALL | FLASHW_TIMERNOFG
    user32.FlashWindowEx(ctypes.byref(info))


def set_app_id(app_id: str = "Studia.App") -> None:
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except (AttributeError, OSError):
        pass


def open_path(path: str | Path) -> None:
    os.startfile(str(path))  # type: ignore[attr-defined]


def reveal(path: str | Path) -> None:
    p = Path(path)
    if p.is_file():
        subprocess.Popen(["explorer", "/select,", str(p)], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        os.startfile(str(p))  # type: ignore[attr-defined]


def work_area() -> tuple[float, float, float, float]:
    """The primary screen's usable area (without the taskbar) as (left, top, width, height) in 96-dpi pixels, the unit
    pywebview places and sizes windows in; all 0 if unknown."""
    user32 = ctypes.windll.user32
    r = wt.RECT()
    if not user32.SystemParametersInfoW(0x30, 0, ctypes.byref(r), 0):      # SPI_GETWORKAREA
        return 0.0, 0.0, 0.0, 0.0
    scale = 1.0
    try:                                     # a DPI-aware process gets physical pixels
        if user32.IsProcessDPIAware():
            scale = (user32.GetDpiForSystem() or 96) / 96
    except (AttributeError, OSError):
        pass
    return r.left / scale, r.top / scale, (r.right - r.left) / scale, (r.bottom - r.top) / scale


class _ReasonContext(ctypes.Structure):
    _fields_ = [("Version", wt.ULONG), ("Flags", wt.DWORD), ("SimpleReasonString", wt.LPWSTR),
                ("_rest", ctypes.c_byte * 16)]           # the union's detailed form is larger


class KeepAwake:
    """While on, Windows' idle timer does not put the PC to sleep (the screen may still turn off; closing a laptop's
    lid still sleeps it). Listed by ``powercfg /requests`` with the reason. On and off from any thread."""

    def __init__(self, reason: str) -> None:
        self._reason = reason
        self._handle = None
        self._on = False

    def on(self) -> None:
        self._set(True)

    def off(self) -> None:
        self._set(False)

    def _set(self, on: bool) -> None:
        if on == self._on:
            return
        try:
            k32 = ctypes.windll.kernel32
            if self._handle is None:
                k32.PowerCreateRequest.restype = wt.HANDLE
                ctx = _ReasonContext(0, 0x1, self._reason)     # POWER_REQUEST_CONTEXT_VERSION, SIMPLE_STRING
                handle = k32.PowerCreateRequest(ctypes.byref(ctx))
                if not handle or handle == wt.HANDLE(-1).value:
                    return
                self._handle = handle
            fn = k32.PowerSetRequest if on else k32.PowerClearRequest
            if fn(wt.HANDLE(self._handle), 1):                # PowerRequestSystemRequired
                self._on = on
        except (AttributeError, OSError):
            pass
