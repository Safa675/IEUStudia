"""The bridge the web UI talks to (pywebview js_api). Methods never raise: errors come back as
``{"ok": false, "error": "..."}``; long work runs in threads and reports through events."""

from __future__ import annotations

import datetime as dt
import functools
import json
import logging
import queue
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any, Callable, Optional

from . import __version__, store, views
from .ieulearn import AuthError, NeedLogin
from .net import NetworkError
from .services import Services
from .winutil import flash_taskbar, open_path, reveal, set_clipboard

log = logging.getLogger("studia")
AUTO_SYNC_S = 30 * 60        # while the app is open: look for new lectures, materials and announcements


def safe(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def wrapper(self: "Api", *args: Any, **kwargs: Any) -> Any:
        try:
            return fn(self, *args, **kwargs)
        except (AuthError, NeedLogin, NetworkError) as exc:
            return {"ok": False, "error": str(exc), "need_login": isinstance(exc, (AuthError, NeedLogin))}
        except Exception as exc:  # noqa: BLE001
            log.exception("API %s failed", fn.__name__)
            msg = exc.args[0] if isinstance(exc, KeyError) and exc.args else exc   # KeyError's str() adds quotes
            return {"ok": False, "error": str(msg) or exc.__class__.__name__}
    return wrapper


class Api:
    def __init__(self, services: Optional[Services] = None) -> None:
        self._services: Optional[Services] = None
        self._ready = threading.Event()
        self._startup_error = ""
        self._window: Any = None
        self._events: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self._sync_lock = threading.Lock()
        self._ai_cancel: Optional[threading.Event] = None
        self._courses_dirty = threading.Event()
        self._mat_lock = threading.Lock()
        self._mat_queue: list[str] = []
        self._mat_running = False
        self._restart = False             # switching profiles: app.py starts a fresh copy once the window closed
        self._open_target = ""            # --open course:<id> (a notification was clicked)
        self._setup_cancel: Optional[threading.Event] = None
        self._setup_running = False
        threading.Thread(target=self._dispatch, name="ui-events", daemon=True).start()
        if services is not None:
            self._set_services(services)

    # pywebview exposes public attributes too: keep non-API state private (leading underscore)
    def _attach(self, window: Any) -> None:
        self._window = window

    def _set_services(self, services: Services) -> None:
        """The app's services start in the background while the window opens; calls wait for them."""
        self._services = services
        services.subscribe(self._on_service_event)
        self._ready.set()
        threading.Thread(target=self._auto_sync_loop, name="auto-sync", daemon=True).start()
        threading.Thread(target=self._watch_open_requests, name="open-requests", daemon=True).start()

    def _auto_sync_loop(self) -> None:
        """A quiet sync every AUTO_SYNC_S while the app is open, so new lectures, materials and announcements (and
        with them the heads-up) show up by themselves. Nothing is downloaded or transcribed, and a failure only
        shows in the top bar (no sign-in screen popping up while you read)."""
        while True:
            time.sleep(AUTO_SYNC_S)
            try:
                if self._services is None or not self._services.sync.has_credentials():
                    continue
            except Exception:  # noqa: BLE001
                continue
            if self._sync_lock.acquire(blocking=False):        # not while another sync or a full sync runs
                self._run_sync(quiet=True)

    def _startup_failed(self, message: str) -> None:
        self._startup_error = message or "IEUStudia could not start."
        self._ready.set()

    @property
    def _s(self) -> Services:
        if not self._ready.wait(120):
            raise RuntimeError("IEUStudia is still starting. Try again in a moment.")
        if self._services is None:
            raise RuntimeError(self._startup_error)
        return self._services

    # ------------------------------------------------------------------ events
    def _push(self, evt: dict[str, Any]) -> None:
        self._events.put(evt)

    def _on_service_event(self, evt: dict[str, Any]) -> None:
        self._push(evt)
        if evt.get("type") == "job" and evt.get("final"):
            self._courses_dirty.set()
            if evt.get("batch_done") and evt.get("stage") in ("done", "failed"):
                try:
                    flash_taskbar(getattr(self, "_hwnd", 0))
                except Exception:  # noqa: BLE001 - cosmetic
                    pass

    def _dispatch(self) -> None:
        """Deliver events to the page from one thread; coalesce bursts (only the latest job/queue state)."""
        while True:
            batch = [self._events.get()]
            time.sleep(0.05)
            while True:
                try:
                    batch.append(self._events.get_nowait())
                except queue.Empty:
                    break
            latest: dict[str, dict[str, Any]] = {}
            ordered: list[dict[str, Any]] = []
            for e in batch:
                if e.get("type") == "job" and not e.get("final"):
                    latest[f"job:{e['id']}"] = e
                elif e.get("type") == "chat":        # full snapshots of one answer: the newest one wins
                    latest[f"chat:{e['chat_id']}:{e['seq']}"] = e
                elif e.get("type") == "material":    # likewise per file (progress, then done/error)
                    latest[f"mat:{e['id']}"] = e
                elif e.get("type") == "plan":        # and per study plan
                    latest[f"plan:{e['id']}"] = e
                elif e.get("type") in ("queue", "engine", "ai"):
                    latest[e["type"]] = e
                else:
                    ordered.append(e)
            out = ordered + list(latest.values())
            if self._courses_dirty.is_set():
                self._courses_dirty.clear()
                out.append({"type": "courses", "courses": self._course_views()})
            if self._window is None:
                continue
            run = getattr(self._window, "run_js", None) or self._window.evaluate_js   # run_js doesn't block
            for e in out:
                try:
                    payload = json.dumps(e, ensure_ascii=False, default=str).replace("</", "<\\/")
                    run(f"window.__lsEvent && window.__lsEvent({payload})")
                except Exception:  # noqa: BLE001 - window closing
                    log.debug("event delivery failed", exc_info=True)

    def _toast(self, message: str, level: str = "info") -> None:
        self._push({"type": "toast", "level": level, "message": message})

    def _course_views(self) -> list[dict[str, Any]]:
        return views.all_course_views(self._s.lib, self._s.settings, self._s.jobs.snapshot())

    # ------------------------------------------------------------------ bootstrap / system
    @safe
    def get_bootstrap(self) -> dict[str, Any]:
        from . import accounts

        profile = self._s.sync.profile
        # Signed in = a known profile plus something to authenticate with (stored password or session
        # cookies). If the session turns out to be dead and no password is stored, the first sync
        # reports need_login and the UI switches to the sign-in screen.
        signed_in = bool(profile and profile.name) and self._s.sync.can_resume()
        active_id = accounts.load()[0]
        if profile and profile.username:          # whose library this is (the profile switcher lists it)
            accounts.remember(active_id, profile.username, profile.name, profile.student_number)
        refresh = threading.Timer(6.0, self._system_info)    # GPU / Claude check, once the first screens are up
        refresh.daemon = True
        refresh.start()
        try:
            cached_system = json.loads(self._s.lib.get_meta("system_info") or "null")
        except ValueError:
            cached_system = None
        from . import appconfig
        from .ai import usage_limits

        target, self._open_target = self._open_target, ""
        return {
            "ok": True, "version": __version__, "signed_in": signed_in, "system": cached_system,
            "limits": usage_limits(), "app": appconfig.load().to_json(), "open": target,
            "first_run": not self._s.settings.first_run_done,
            "profile": profile.__dict__ if profile else None,
            "settings": self._s.settings.to_json(), "last_sync": self._s.lib.get_meta("last_sync"),
            "courses": self._course_views() if signed_in else [], "queue": self._s.jobs.snapshot(),
            **self._profiles_view(),
        }

    def _system_info(self) -> None:
        try:
            from .ai import claude_info
            from .engine.models import MODEL_PRESETS, detect_gpu, model_is_downloaded

            gpu = detect_gpu(quick=True)
            presets = [{"id": k, "label": v["label"], "description": v["description"], "size_mb": v["size_mb"],
                        "downloaded": model_is_downloaded(k)} for k, v in MODEL_PRESETS.items()]
            from .components import cuda_ready

            info = {"gpu": {**gpu, "libraries": cuda_ready()}, "presets": presets, "ai": claude_info(),
                    "engine": {"model": self._s.settings.model, "loaded": self._s.jobs.engine_loaded()}}
            self._s.lib.set_meta("system_info", json.dumps(info))
            self._push({"type": "system", **info})
        except Exception:  # noqa: BLE001
            log.exception("system info failed")

    # ------------------------------------------------------------------ first-start setup and machine settings
    @safe
    def setup_state(self) -> dict[str, Any]:
        """What the first-start screen needs: this PC's check, the components and their sizes, the choices so far."""
        from . import appconfig, hardware, launch
        from .components import catalog

        facts = hardware.check(output_dir=Path(self._s.settings.output_dir))
        verdict = hardware.assess(facts)
        cfg = appconfig.load()
        return {"ok": True, "facts": facts, "verdict": verdict, "catalog": catalog(facts), "app": cfg.to_json(),
                "startup": launch.startup_enabled(), "model": self._s.settings.model,
                "output_dir": self._s.settings.output_dir, "frozen": launch.frozen()}

    @safe
    def setup_plan(self, speech: str, llm: str = "") -> dict[str, Any]:
        """The downloads a choice needs, their total and whether the disk has room."""
        from . import hardware
        from .components import plan

        facts = hardware.check()
        verdict = hardware.assess(facts)
        items = plan(speech, llm, gpu=verdict["gpu"])
        need_gb = sum(i["size_mb"] for i in items) / 1024 * 1.4          # unpacked + room to work
        again = hardware.assess(facts, need_gb)
        return {"ok": True, "items": items, "total_mb": sum(i["size_mb"] for i in items),
                "free_gb": facts["disk"]["data_free_gb"], "blockers": again["blockers"]}

    @safe
    def setup_download(self, ids: list[str]) -> dict[str, Any]:
        """Download the components one after another in the background (events: type "setup")."""
        from .components import download

        if self._setup_running:
            return {"ok": True, "running": True}
        self._setup_running = True
        cancel = self._setup_cancel = threading.Event()

        def run() -> None:
            failed = ""
            try:
                for cid in ids:
                    last = [0.0]

                    def progress(done: int, total: int, cid: str = cid) -> None:
                        now = time.monotonic()
                        if now - last[0] > 0.25 or done >= total:
                            last[0] = now
                            self._push({"type": "setup", "id": cid, "state": "downloading", "done": done, "total": total})

                    self._push({"type": "setup", "id": cid, "state": "downloading", "done": 0, "total": 0})
                    try:
                        download(cid, progress, cancel)
                        self._push({"type": "setup", "id": cid, "state": "done"})
                    except Exception as exc:  # noqa: BLE001
                        if cancel.is_set():
                            self._push({"type": "setup", "id": cid, "state": "cancelled"})
                            return
                        log.exception("Download of %s failed", cid)
                        failed = str(exc) or "Download failed"
                        self._push({"type": "setup", "id": cid, "state": "error", "error": failed})
                        return
            finally:
                self._setup_running = False
                self._push({"type": "setup", "id": "", "state": "idle", "ok": not failed and not cancel.is_set()})
                threading.Thread(target=self._system_info, daemon=True).start()

        threading.Thread(target=run, name="setup-download", daemon=True).start()
        return {"ok": True}

    @safe
    def setup_cancel(self) -> dict[str, Any]:
        if self._setup_cancel is not None:
            self._setup_cancel.set()
        return {"ok": True}

    @safe
    def app_config(self, patch: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Read or change the machine-wide settings (AI choice, background helper, notifications)."""
        from . import agent, appconfig, launch

        cfg = appconfig.update(patch) if patch else appconfig.load()
        if patch and "start_with_windows" in patch:
            launch.set_startup(bool(cfg.start_with_windows and cfg.tray))
        if patch and "tray" in patch:
            if cfg.tray:
                agent.ensure_running()
            elif launch.startup_enabled():
                launch.set_startup(False)
        if patch and {"ai_provider", "codex_model", "local_model"} & set(patch):
            from .ai import claude_cli_info, codex_cli_info

            claude_cli_info.cache_clear()
            codex_cli_info.cache_clear()
            threading.Thread(target=self._system_info, daemon=True).start()
        return {"ok": True, "app": cfg.to_json(), "startup": launch.startup_enabled(), "helper": agent.running()}

    @safe
    def ai_status(self, which: str = "") -> dict[str, Any]:
        from .ai import ai_status

        return {"ok": True, **ai_status(which or "")}

    @safe
    def ai_login(self, which: str, consent: bool = False) -> dict[str, Any]:
        """Sign in: "claude" opens Claude Code's own sign-in; "codex" is "Continue with ChatGPT" in the browser
        (events: type "chatgpt"); "codex-cli" opens the Codex CLI's own sign-in."""
        from . import ai_setup, chatgpt
        from .ai import start_login

        if which == "codex":
            ai_setup.forget_test("codex")
            return {"ok": True, **chatgpt.start_sign_in(self._push, consent=bool(consent))}
        start_login("codex" if which == "codex-cli" else which)
        return {"ok": True}

    @safe
    def ai_sign_out(self, which: str) -> dict[str, Any]:
        """Sign out of ChatGPT ("Continue with ChatGPT"): OpenAI revokes the session, IEUStudia forgets it."""
        from . import ai_setup, chatgpt

        if which != "codex":
            return {"ok": False, "error": "Sign out of Claude in Claude Code itself (claude auth logout)."}
        ai_setup.forget_test("codex")
        return {"ok": True, **chatgpt.sign_out()}

    @safe
    def ai_install(self, which: str) -> dict[str, Any]:
        """Install Claude Code, the Codex CLI or Git for Windows in the background (events: type "ai_setup")."""
        from .ai_setup import start_install

        return {"ok": True, "job": start_install(which, self._push)}

    @safe
    def ai_test(self, which: str) -> dict[str, Any]:
        """A short test message through Claude Code or the Codex CLI (a few tokens of the user's plan)."""
        from .ai_setup import test

        return {"ok": True, "test": test(which)}

    @safe
    def finish_setup(self) -> dict[str, Any]:
        from . import agent, appconfig

        cfg = appconfig.update({"onboarded": True, "terms_accepted": dt.date.today().isoformat()})
        if not self._s.settings.first_run_done:
            self._s.update_settings({"first_run_done": True})
        if cfg.tray:
            agent.ensure_running()
        return {"ok": True, "app": cfg.to_json()}

    @safe
    def storage(self) -> dict[str, Any]:
        from . import hardware
        from .components import catalog, disk_usage

        facts = hardware.check()
        return {"ok": True, "usage": disk_usage(Path(self._s.settings.output_dir)), "catalog": catalog(facts),
                "free_gb": facts["disk"]["data_free_gb"], "data_dir": facts["disk"]["data_dir"]}

    @safe
    def delete_component(self, cid: str) -> dict[str, Any]:
        from .components import delete_llm
        from .engine.models import delete_model

        kind, _, key = cid.partition(":")
        if kind == "speech":
            if key == self._s.settings.model:
                return {"ok": False, "error": "That is the speech model in use; pick another one first."}
            delete_model(key)
        elif kind == "llm":
            from . import appconfig

            if appconfig.load().ai_provider == "local" and appconfig.load().local_model == key:
                return {"ok": False, "error": "That offline model is in use; pick another AI first."}
            delete_llm(key)
        else:
            return {"ok": False, "error": "This part cannot be removed."}
        return self.storage()

    def _watch_open_requests(self) -> None:
        """Another start of the app (e.g. a notification was clicked) asks this window to show a page."""
        from .winutil import instance_name, take_open_request

        name = instance_name()
        while True:
            time.sleep(1.0)
            target = take_open_request(name)
            if target:
                self._push({"type": "open", "target": target})

    # ------------------------------------------------------------------ account / sync
    @safe
    def sign_in(self, username: str, password: str) -> dict[str, Any]:
        from . import accounts

        cur = accounts.active()
        if cur.username and cur.username.strip().casefold() != (username or "").strip().casefold():
            # someone else: their courses and transcripts get a profile of their own (then the UI switches to it)
            res = self.profile_add(username, password)
            if not res.get("ok"):
                return res
            return {"ok": True, "switch_to": res["profile"]["id"], "profile": res["profile"]}
        profile = self._s.sync.sign_in(username, password)
        accounts.remember(cur.id, profile.username, profile.name, profile.student_number)
        self.sync()
        return {"ok": True, "profile": profile.__dict__}

    @safe
    def sign_out(self) -> dict[str, Any]:
        self._s.jobs.cancel(None)
        self._s.sync.sign_out()
        return {"ok": True, **self._profiles_view()}

    # ------------------------------------------------------------------ profiles (several accounts on this PC)
    def _profiles_view(self) -> dict[str, Any]:
        from . import accounts

        active_id, accs = accounts.load()
        return {"profile_id": active_id, "profiles": [a.view(active_id) for a in accs]}

    @safe
    def profiles(self) -> dict[str, Any]:
        return {"ok": True, **self._profiles_view()}

    @safe
    def profile_add(self, username: str, password: str) -> dict[str, Any]:
        """Sign in to another IEULearn account and keep it as a profile of its own (nothing switches yet)."""
        from . import accounts, secrets_store
        from .sync import SyncService

        username = (username or "").strip()
        if not username or not password:
            return {"ok": False, "error": "Enter the username and password."}
        cur = accounts.active()
        if cur.username and cur.username.casefold() == username.casefold():
            return {"ok": False, "error": "That is the profile you are using now."}
        profile, courses, cookies = SyncService.check_account(username, password)
        acc = accounts.find(username)
        created = acc is None
        if acc is None:
            acc = accounts.create(username, profile.name, profile.student_number)
            accounts.seed(acc, profile, courses, self._s.settings)
        else:
            accounts.remember(acc.id, username, profile.name, profile.student_number)
        secrets_store.set_credentials(username, password, folder=acc.path)
        secrets_store.save_cookies(cookies, folder=acc.path)
        log.info("Profile %s ready (%s)", acc.id, "new" if created else "existing")
        active_id = accounts.load()[0]
        return {"ok": True, "created": created, "profile": (accounts.get(acc.id) or acc).view(active_id)}

    @safe
    def profile_switch(self, profile_id: str, force: bool = False) -> dict[str, Any]:
        """Open another profile: the app closes and starts again with that account's library."""
        from . import accounts

        if profile_id == accounts.load()[0]:
            return {"ok": True, "same": True}
        if self._services is not None and self._services.jobs.busy():
            if not force:
                return {"ok": False, "busy": True, "error": "A transcription is still running."}
            self._services.jobs.cancel(None)
        acc = accounts.set_active(profile_id)
        log.info("Switching to profile %s", acc.id)
        self._restart = True
        timer = threading.Timer(0.4, self._close_for_restart)     # the page shows "Switching…" first
        timer.daemon = True
        timer.start()
        return {"ok": True, "profile": acc.view(acc.id)}

    def _close_for_restart(self) -> None:
        try:
            self._window.destroy()
        except Exception:  # noqa: BLE001 - the window is already gone
            log.debug("closing for restart failed", exc_info=True)

    @safe
    def profile_remove(self, profile_id: str) -> dict[str, Any]:
        from . import accounts

        accounts.remove(profile_id)
        return {"ok": True, **self._profiles_view()}

    @safe
    def sync(self) -> dict[str, Any]:
        if not self._sync_lock.acquire(blocking=False):
            return {"ok": True, "running": True}
        threading.Thread(target=self._run_sync, name="sync", daemon=True).start()
        return {"ok": True}

    def _run_sync(self, quiet: bool = False) -> bool:
        """One sync (the caller holds ``_sync_lock``; released here). True when it worked. ``quiet``: started by the
        app itself (the page then shows errors in the top bar only)."""
        q = {"quiet": True} if quiet else {}
        try:
            self._push({"type": "sync", "phase": "start", "message": "Syncing…", **q})
            res = self._s.sync.sync(
                lambda m: self._push({"type": "sync", "phase": "progress", "message": m, **q}),
                lambda ids: self._push({"type": "courses", "courses": self._course_views()}))
            course_views = self._course_views()
            self._push({"type": "courses", "courses": course_views})
            new = self._own_new(course_views, res.new_ids)
            self._push({"type": "sync", "phase": "done", "new_count": new,
                        "message": f"{new} new recording(s)" if new else "Up to date",
                        "last_sync": self._s.lib.get_meta("last_sync"),
                        "profile": res.profile.__dict__ if res.profile else None,
                        "errors": res.errors, **q})
            # then each course's weekly materials, announcements, outcomes (listed, nothing downloaded)
            self._push({"type": "content", "phase": "start", **q})
            content = self._s.sync.sync_content()
            self._push({"type": "courses", "courses": self._course_views()})
            self._push({"type": "content", "phase": "done", "new_materials": len(content["new_materials"]),
                        "new_announcements": len(content["new_announcements"]), "errors": content["errors"], **q})
            return True
        except (AuthError, NeedLogin) as exc:
            self._push({"type": "sync", "phase": "error", "need_login": True, "message": str(exc), **q})
        except Exception as exc:  # noqa: BLE001
            log.log(logging.WARNING if quiet else logging.ERROR, "sync failed", exc_info=True)
            self._push({"type": "sync", "phase": "error", "message": str(exc) or "Sync failed", **q})
        finally:
            self._sync_lock.release()
        return False

    @staticmethod
    def _own_new(course_views: list[dict[str, Any]], new_ids: list[str]) -> int:
        """New recordings worth telling you about: not other sections' lectures in a course where you follow yours."""
        new = set(new_ids)
        n = 0
        for c in course_views:
            mine_only = c.get("show_mode") == "mine" and len(c.get("available_sections") or []) > 1
            for d in c.get("days", []):
                n += sum(1 for r in d["recordings"]
                         if r["id"] in new and not r["duplicate"] and not (mine_only and r.get("mine") is False))
        return n

    # ------------------------------------------------------------------ full sync / course sync
    def _gpu(self) -> bool:
        if self._s.settings.device == "cpu":
            return False
        try:
            return bool((json.loads(self._s.lib.get_meta("system_info") or "{}").get("gpu") or {}).get("available"))
        except ValueError:
            return False

    @safe
    def catch_up_estimate(self, course_id: Optional[str] = None) -> dict[str, Any]:
        """What a full sync (or one course's sync) would queue, from what is known now."""
        from .catchup import course_needs, summary

        needs = course_needs(self._s.lib, self._s.settings, [course_id] if course_id else None,
                             self._s.jobs.active_ids())
        return {"ok": True, **summary(needs, self._gpu(), self._s.settings.model)}

    @safe
    def catch_up(self, course_ids: Optional[list[str]] = None, materials: bool = True) -> dict[str, Any]:
        """Sync, then queue every lecture of your sections that is not transcribed yet (newest first) and, if asked,
        download the materials. ``course_ids``: None = every course. Claude is not involved."""
        from .catchup import course_needs, queue_order

        ids = [str(c) for c in course_ids] if course_ids else None
        scope = "all" if ids is None else (ids[0] if len(ids) == 1 else "some")

        def run() -> None:
            self._push({"type": "catchup", "phase": "sync", "scope": scope})
            if self._sync_lock.acquire(blocking=False):
                ok = self._run_sync()
            else:                                  # a sync is running already: use its result
                with self._sync_lock:
                    ok = True
            if not ok:
                self._push({"type": "catchup", "phase": "error", "scope": scope,
                            "message": "The sync failed, so nothing was queued."})
                return
            try:
                needs = course_needs(self._s.lib, self._s.settings, ids, self._s.jobs.active_ids())
                order = queue_order(needs)
                queued = self._s.jobs.enqueue(order) if order else 0
                mats = [m for n in needs for m in n.materials] if materials else []
                if mats:
                    self.materials_download(mats)
                self._courses_dirty.set()
                self._push({"type": "queue", "jobs": self._s.jobs.snapshot()})
                self._push({"type": "catchup", "phase": "done", "scope": scope, "lectures": queued,
                            "materials": len(mats), "audio_s": round(sum(n.audio_s for n in needs)),
                            "waiting": [n.code for n in needs if n.needs_section and n.waiting]})
            except Exception as exc:  # noqa: BLE001
                log.exception("catch-up failed")
                self._push({"type": "catchup", "phase": "error", "scope": scope, "message": str(exc) or "Failed"})

        threading.Thread(target=run, name="catch-up", daemon=True).start()
        return {"ok": True}

    # ------------------------------------------------------------------ transcription
    @safe
    def transcribe(self, ids: list[str]) -> dict[str, Any]:
        n = self._s.jobs.enqueue([str(i) for i in ids])
        self._courses_dirty.set()
        self._push({"type": "queue", "jobs": self._s.jobs.snapshot()})
        return {"ok": True, "queued": n}

    @safe
    def transcribe_day(self, day_key: str) -> dict[str, Any]:
        _, recs = self._s.day(day_key)
        s = self._s.settings
        ids = [r.delivery_id for r in recs if not r.duplicate_of and not r.hidden and r.status != "done"
               and not (0 < r.duration_s < s.min_duration_s)]
        return self.transcribe(ids)

    @safe
    def cancel(self, job_id: Optional[str] = None) -> dict[str, Any]:
        self._s.jobs.cancel(job_id or None)
        return {"ok": True}

    @safe
    def transcribe_local_file(self) -> dict[str, Any]:
        import webview

        paths = self._window.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=True,
            file_types=("Audio/video (*.mp4;*.m4a;*.mp3;*.wav;*.mkv;*.mov;*.webm;*.aac;*.ogg;*.flac)", "All files (*.*)"))
        for p in paths or []:
            self._s.jobs.enqueue_local(Path(p))
        return {"ok": True, "queued": len(paths or [])}

    # ------------------------------------------------------------------ reading
    @safe
    def get_day(self, day_key: str) -> dict[str, Any]:
        detail = views.day_detail(self._s.lib, self._s.settings, day_key, self._s.day_files(day_key))
        if detail is None:
            return {"ok": False, "error": "This class day no longer exists."}
        notes = detail["files"].get("notes")
        detail["notes_md"] = Path(notes).read_text(encoding="utf-8") if notes else ""
        return {"ok": True, "day": detail}

    @safe
    def library(self) -> dict[str, Any]:
        out = []
        for cv in self._course_views():
            days = [d for d in cv["days"] if d["any_done"]]
            if days:
                out.append({"course_id": cv["id"], "code": cv["code"], "name": cv["display_name"], "days": days})
        return {"ok": True, "courses": out}

    @safe
    def search(self, query: str) -> dict[str, Any]:
        hits = self._s.lib.search(query, limit=60)
        out = []
        cache: dict[str, Any] = {}
        for h in hits:
            rec = cache.get(h["delivery_id"]) or self._s.lib.get_recording(h["delivery_id"])
            if rec is None:
                continue
            cache[h["delivery_id"]] = rec
            course = self._s.lib.get_course(rec.course_id)
            t = float(h["t"])
            out.append({"day_key": rec.day_key, "delivery_id": rec.delivery_id, "t": t,
                        "ts": f"{int(t // 60):02d}:{int(t % 60):02d}", "snippet": h["snippet"],
                        "course_code": course.code if course else "", "date": rec.start.date().isoformat(),
                        "time": f"{rec.start:%H:%M}", "viewer_url": rec.viewer_url})
        mats = []
        for h in self._s.lib.search_materials(query, limit=30):
            m = self._s.lib.get_material(h["resource_id"])
            if not m:
                continue
            course = self._s.lib.get_course(m["course_id"])
            mats.append({"resource_id": m["resource_id"], "title": m["title"], "page": h["page"], "snippet": h["snippet"],
                         "course_id": m["course_id"], "course_code": course.code if course else ""})
        return {"ok": True, "results": out, "materials": mats}

    # ------------------------------------------------------------------ course content & materials
    @safe
    def course_detail(self, course_id: str) -> dict[str, Any]:
        detail = views.course_detail(self._s.lib, self._s.settings, course_id, self._s.jobs.snapshot())
        if detail is None:
            return {"ok": False, "error": "This course is no longer in your list."}
        return {"ok": True, "course": detail}

    @safe
    def materials_download(self, resource_ids: list[str]) -> dict[str, Any]:
        """Download materials (and index their text) one after another in the background."""
        ids = [str(i) for i in resource_ids or []]
        with self._mat_lock:
            fresh = [i for i in ids if i not in self._mat_queue]
            self._mat_queue.extend(fresh)
            start = not self._mat_running
            self._mat_running = True
        for i in fresh:
            self._push({"type": "material", "id": i, "state": "queued"})
        if start:
            threading.Thread(target=self._material_worker, name="materials", daemon=True).start()
        return {"ok": True, "queued": len(fresh)}

    def _material_worker(self) -> None:
        session = self._s.sync.worker_session()
        while True:
            with self._mat_lock:
                if not self._mat_queue:
                    self._mat_running = False
                    break
                rid = self._mat_queue.pop(0)
            last = [0.0]

            def progress(done: int, total: int, rid: str = rid) -> None:
                now = time.monotonic()
                if now - last[0] > 0.15:
                    last[0] = now
                    self._push({"type": "material", "id": rid, "state": "downloading", "done": done, "total": total})

            try:
                self._push({"type": "material", "id": rid, "state": "downloading", "done": 0, "total": 0})
                path = self._s.materials.download(rid, session, progress)
                pages = self._s.materials.index_text(rid)
                self._push({"type": "material", "id": rid, "state": "done", "path": str(path), "pages": pages})
            except NeedLogin:
                try:
                    self._s.sync.ensure_login()
                    session = self._s.sync.worker_session()
                    with self._mat_lock:
                        self._mat_queue.insert(0, rid)
                    continue
                except Exception as exc:  # noqa: BLE001
                    self._push({"type": "material", "id": rid, "state": "error", "error": str(exc)})
            except Exception as exc:  # noqa: BLE001 - reported per file
                log.warning("Material %s failed: %s", rid, exc)
                self._push({"type": "material", "id": rid, "state": "error", "error": str(exc) or "Download failed"})
        self._s.sync.absorb(session)
        self._courses_dirty.set()
        self._push({"type": "material", "id": "", "state": "idle"})

    @safe
    def material_open(self, resource_id: str, reveal_only: bool = False) -> dict[str, Any]:
        m = self._s.lib.get_material(resource_id)
        if not m or not m.get("local_path") or not Path(m["local_path"]).is_file():
            return {"ok": False, "error": "Download it first."}
        (reveal if reveal_only else open_path)(m["local_path"])
        return {"ok": True}

    @safe
    def course_mark_seen(self, course_id: str, what: str = "all") -> dict[str, Any]:
        if what in ("all", "materials"):
            self._s.lib.clear_new_materials(course_id or None)
        if what in ("all", "announcements"):
            self._s.lib.clear_new_announcements(course_id or None)
        return {"ok": True, "courses": self._course_views()}

    # ------------------------------------------------------------------ files & links
    @safe
    def open_file(self, day_key: str, what: str = "md") -> dict[str, Any]:
        files = self._s.day_files(day_key)
        target = files.get(what) or files.get("folder")
        if not target:
            return {"ok": False, "error": "File not found."}
        (reveal if what in ("zip", "pdf_folder") else open_path)(target)
        return {"ok": True}

    @safe
    def open_output(self) -> dict[str, Any]:
        open_path(self._s.output_root)
        return {"ok": True}

    @safe
    def open_course_folder(self, course_id: str) -> dict[str, Any]:
        from .outputs.writer import course_dir

        course = self._s.lib.get_course(course_id)
        folder = course_dir(self._s.output_root, course, self._s.lib.courses(True)) if course else self._s.output_root
        open_path(folder if folder.exists() else self._s.output_root)
        return {"ok": True}

    @safe
    def export_calendar(self) -> dict[str, Any]:
        """The announced quizzes and exams of every course as an .ics file in the output folder, shown in Explorer."""
        from .calendar_export import assessment_events, ics

        events = assessment_events(self._s.lib, self._s.settings)
        if not events:
            return {"ok": False, "error": "No quiz or exam with a known week has been announced yet."}
        path = self._s.output_root / "IEUStudia - quizzes and exams.ics"
        path.write_bytes(ics(events).encode("utf-8"))
        reveal(path)
        return {"ok": True, "count": len(events), "path": str(path)}

    @safe
    def open_output_file(self, path: str) -> dict[str, Any]:
        """Open a file IEUStudia wrote in the output folder (nothing outside it)."""
        p = Path(path or "")
        root = self._s.output_root.resolve()
        if not p.is_file() or not p.resolve().is_relative_to(root):
            return {"ok": False, "error": "File not found."}
        open_path(p)
        return {"ok": True}

    @safe
    def open_logs(self) -> dict[str, Any]:
        from .paths import logs_dir

        open_path(logs_dir())
        return {"ok": True}

    @safe
    def open_url(self, url: str) -> dict[str, Any]:
        if url.startswith(("https://", "http://")):
            webbrowser.open(url)
        return {"ok": True}

    @safe
    def copy_for_ai(self, scope: str, template: str = "notes") -> dict[str, Any]:
        from .outputs.ai_copy import build_ai_text

        if scope.startswith("course:"):
            cid = scope.split(":", 1)[1]
            course = self._s.lib.get_course(cid)
            done = self._s.lib.transcribed_ids()
            recs = [r for r in self._s.lib.recordings(cid) if r.delivery_id in done]
            keys = sorted({r.day_key for r in recs}, key=lambda k: k.split("|")[1])
            days = [([r for r in recs if r.day_key == k], store.load_transcripts([r.delivery_id for r in recs if r.day_key == k]))
                    for k in keys]
        else:
            course, recs = self._s.day(scope)
            days = [(recs, store.load_transcripts([r.delivery_id for r in recs]))]
        text = build_ai_text(template, course, days)
        if not text.strip():
            return {"ok": False, "error": "Nothing transcribed yet."}
        from .ai import text_tokens

        ok = set_clipboard(text)
        return {"ok": ok, "chars": len(text), "tokens": text_tokens(text),
                "error": None if ok else "Could not access the clipboard."}

    # ------------------------------------------------------------------ export / AI
    @safe
    def export_pack(self, day_key: str) -> dict[str, Any]:
        def run() -> None:
            try:
                self._push({"type": "export", "phase": "start", "day_key": day_key})
                out = self._s.export_pack(day_key)
                self._push({"type": "export", "phase": "done", "day_key": day_key,
                            "files": {k: str(v) for k, v in out.items()}})
                reveal(out.get("pdf") or out["zip"])
            except Exception as exc:  # noqa: BLE001
                log.exception("export failed")
                self._push({"type": "export", "phase": "error", "day_key": day_key, "message": str(exc)})

        threading.Thread(target=run, name="export", daemon=True).start()
        return {"ok": True}

    @safe
    def ai_estimate(self, day_key: str, include_slides: bool = False) -> dict[str, Any]:
        from .ai import claude_info, estimate_tokens

        text, images, _, _ = self._s.ai_inputs(day_key, include_slides)
        est = estimate_tokens(text, min(len(images), 60))
        return {"ok": True, "estimate": est, "ai": claude_info(), "model": self._s.settings.ai_model}

    @safe
    def ai_notes(self, day_key: str, template: str = "notes", include_slides: bool = False) -> dict[str, Any]:
        if self._ai_cancel is not None:
            return {"ok": False, "error": "Claude is already working on something."}
        cancel = threading.Event()
        self._ai_cancel = cancel

        def run() -> None:
            try:
                self._push({"type": "ai", "phase": "start", "day_key": day_key, "text": ""})
                path = self._s.ai_notes(day_key, template, include_slides, self._s.settings.ai_model, cancel,
                                        lambda t: self._push({"type": "ai", "phase": "progress", "day_key": day_key,
                                                              "text": t}))
                self._push({"type": "ai", "phase": "done", "day_key": day_key, "path": str(path),
                            "text": path.read_text(encoding="utf-8")})
            except Exception as exc:  # noqa: BLE001
                self._push({"type": "ai", "phase": "error", "day_key": day_key, "message": str(exc)})
            finally:
                self._ai_cancel = None

        threading.Thread(target=run, name="claude", daemon=True).start()
        return {"ok": True}

    @safe
    def ai_cancel(self) -> dict[str, Any]:
        if self._ai_cancel is not None:
            self._ai_cancel.set()
        return {"ok": True}

    # ------------------------------------------------------------------ video study plans
    @safe
    def plan_options(self, course_id: str) -> dict[str, Any]:
        """What the new-plan dialog offers: the weeks (with what is downloaded and transcribed) and the assessments
        the lecturer announced, each with the weeks it most likely covers."""
        from .ai import claude_info

        d = views.course_detail(self._s.lib, self._s.settings, course_id)
        if d is None:
            return {"ok": False, "error": "This course is no longer in your list."}
        weeks = []
        for w in d["weeks"]:
            mats = [m for u in w["units"] for m in u["materials"]]
            own = [x for x in w["days"] if x.get("mine") is not False]       # your section's class days
            weeks.append({"no": w["no"], "start": w["start"], "end": w["end"], "current": w["current"], "past": w["past"],
                          "topic": w["topic_en"] or w["topic_tr"] or " · ".join(u["title"] for u in w["units"]),
                          "exam": w["exam"], "files": len(mats),
                          "files_ready": sum(1 for m in mats if m["downloaded"] and m["text"] == "done"),
                          "missing": [m["id"] for m in mats if not m["downloaded"]],
                          "lectures": len(own), "lectures_done": sum(1 for x in own if x["any_done"]),
                          "todo_days": [x["day_key"] for x in own if not x["transcribed"]]})
        assess, prev = [], 0
        for e in sorted((e for e in (d.get("alerts") or {}).get("plan") or [] if e.get("week")), key=lambda e: e["week"]):
            start = 1 if e["kind"] in ("midterm", "final") else prev + 1     # a quiz: since the last assessment
            assess.append({**e, "weeks": list(range(min(start, e["week"]), e["week"] + 1))})
            prev = e["week"]
        return {"ok": True, "course": {"id": d["id"], "code": d["code"], "name": d["display_name"]}, "weeks": weeks,
                "assessments": assess, "ai": claude_info(),
                "model": self._s.settings.plan_model or self._s.settings.ai_model}

    @safe
    def plan_estimate(self, course_id: str, scope: dict[str, Any]) -> dict[str, Any]:
        from .plans import estimate

        return {"ok": True, "estimate": estimate(self._s.lib, self._s.settings, course_id, scope or {})}

    @safe
    def plan_create(self, course_id: str, scope: dict[str, Any]) -> dict[str, Any]:
        from .ai import claude_info

        info = claude_info()
        if not info["available"]:
            return {"ok": False, "error": "Set up Claude or ChatGPT in Settings → AI first (needed to build the plan)."}
        if info.get("can_plans") is False:
            return {"ok": False, "error": "Study plans need Claude or ChatGPT — the offline model is too small for them."}
        pid = self._s.plans.create(course_id, scope or {})
        self._s.plans.start(pid)
        return {"ok": True, "plan": self._s.lib.plan_brief(pid)}

    @safe
    def plan_list(self, course_id: Optional[str] = None) -> dict[str, Any]:
        return {"ok": True, "plans": self._s.lib.plans(course_id or None)}

    @safe
    def plan_get(self, plan_id: str) -> dict[str, Any]:
        p = self._s.lib.plan(plan_id)
        if p is None:
            return {"ok": False, "error": "This plan no longer exists."}
        data = p.pop("data", {}) or {}
        p["concepts"] = (data.get("concepts") or {}).get("concepts", [])
        p["stages_done"] = [s for s in ("gather", "concepts", "search", "inspect", "verify") if s in data]
        return {"ok": True, "plan": p}

    @safe
    def plan_resume(self, plan_id: str, from_stage: str = "") -> dict[str, Any]:
        self._s.plans.start(plan_id, from_stage)
        return {"ok": True}

    @safe
    def plan_cancel(self, plan_id: str) -> dict[str, Any]:
        self._s.plans.cancel(plan_id)
        return {"ok": True}

    @safe
    def plan_delete(self, plan_id: str) -> dict[str, Any]:
        self._s.plans.cancel(plan_id)
        self._s.lib.delete_plan(plan_id)
        return {"ok": True}

    @safe
    def plan_mark(self, plan_id: str, key: str, done: bool) -> dict[str, Any]:
        p = self._s.lib.plan_brief(plan_id)
        if p is None:
            return {"ok": False, "error": "This plan no longer exists."}
        full = self._s.lib.plan(plan_id) or {}
        marks = dict(full.get("marks") or {})
        if done:
            marks[str(key)] = True
        else:
            marks.pop(str(key), None)
        self._s.lib.update_plan(plan_id, marks=marks)
        return {"ok": True, "marks": marks}

    @safe
    def plan_update(self, plan_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        """A new version of a plan (other weeks, difficulties, time…): one consolidated plan that says what was
        added, replaced or is no longer needed. The old version stays until deleted."""
        old = self._s.lib.plan_brief(plan_id)
        if old is None:
            return {"ok": False, "error": "This plan no longer exists."}
        scope = {**(old.get("scope") or {}), **(patch or {})}
        pid = self._s.plans.create(old["course_id"], scope, parent_id=plan_id)
        self._s.plans.start(pid)
        return {"ok": True, "plan": self._s.lib.plan_brief(pid)}

    @safe
    def plan_open_file(self, plan_id: str) -> dict[str, Any]:
        p = self._s.lib.plan(plan_id) or {}
        f = (p.get("result") or {}).get("file")
        if not f or not Path(f).is_file():
            return {"ok": False, "error": "The plan file was not found."}
        open_path(f)
        return {"ok": True}

    # ------------------------------------------------------------------ weekly digest
    @safe
    def digest(self, offset: int = 0) -> dict[str, Any]:
        from .digest import weekly_digest

        d = weekly_digest(self._s.lib, self._s.settings, int(offset or 0))
        try:
            saved = json.loads(self._s.lib.get_meta(f"digest:{d['week']['start']}") or "{}")
        except ValueError:
            saved = {}
        return {"ok": True, "digest": d, "summary": saved}

    @safe
    def digest_summary(self, offset: int = 0) -> dict[str, Any]:
        """A short written summary of the week by Claude (light model); saved, and written to the output folder."""
        from .ai import claude_info, one_shot
        from .digest import digest_markdown, digest_prompt, weekly_digest

        if not claude_info()["available"]:
            return {"ok": False, "error": "Claude Code is not installed on this PC."}
        if self._ai_cancel is not None:
            return {"ok": False, "error": "Claude is already working on something."}
        cancel = threading.Event()
        self._ai_cancel = cancel
        offset = int(offset or 0)
        model = getattr(self._s.settings, "ai_model_light", "") or "haiku"

        def run() -> None:
            try:
                d = weekly_digest(self._s.lib, self._s.settings, offset)
                self._push({"type": "digest", "phase": "start", "offset": offset, "text": ""})
                res = one_shot("You write brief, accurate study digests for a university student, in Markdown.",
                               [{"type": "text", "text": digest_prompt(d)}], model, "low", cancel,
                               lambda t: self._push({"type": "digest", "phase": "progress", "offset": offset, "text": t}))
                text = (res.get("text") or "").strip()
                saved = {"text": text, "model": model, "at": dt.datetime.now().isoformat(timespec="seconds"),
                         "usage": res.get("usage") or {}}
                folder = self._s.output_root / "Weekly digests"
                folder.mkdir(parents=True, exist_ok=True)
                path = folder / f"{d['week']['start']} weekly digest.md"
                path.write_bytes(digest_markdown(d, text).encode("utf-8"))
                saved["file"] = str(path)
                self._s.lib.set_meta(f"digest:{d['week']['start']}", json.dumps(saved, ensure_ascii=False))
                self._push({"type": "digest", "phase": "done", "offset": offset, **saved})
            except Exception as exc:  # noqa: BLE001
                self._push({"type": "digest", "phase": "error", "offset": offset, "message": str(exc) or "Claude failed"})
            finally:
                self._ai_cancel = None

        threading.Thread(target=run, name="digest", daemon=True).start()
        return {"ok": True}

    # ------------------------------------------------------------------ chat with Claude
    @safe
    def chat_courses(self) -> dict[str, Any]:
        from .ai import usage_limits

        return {"ok": True, "courses": self._s.chat.courses(), "limits": usage_limits()}

    @safe
    def chat_scope(self, course_id: str) -> dict[str, Any]:
        return {"ok": True, **self._s.chat.scope_options(course_id)}

    @safe
    def chat_estimate(self, course_id: str, day_keys: list[str], focus: str = "full",
                      slides: bool = False) -> dict[str, Any]:
        keys = self._s.chat.all_days(course_id) if focus == "course" else list(day_keys or [])
        days = self._s.chat.ordered_days(course_id, keys)
        ctx = self._s.chat.context(course_id, days, focus, bool(slides))
        return {"ok": True, "tokens": ctx["tokens"] if days else 0, "images": len(ctx["images"]),
                "slides_total": ctx["slides_total"], "days": len(days)}

    @safe
    def chat_list(self) -> dict[str, Any]:
        from .ai import usage_limits

        return {"ok": True, "chats": self._s.chat.list(), "limits": usage_limits()}

    @safe
    def chat_get(self, chat_id: str) -> dict[str, Any]:
        return {"ok": True, "chat": self._s.chat.view(chat_id)}

    @safe
    def chat_create(self, course_id: str, day_keys: list[str], focus: str = "full", slides: bool = False,
                    model: Optional[str] = None) -> dict[str, Any]:
        chat = self._s.chat.create(course_id, list(day_keys or []), focus, bool(slides),
                                   model or self._s.settings.ai_model)
        return {"ok": True, "chat": chat}

    @safe
    def chat_send(self, chat_id: str, text: str) -> dict[str, Any]:
        return {"ok": True, **self._s.chat.send(chat_id, text)}

    @safe
    def chat_stop(self, chat_id: str) -> dict[str, Any]:
        self._s.chat.stop(chat_id)
        return {"ok": True}

    @safe
    def chat_update(self, chat_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "chat": self._s.chat.update(chat_id, patch or {})}

    @safe
    def chat_delete(self, chat_id: str) -> dict[str, Any]:
        self._s.chat.delete(chat_id)
        return {"ok": True}

    @safe
    def chat_export(self, chat_id: str) -> dict[str, Any]:
        path = self._s.chat.export(chat_id)
        reveal(path)
        return {"ok": True, "path": str(path)}

    @safe
    def chat_retry(self, chat_id: str, seq: int) -> dict[str, Any]:
        return {"ok": True, **self._s.chat.retry(chat_id, int(seq))}

    @safe
    def log_client_error(self, info: Any) -> dict[str, Any]:
        """A script error in the UI: written to the log so problems can be traced (works before services start)."""
        d = info if isinstance(info, dict) else {"message": info}
        log.warning("UI error (%s, view %s): %s\n%s", str(d.get("kind", ""))[:20], str(d.get("view", ""))[:20],
                    str(d.get("message", ""))[:500], str(d.get("stack", ""))[:2000])
        return {"ok": True}

    @safe
    def copy_text(self, text: str) -> dict[str, Any]:
        ok = set_clipboard(str(text or ""))
        return {"ok": ok, "error": None if ok else "Could not access the clipboard."}

    # ------------------------------------------------------------------ preferences
    @safe
    def set_course_prefs(self, course_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        cur = self._s.settings.prefs(course_id).__dict__.copy()
        cur.update({k: v for k, v in (patch or {}).items() if k in cur})
        self._s.update_settings({"course_prefs": {course_id: cur}})
        return {"ok": True, "courses": self._course_views()}

    @safe
    def set_hidden(self, delivery_id: str, hidden: bool) -> dict[str, Any]:
        self._s.lib.set_hidden(delivery_id, bool(hidden))
        return {"ok": True, "courses": self._course_views()}

    @safe
    def mark_seen(self, course_id: Optional[str] = None) -> dict[str, Any]:
        self._s.lib.clear_new(course_id or None)
        return {"ok": True, "courses": self._course_views()}

    @safe
    def update_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        old_out = self._s.settings.output_dir
        s = self._s.update_settings(patch or {})
        if s.output_dir != old_out:
            threading.Thread(target=self._rewrite_all, args=(old_out,), daemon=True).start()
        if "model" in (patch or {}):
            threading.Thread(target=self._system_info, daemon=True).start()
        return {"ok": True, "settings": s.to_json()}

    def _rewrite_all(self, old_out: str) -> None:
        try:
            copied = self._s.relocate_outputs(Path(old_out), self._s.output_root)
        except Exception:  # noqa: BLE001
            log.exception("copying outputs failed")
            copied = 0
        for c in self._s.lib.courses():
            try:
                self._s.rewrite_course(c.id)
            except Exception:  # noqa: BLE001
                log.exception("rewrite failed for %s", c.id)
        self._toast("Your transcripts were copied to the new folder (the old folder was left as it was)."
                    if copied else "New transcripts will be saved in the new folder.", "success")

    @safe
    def pick_folder(self) -> dict[str, Any]:
        import webview

        res = self._window.create_file_dialog(webview.FOLDER_DIALOG, directory=self._s.settings.output_dir)
        return {"ok": True, "path": (res[0] if isinstance(res, (list, tuple)) else res) if res else None}

    @safe
    def finish_first_run(self) -> dict[str, Any]:
        self._s.update_settings({"first_run_done": True})
        return {"ok": True}
