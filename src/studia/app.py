"""Desktop window (pywebview + WebView2) hosting the web UI.

Start-up order, for a window that appears at once: the window opens first (the page shows its start-up
screen), while the app's services start on a background thread; the UI's first call waits for them.

Threading rule: pywebview runs event handlers (shown, loaded, …) on worker threads. They must not touch
WinForms objects (``window.native``): pythonnet keeps the GIL during such calls while WinForms waits for
the UI thread, which in turn waits for the GIL — the window then freezes ("Not Responding").
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

from . import APP_NAME, __version__
from .winutil import acquire_single_instance, focus_window, set_app_id

ASSETS = Path(__file__).parent / "assets"
WEB = Path(__file__).parent / "web"
HANG_AFTER_S = 45
log = logging.getLogger("studia")


def _on_shown(api: Any, instance: str = "") -> None:
    """Window handle (taskbar flash) and icon, with plain Win32 calls only (see the threading rule)."""
    from .winutil import mark_instance, own_main_window, set_window_icon

    if instance:
        mark_instance(instance, "shown")
    hwnd = 0
    for _ in range(40):
        hwnd = own_main_window()
        if hwnd:
            break
        time.sleep(0.05)
    api._hwnd = hwnd
    from .winutil import process_age

    log.info("Window shown %.1f s after launch", process_age())
    ico = ASSETS / "icon.ico"
    if hwnd and ico.exists():
        set_window_icon(hwnd, ico)


def _start_watchdog(folder: Path) -> Optional[Any]:
    """If the page has not loaded after HANG_AFTER_S, write every thread's stack to startup-hang.txt."""
    import faulthandler

    try:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "startup-hang.txt"
        if path.exists() and path.stat().st_size:           # keep the last report for diagnosis
            os.replace(path, folder / "startup-hang-previous.txt")
        f = open(path, "w", encoding="utf-8")                # noqa: SIM115 - faulthandler writes to it later
        faulthandler.dump_traceback_later(HANG_AFTER_S, exit=False, file=f)
        return f
    except OSError:
        return None


def _stop_watchdog(f: Optional[Any]) -> None:
    import faulthandler

    faulthandler.cancel_dump_traceback_later()
    if f is None or f.closed:
        return
    name = f.name
    f.close()
    try:
        if os.path.getsize(name) == 0:
            os.remove(name)
    except OSError:
        pass


