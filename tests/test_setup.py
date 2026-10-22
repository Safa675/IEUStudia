"""First-start setup and the machine-wide parts: the hardware verdict, the downloads, the app settings, starting
with Windows, the tray helper's notifications and the AI providers (Claude / ChatGPT through Codex / offline)."""

import datetime as dt
import hashlib
import io
import json
import zipfile

import pytest

from studia import ai, appconfig, components, hardware, launch


def _facts(**over):
    f = {"windows": {"name": "Windows 11", "build": 26100, "version": "24H2", "x64": True},
         "cpu": {"name": "CPU", "cores": 16, "avx": True, "avx2": True, "arch": "AMD64"},
         "ram_gb": 16.0, "ram_free_gb": 8.0, "webview2": "130.0",
         "gpu": {"present": True, "name": "RTX 4060", "vram_mb": 8188, "driver": "566.14"},
         "disk": {"data_dir": "C:\\Users\\x\\AppData\\Local\\IEUStudia", "data_free_gb": 120.0}}
    for k, v in over.items():
        f[k] = {**f[k], **v} if isinstance(v, dict) else v
    return f


def test_hardware_verdicts():
    good = hardware.assess(_facts())
    assert good["ok"] and good["gpu"] and good["recommended_model"] == "large-v3-turbo" and good["recommended_llm"] == "qwen3-4b"
    old = hardware.assess(_facts(windows={"build": 10240}))
    assert not old["ok"] and "Windows 10" in old["blockers"][0]
    no_avx = hardware.assess(_facts(cpu={"avx": False, "avx2": False}))
    assert not no_avx["ok"] and any("AVX" in b for b in no_avx["blockers"])
    small = hardware.assess(_facts(ram_gb=3.5))
    assert not small["ok"] and any("4 GB" in b for b in small["blockers"])
    full = hardware.assess(_facts(disk={"data_free_gb": 2.0}), need_gb=3.0)
    assert not full["ok"] and any("disk space" in b for b in full["blockers"])
    old_driver = hardware.assess(_facts(gpu={"driver": "472.12"}))
    assert old_driver["ok"] and not old_driver["gpu"] and any("driver" in w for w in old_driver["warnings"])
    cpu_only = hardware.assess(_facts(gpu={"present": False, "name": "", "vram_mb": 0, "driver": ""}, ram_gb=8.0))
    assert cpu_only["recommended_model"] == "small" and cpu_only["recommended_llm"] == "qwen3-1.7b"
    assert not cpu_only["models"]["large-v3"]["ok"] and cpu_only["models"]["small"]["ok"]
    assert hardware.assess(_facts(gpu={"present": False, "name": "", "vram_mb": 0, "driver": ""}, ram_gb=6.0))["recommended_llm"] == ""
    strong_cpu = hardware.assess(_facts(gpu={"present": False, "name": "", "vram_mb": 0, "driver": ""}, ram_gb=32.0))
    assert strong_cpu["recommended_model"] == "small"           # medium on a processor takes hours per lecture
    assert any("20 minutes to an hour" in w for w in strong_cpu["warnings"])


def test_hardware_check_runs_on_this_pc(tmp_path):
    f = hardware.check(tmp_path)
    assert f["windows"]["build"] > 0 and f["cpu"]["cores"] >= 1 and f["ram_gb"] > 0 and f["disk"]["data_free_gb"] > 0
    assert "summary" in hardware.assess(f)


def test_download_plan(monkeypatch):
    from studia.engine import models

    monkeypatch.setattr(models, "model_is_downloaded", lambda m: m == "small")
    monkeypatch.setattr(components, "cuda_ready", lambda: False)
    monkeypatch.setattr(components, "llm_ready", lambda k: False)
    items = components.plan("large-v3-turbo", "qwen3-1.7b", gpu=True)
    assert [i["id"] for i in items] == ["speech:large-v3-turbo", "cuda", "llm:qwen3-1.7b"]
    assert components.plan("small", "", gpu=False) == []                     # all there already


