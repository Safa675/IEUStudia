"""How to start IEUStudia's processes: the app window, the tray helper (``--agent``) and the start-up window
(``--splash``), both for an installed release (one ``IEUStudia.exe``, built with PyInstaller) and for a source
install (``pythonw -m studia.app``). Also the "start with Windows" registry entry."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "IEUStudia"
DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def windowless_python() -> str:
    exe = Path(sys.executable)
    if exe.name.lower() == "python.exe" and (exe.parent / "pythonw.exe").exists():
        exe = exe.parent / "pythonw.exe"
    return str(exe)


def app_command(*args: str) -> list[str]:
    """The app (or, with ``--agent``, the tray helper) as a new process without a console window."""
    if frozen():
        return [sys.executable, *args]
    return [windowless_python(), "-m", "studia.app", *args]


def splash_command(*args: str) -> list[str]:
    """The start-up window: in a release the same exe (``--splash``), from source the base interpreter in isolated
    mode, which starts in a blink."""
    if frozen():
        return [sys.executable, "--splash", *args, "--frozen"]
    base = Path(getattr(sys, "_base_executable", "") or sys.executable)
    exe = base.with_name("pythonw.exe") if base.with_name("pythonw.exe").exists() else Path(windowless_python())
    return [str(exe), "-I", "-S", str(Path(__file__).with_name("splash.py")), *args]


def spawn(cmd: list[str]) -> bool:
    from .winutil import normal_priority

    try:
        with normal_priority():          # not the lower priority the app has while it transcribes
            subprocess.Popen(cmd, close_fds=True, creationflags=DETACHED)
        return True
    except OSError:
        return False


# ----------------------------------------------------------------------------- start with Windows
def _quote(parts: list[str]) -> str:
    return subprocess.list2cmdline(parts)


def startup_enabled() -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return bool(winreg.QueryValueEx(k, RUN_VALUE)[0])
    except OSError:
        return False


def set_startup(on: bool) -> None:
    """Start the tray helper when you sign in to Windows (HKCU Run key: no admin rights needed)."""
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
        if on:
            winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, _quote(app_command("--agent", "--at-login")))
        else:
            try:
                winreg.DeleteValue(k, RUN_VALUE)
            except FileNotFoundError:
                pass