def main(argv: Optional[list[str]] = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["--splash"]:                 # a release build: the start-up window is this exe too
        from .splash import main as splash_main

        sys.exit(splash_main(args[1:]))
    if "--agent" in args:                        # the tray helper (no window)
        from .agent import main as agent_main

        agent_main(args)
        return
    if args[:1] == ["--selftest"]:               # checks a release build: imports and bundled files, no window
        sys.exit(_selftest(args[1] if len(args) > 1 else ""))
    try:
        _main(args)
    except Exception as exc:  # noqa: BLE001 - last resort: never fail silently under pythonw
        import ctypes
        import traceback

        log.exception("IEUStudia crashed")
        try:                                     # the folder in use (an earlier name's until it is moved)
            from .paths import logs_dir

            where = str(logs_dir())
        except Exception:  # noqa: BLE001
            where = "%LOCALAPPDATA%\\IEUStudia\\logs"
        ctypes.windll.user32.MessageBoxW(None, f"IEUStudia could not start:\n\n{exc}\n\n"
                                         f"{traceback.format_exc(limit=3)}\nDetails are in the log folder "
                                         f"({where}).", APP_NAME, 0x10)


def _selftest(out: str) -> int:
    """Import everything the app needs and check its bundled files; write the result as JSON to ``out``."""
    import importlib
    import json

    report: dict[str, Any] = {"version": __version__, "frozen": bool(getattr(sys, "frozen", False)), "ok": True,
                              "modules": {}, "files": {}}
    for mod in ("numpy", "requests", "av", "ctranslate2", "faster_whisper", "onnxruntime", "tokenizers", "pypdfium2",
                "yt_dlp", "webview", "webview.platforms.winforms", "studia.services", "studia.agent", "studia.tray",
                "studia.local_llm", "studia.splash"):
        try:
            m = importlib.import_module(mod)
            report["modules"][mod] = str(getattr(m, "__version__", "ok"))
        except Exception as exc:  # noqa: BLE001
            report["modules"][mod] = f"FAILED: {exc!r}"
            report["ok"] = False
    files = ["web/index.html", "web/app.js", "web/setup.js", "web/styles.css", "assets/icon.ico", "assets/logo.svg"]
    if not report["frozen"]:
        files.append("splash.py")               # a release runs the start-up window from the exe itself
    for rel in files:
        p = Path(__file__).parent / rel
        report["files"][rel] = p.is_file()
        report["ok"] = report["ok"] and p.is_file()
    try:
        import faster_whisper

        vad = Path(faster_whisper.__file__).parent / "assets" / "silero_vad_v6.onnx"
        report["files"]["faster_whisper/assets/silero_vad_v6.onnx"] = vad.is_file()
        report["ok"] = report["ok"] and vad.is_file()
    except Exception:  # noqa: BLE001
        report["ok"] = False
    text = json.dumps(report, indent=1)
    if out:
        Path(out).write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0 if report["ok"] else 1


def _windowless_python() -> str:
    from .launch import windowless_python

    return windowless_python()


def _start_splash(dark: bool, recovered: bool, marker: Optional[Path] = None, logs: Optional[Path] = None) -> None:
    """The native start-up window (its own process, so it shows and animates at once, whatever this one is busy
    with); it closes itself when the real window appears. ``marker``: the instance file where start-up notes its
    phase; ``logs``: where a stuck start leaves its diagnostics."""
    import subprocess

    from .launch import app_command, splash_command

    restart = app_command()          # how the start-up window starts a fresh copy if this one gets stuck
    try:
        subprocess.Popen(splash_command(str(os.getpid()), "dark" if dark else "light", "--python", restart[0],
                                        *(["--marker", str(marker)] if marker else []),
                                        *(["--logs", str(logs)] if logs else []),
                                        *(["--recovered"] if recovered else [])), close_fds=True)
    except OSError:
        log.debug("no start-up window", exc_info=True)


def _webview2_missing() -> bool:
    """Without the WebView2 runtime the window cannot show: say so (with the download) instead of failing."""
    from .hardware import webview2_version

    if webview2_version():
        return False
    from .winutil import message_box

    if message_box("IEUStudia needs the Microsoft Edge WebView2 Runtime, which is not installed on this PC.\n\n"
                   "It is free and comes with Windows 11 and current Windows 10. Open the download page now?",
                   APP_NAME, 0x4 | 0x30) == 6:
        import webbrowser

        webbrowser.open("https://developer.microsoft.com/microsoft-edge/webview2/#download")
    return True


def _note_window_creation(webview: Any, instance: str) -> None:
    """Mark the moment pywebview starts creating the WinForms window (after .NET has loaded). Creating it has
    been seen to hang inside Windows (CreateWindowEx waiting on an ALPC reply); the start-up window restarts
    the app when the window still isn't there well after this mark."""
    from .winutil import mark_instance

    init = getattr(webview, "initialize", None)
    if init is None or getattr(init, "_studia_marks", False):
        return

    def initialize(*args: Any, **kwargs: Any) -> Any:
        lib = init(*args, **kwargs)
        create = getattr(lib, "create_window", None)
        if create is not None and not getattr(create, "_studia_marks", False):
            def create_window(window: Any) -> Any:
                if getattr(window, "uid", "") == "master":
                    mark_instance(instance, "window")
                return create(window)

            create_window._studia_marks = True    # type: ignore[attr-defined]
            lib.create_window = create_window
        return lib

    initialize._studia_marks = True                # type: ignore[attr-defined]
    webview.initialize = initialize


def _other_instance(name: str) -> bool:
    """Another copy holds the single-instance lock. Bring its window (or its start-up window) forward. If it has
    shown nothing for a while it is stuck: offer to replace it. True if this copy should start after all."""
    import time

    from .winutil import (acquire_single_instance, bring_to_front, find_class_window, focus_window, is_hung, kill_tree,
                          message_box, read_instance)

    deadline = time.monotonic() + 3          # a double-click starts two copies at once: let the first one show up
    while True:
        if focus_window(APP_NAME):
            return False
        splash = find_class_window("StudiaSplash")
        if splash:
            bring_to_front(splash)
            return False
        if time.monotonic() > deadline:
            break
        time.sleep(0.1)
    info = read_instance(name)
    if time.time() - float(info.get("started") or 0) < 60:
        return False                          # still starting; its own start-up window handles a hang
    if message_box("IEUStudia is already running, but its window is not showing and it does not respond.\n\n"
                   "Close that copy and start IEUStudia again?", APP_NAME, 0x4 | 0x30) != 6:     # MB_YESNO, IDYES
        return False
    if info.get("pid"):
        kill_tree(int(info["pid"]))
    return acquire_single_instance(name, wait_s=15)


def _relaunch(argv: list[str]) -> None:
    """Start a fresh copy of the app (after switching profiles), windowless like this one. It waits for this
    process to exit (the single-instance lock) before it opens its window."""
    import subprocess

    from .launch import app_command

    args = [a for a in argv if a not in ("--restarted", "--recovered")] + ["--restarted"]
    try:
        subprocess.Popen(app_command(*args), close_fds=True,
                         creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        log.info("Restarting for another profile")
    except OSError:
        log.exception("Could not restart IEUStudia")


def _main(argv: Optional[list[str]] = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    debug = "--debug" in argv
    from .winutil import instance_name, write_instance

    name = instance_name()
    target = argv[argv.index("--open") + 1] if "--open" in argv[:-1] else ""
    if not acquire_single_instance(name, wait_s=30 if "--restarted" in argv else 0):
        if target:                               # e.g. a notification was clicked: the open window shows it
            from .winutil import request_open

            request_open(name, target)
        if not _other_instance(name):
            return
    if _webview2_missing():
        return
    write_instance(name)
    set_app_id()
    from . import appconfig
    from .accounts import activate
    from .config import load_settings
    from .log import setup_logging
    from .paths import app_dir, logs_dir, migrate_legacy_data
    from .winutil import process_age, windows_dark_mode

    moved = migrate_legacy_data()            # before anything opens the data folder
    appconfig.migrate()                      # the AI choice as an earlier version stored it
    account = activate()                     # whose library, settings and password this run uses
    theme = load_settings().theme
    dark = theme == "dark" or (theme == "system" and windows_dark_mode())
    from .winutil import _instance_file

    _start_splash(dark, "--recovered" in argv, _instance_file(name), logs_dir())   # on screen in a fraction of a second
    setup_logging(debug)
    log.info("IEUStudia %s starting (python %s), profile %s", __version__, sys.version.split()[0], account.id)
    if "--recovered" in argv:
        log.warning("Restarted by the start-up window after a stuck start (details: startup-stall.txt here)")
    if moved != "none":
        log.info("Data folder of the app's earlier name: %s", moved)

    def thread_error(args: threading.ExceptHookArgs) -> None:   # pythonw has no console: keep a trace
        log.error("Unhandled error in thread %s", getattr(args.thread, "name", "?"),
                  exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    threading.excepthook = thread_error
    import webview

    from .api import Api

    log.info("Modules loaded %.1f s after launch", process_age())
    api = Api()
    api._open_target = target
    # file:// (not pywebview's http server) so the page may show slide images stored on disk
    window = webview.create_window(APP_NAME, url=(WEB / "index.html").as_uri(), js_api=api, width=1280, height=820,
                                   min_size=(980, 640), background_color="#0e1016" if dark else "#f5f6fa",
                                   text_select=True)
    api._attach(window)
    started: dict[str, Any] = {}

    def start_services() -> None:
        try:
            from .services import Services

            services = Services(debug=debug)
        except Exception as exc:  # noqa: BLE001 - shown on the start-up screen
            log.exception("Services failed to start")
            api._startup_failed(f"IEUStudia could not open its data folder or library: {exc}")
            return
        started["services"] = services
        api._set_services(services)
        log.info("Services ready %.1f s after launch", process_age())
        try:                                     # the tray helper, if the user wants it running
            from .agent import ensure_running

            ensure_running()
        except Exception:  # noqa: BLE001
            log.debug("helper start failed", exc_info=True)

    threading.Thread(target=start_services, name="services-start", daemon=True).start()
    watchdog = _start_watchdog(logs_dir())

    def on_loaded() -> None:
        _stop_watchdog(watchdog)
        log.info("UI loaded %.1f s after launch", process_age())

    def on_closing() -> bool:     # runs on the UI thread
        services = started.get("services")
        if api._restart:          # switching profiles: the user already confirmed
            return True
        if services is not None and services.jobs.busy():
            ok = window.create_confirmation_dialog(
                "Transcription in progress", "A transcription is still running. Quit and cancel it?")
            if not ok:
                return False
            services.jobs.cancel(None)
        return True

    window.events.shown += lambda: _on_shown(api, name)
    window.events.loaded += on_loaded
    window.events.closing += on_closing
    _note_window_creation(webview, name)
    webview.start(gui="edgechromium", debug=debug, private_mode=False,
                  storage_path=str(app_dir() / "webview"))
    _stop_watchdog(watchdog)
    if started.get("services") is not None:
        started["services"].shutdown()
    log.info("IEUStudia closed")
    if api._restart:
        _relaunch(argv)


if __name__ == "__main__":
    main()