class _Resp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def test_cuda_download_extracts_only_the_dlls_and_checks_the_hash(monkeypatch, tmp_path):
    def wheel(names):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for n in names:
                z.writestr(n, b"dll-" + n.encode())
        return buf.getvalue()

    blobs = {"cublas.whl": wheel(["nvidia/cublas/bin/cublas64_12.dll", "nvidia/cublas/bin/cublasLt64_12.dll",
                                  "nvidia/cublas/include/cublas.h"]),
             "cudart.whl": wheel(["nvidia/cuda_runtime/bin/cudart64_12.dll"])}

    class Session:
        def get(self, url, timeout=0):
            pkg = url.split("/pypi/")[1].split("/")[0]
            name = "cublas.whl" if "cublas" in pkg else "cudart.whl"
            files = [{"filename": f"{pkg}-12.8.1-py3-none-win_amd64.whl", "url": name, "size": len(blobs[name]),
                      "digests": {"sha256": hashlib.sha256(blobs[name]).hexdigest()}}]
            return _Resp({"releases": {"12.8.1": files, "12.9.0": files, "13.0.0": []}})

    def fake_download(url, dest, session=None, progress=None, cancel=None, parts=1):
        dest.write_bytes(blobs[url])
        progress(len(blobs[url]), len(blobs[url]))

    monkeypatch.setattr(components, "_session", Session)
    monkeypatch.setattr(components.httpdl, "download", fake_download)
    seen = []
    out = components.download_cuda(lambda d, t: seen.append((d, t)))
    assert sorted(p.name for p in out.glob("*.dll")) == ["cublas64_12.dll", "cublasLt64_12.dll", "cudart64_12.dll"]
    assert not (out / "cublas.h").exists() and (out / ".complete").exists() and components.cuda_ready()
    assert seen[-1][0] == seen[-1][1]
    # a damaged download is refused
    blobs["cublas.whl"] = blobs["cublas.whl"][:-3] + b"bad"
    monkeypatch.setattr(components, "_session", lambda: type("S", (), {"get": lambda self, url, timeout=0: _Resp(
        {"releases": {"12.8.1": [{"filename": "x-win_amd64.whl", "url": "cublas.whl", "size": len(blobs["cublas.whl"]),
                                  "digests": {"sha256": "0" * 64}}]}})})())
    with pytest.raises(RuntimeError, match="damaged"):
        components.download_cuda(lambda d, t: None)


def test_app_config_round_trip():
    c = appconfig.load()
    assert not c.onboarded and c.ai_provider == "" and not c.tray
    c = appconfig.update({"ai_provider": "codex", "tray": True, "sync_minutes": 2, "nonsense": 1})
    assert c.ai_provider == "codex" and c.tray and c.sync_minutes == 10                 # clamped
    assert appconfig.update({"ai_provider": "gemini"}).ai_provider == ""             # unknown -> not chosen


def test_start_with_windows_registry(monkeypatch):
    import winreg

    key = r"Software\IEUStudiaTests\Run"
    monkeypatch.setattr(launch, "RUN_KEY", key)
    try:
        assert not launch.startup_enabled()
        launch.set_startup(True)
        assert launch.startup_enabled()
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            cmd = winreg.QueryValueEx(k, launch.RUN_VALUE)[0]
        assert "--agent" in cmd and "--at-login" in cmd and "studia.app" in cmd
        launch.set_startup(False)
        launch.set_startup(False)                                                     # twice is fine
        assert not launch.startup_enabled()
    finally:
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, r"Software\IEUStudiaTests")
        except OSError:
            pass


def test_launch_commands_from_source():
    assert launch.app_command("--agent")[1:] == ["-m", "studia.app", "--agent"]
    sp = launch.splash_command("123", "dark")
    assert sp[1:3] == ["-I", "-S"] and sp[3].endswith("splash.py") and sp[4:] == ["123", "dark"]


def test_helper_notifications(tmp_path):
    from studia.agent import summarize
    from studia.config import CoursePrefs, Settings
    from studia.coursesite import Announcement
    from studia.domain import Course, Recording
    from studia.library import Library

    lib = Library(tmp_path / "lib.db")
    lib.upsert_courses([Course(id="b", title="t", code="EEE 215", name="Circuits", section="0 - 1")])

    def rec(did, name, hour):
        return Recording(delivery_id=did, course_id="b", folder_id="f", folder_path="", name=name,
                         start=dt.datetime(2026, 10, 6, hour), duration_s=3000, viewer_url=f"u/{did}")

    lib.merge_recordings("b", [rec("s1", "EEE 215 Section 1", 9), rec("s3", "EEE 215 Section 3", 13)], first_sync=True)
    lib.merge_announcements("b", [Announcement("a1", "Quiz 2 next week", "Dr", dt.datetime(2026, 10, 7, 9), "Quiz 2 covers LO3."),
                                  Announcement("a2", "Room change", "Dr", dt.datetime(2026, 10, 7, 10), "We meet in M01.")],
                            first_sync=True)
    s = Settings(course_prefs={"b": CoursePrefs(my_section="3")})
    cfg = appconfig.AppConfig(tray=True)
    seen = {}
    title, text, payload = summarize(lib, s, cfg, ["s1", "s3"], ["a1", "a2"], [], seen)
    assert title == "IEUStudia: heads-up" and text.splitlines()[0] == "EEE 215: Quiz · Quiz 2 next week"
    assert "EEE 215: 1 new lecture recording" in text and "Room change" in text and payload == "course:b"
    assert summarize(lib, s, cfg, ["s1", "s3"], ["a1", "a2"], [], seen) is None        # already told
    assert summarize(lib, s, cfg, ["s1"], [], [], {}) is None                          # another section's lecture


class _FakeProc:
    def __init__(self, lines, rc=0):
        self.stdin = io.BytesIO()
        self.stdout = iter([json.dumps(x).encode() + b"\n" for x in lines])
        self.stderr = io.BytesIO(b"")
        self.returncode = rc

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        pass


def test_chatgpt_through_codex(monkeypatch):
    appconfig.update({"ai_provider": "codex", "codex_model": "gpt-x"})
    monkeypatch.setattr(ai, "codex_cli_info", lambda: {"available": True, "path": "codex.exe", "version": "0.1"})
    calls = []

    def popen(cmd, **kw):
        calls.append(cmd)
        return _FakeProc([{"type": "thread.started", "thread_id": "t"},
                          {"type": "item.completed", "item": {"type": "agent_message", "text": "Hello **notes**"}},
                          {"type": "turn.completed", "usage": {"input_tokens": 1200, "cached_input_tokens": 1000, "output_tokens": 30}}])

    monkeypatch.setattr(ai.subprocess, "Popen", popen)
    assert ai.provider() == "codex" and ai.ai_info()["label"] == "ChatGPT"
    r = ai.one_shot("You write notes.", [{"type": "text", "text": "transcript"}], model="opus")
    assert r["text"] == "Hello **notes**" and r["usage"]["input_tokens"] == 1200
    cmd = calls[0]
    for flag in ("exec", "--json", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check"):
        assert flag in cmd
    assert cmd[cmd.index("-s") + 1] == "read-only" and 'model_reasoning_effort="high"' in cmd and cmd[cmd.index("-m") + 1] == "gpt-x"
    s = ai.new_session("ctx", model="sonnet")
    assert s.ask([{"type": "text", "text": "q1"}])["text"] == "Hello **notes**"
    s.ask([{"type": "text", "text": "q2"}])
    assert len(calls) == 3                                                            # one exec per turn

    monkeypatch.setattr(ai.subprocess, "Popen", lambda cmd, **kw: _FakeProc([{"type": "error", "message": "You've hit your usage limit"}]))
    with pytest.raises(RuntimeError, match="usage limit"):
        ai.one_shot("x", [{"type": "text", "text": "y"}])


def test_no_ai_and_offline_choices(monkeypatch):
    appconfig.update({"ai_provider": "none"})
    assert not ai.ai_info()["available"]
    with pytest.raises(RuntimeError, match="turned off"):
        ai.one_shot("x", [{"type": "text", "text": "y"}])
    appconfig.update({"ai_provider": "local"})
    info = ai.ai_info()
    assert info["label"] == "Offline model" and not info["available"] and not info["can_plans"]


def test_offline_model_prompt_and_chunks(monkeypatch):
    from studia import local_llm

    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "Q"}]
    assert local_llm._prompt(msgs, "qwen3").endswith("<|im_start|>assistant\n<think>\n\n</think>\n\n")
    assert local_llm._prompt(msgs, "qwen3-instruct").endswith("<|im_start|>assistant\n")
    monkeypatch.setattr(local_llm, "count_tokens", lambda text, key: len(text.split()))
    parts = local_llm._chunks("\n".join(["a b c d e"] * 10), "k", 12)
    assert len(parts) == 5 and all(len(p.split()) <= 12 for p in parts)


def test_setup_api(monkeypatch):
    from studia import agent
    from studia.api import Api
    from studia.services import Services

    started = []
    monkeypatch.setattr(agent, "ensure_running", lambda: started.append(1) or True)
    monkeypatch.setattr(launch, "set_startup", lambda on: started.append(("startup", on)))
    monkeypatch.setattr(launch, "startup_enabled", lambda: False)
    api = Api(Services())
    st = api.setup_state()
    assert st["ok"] and "verdict" in st and st["catalog"]["speech"] and not st["app"]["onboarded"]
    r = api.app_config({"tray": True, "start_with_windows": True, "ai_provider": "none"})
    assert r["ok"] and r["app"]["tray"] and started and ("startup", True) in started
    plan = api.setup_plan("small", "")
    assert plan["ok"] and "items" in plan
    done = api.finish_setup()
    assert done["ok"] and done["app"]["onboarded"] and done["app"]["terms_accepted"]
    assert api.get_bootstrap()["app"]["onboarded"]
