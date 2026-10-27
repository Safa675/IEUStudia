"""Can this PC run IEUStudia, and how well? Windows version, processor, memory, disk and NVIDIA GPU.

``check()`` gathers the facts (fast: registry, Win32 calls and one ``nvidia-smi`` call) and ``assess()`` turns them
into a verdict: ``blockers`` (IEUStudia cannot work here; the first-start screen stops), ``warnings`` (it works,
with limits) and recommendations (speech model, whether the GPU is used, which offline AI model fits).
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from .i18n import num, t

MIN_BUILD = 17763                 # Windows 10 1809: the oldest Windows 10 that WebView2 supports
MIN_RAM_GB = 4.0
MIN_CUDA_DRIVER = 527.0           # CUDA 12 on Windows
WEBVIEW2_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
GB = 1024 ** 3
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class _MemoryStatus(ctypes.Structure):
    _fields_ = [("dwLength", wt.DWORD), ("dwMemoryLoad", wt.DWORD), ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def memory_gb() -> tuple[float, float]:
    """(total, available) physical memory in GB that Windows can use."""
    st = _MemoryStatus()
    st.dwLength = ctypes.sizeof(_MemoryStatus)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
        return 0.0, 0.0
    return st.ullTotalPhys / GB, st.ullAvailPhys / GB


def installed_memory_gb() -> float:
    """The memory built into the PC, as its specs say ("8 GB"); 0 if unknown. Windows can use a little less: the
    firmware and the integrated graphics keep some, so an 8 GB laptop has about 7.7 GB usable."""
    kb = ctypes.c_ulonglong(0)
    try:
        if ctypes.windll.kernel32.GetPhysicallyInstalledSystemMemory(ctypes.byref(kb)):
            return kb.value / (1024 * 1024)
    except (AttributeError, OSError):
        pass
    return 0.0


def eight_gb(facts: dict[str, Any]) -> bool:
    """An 8 GB PC or more (by what is installed, else by what is usable, which is a few hundred MB less)."""
    installed = float(facts.get("ram_installed_gb") or 0)
    return installed >= 7.5 if installed else float(facts.get("ram_gb") or 0) >= 7.0


def enough_memory(facts: dict[str, Any]) -> bool:
    """The 4 GB minimum by what is installed, like :func:`eight_gb` (a 4 GB laptop lets Windows use 3.4-3.8 GB)."""
    installed = float(facts.get("ram_installed_gb") or 0)
    return installed >= MIN_RAM_GB - 0.5 if installed else float(facts.get("ram_gb") or 0) >= MIN_RAM_GB


def _reg(root: int, path: str, name: str) -> str:
    import winreg

    for flags in (winreg.KEY_READ | winreg.KEY_WOW64_64KEY, winreg.KEY_READ | winreg.KEY_WOW64_32KEY):
        try:
            with winreg.OpenKey(root, path, 0, flags) as k:
                return str(winreg.QueryValueEx(k, name)[0])
        except OSError:
            continue
    return ""


def cpu_info() -> dict[str, Any]:
    import winreg

    feature = ctypes.windll.kernel32.IsProcessorFeaturePresent
    name = _reg(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0", "ProcessorNameString")
    return {"name": " ".join(name.split()) or platform.processor(), "cores": os.cpu_count() or 1,
            "avx": bool(feature(39)), "avx2": bool(feature(40)),          # PF_AVX_/PF_AVX2_INSTRUCTIONS_AVAILABLE
            "arch": platform.machine()}


def windows_info() -> dict[str, Any]:
    v = sys.getwindowsversion()
    import winreg

    display = _reg(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion", "DisplayVersion")
    edition = _reg(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion", "ProductName")
    name = "Windows 11" if v.build >= 22000 else ("Windows 10" if v.major == 10 else f"Windows {v.major}.{v.minor}")
    return {"name": name, "build": v.build, "version": display, "edition": edition,
            "x64": platform.machine().upper() in ("AMD64", "X86_64", "ARM64") and sys.maxsize > 2 ** 32}


def webview2_version() -> str:
    import winreg

    for root, path in ((winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_GUID}"),
                       (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_GUID}"),
                       (winreg.HKEY_CURRENT_USER, rf"Software\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_GUID}")):
        v = _reg(root, path, "pv")
        if v and v != "0.0.0.0":
            return v
    return ""


def nvidia_gpu() -> dict[str, Any]:
    """The first NVIDIA GPU according to nvidia-smi (installed with the driver): name, memory, driver version."""
    out: dict[str, Any] = {"present": False, "name": "", "vram_mb": 0, "driver": ""}
    exe = shutil.which("nvidia-smi") or os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "nvidia-smi.exe")
    try:
        r = subprocess.run([exe, "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW)
        first = (r.stdout or "").strip().splitlines()[0]
        name, mem, driver = [x.strip() for x in first.split(",")[:3]]
        out.update(present=True, name=name, vram_mb=int(float(mem)), driver=driver)
    except (OSError, subprocess.SubprocessError, IndexError, ValueError):
        pass
    return out


def _driver_number(driver: str) -> float:
    try:
        major, _, minor = driver.partition(".")
        return float(f"{int(major)}.{int(minor or 0):02d}")
    except ValueError:
        return 0.0


def disk_free_gb(path: Path) -> float:
    p = Path(path)
    while not p.exists() and p.parent != p:
        p = p.parent
    try:
        return shutil.disk_usage(p).free / GB
    except OSError:
        return 0.0


def check(data_dir: Optional[Path] = None, output_dir: Optional[Path] = None) -> dict[str, Any]:
    """The facts about this PC (no judgement yet)."""
    if data_dir is None:
        from .paths import app_dir

        data_dir = app_dir()
    total, avail = memory_gb()
    facts = {"windows": windows_info(), "cpu": cpu_info(), "ram_gb": round(total, 1), "ram_free_gb": round(avail, 1),
             "ram_installed_gb": round(installed_memory_gb(), 1), "gpu": nvidia_gpu(), "webview2": webview2_version(),
             "disk": {"data_dir": str(data_dir), "data_free_gb": round(disk_free_gb(data_dir), 1)}}
    if output_dir is not None:
        facts["disk"].update(output_dir=str(output_dir), output_free_gb=round(disk_free_gb(output_dir), 1))
    return facts


def assess(facts: dict[str, Any], need_gb: float = 0.0) -> dict[str, Any]:
    """Blockers, warnings and recommendations from :func:`check`'s facts. ``need_gb``: what the chosen downloads need."""
    from .engine.models import MODEL_PRESETS

    w, cpu, gpu = facts["windows"], facts["cpu"], facts["gpu"]
    ram = float(facts.get("ram_gb") or 0)
    blockers: list[str] = []
    warnings: list[str] = []
    if not w.get("x64"):
        blockers.append(t("IEUStudia needs 64-bit Windows."))
    if int(w.get("build") or 0) < MIN_BUILD:
        blockers.append(t("IEUStudia needs Windows 10 (version 1809 or newer) or Windows 11."))
    if not cpu.get("avx"):
        blockers.append(t("This processor can't run the speech recognition: it lacks the AVX instructions it needs "
                          "(most processors since about 2011 have them, but some low-cost Celeron and Pentium ones "
                          "don't)."))
    if ram and not enough_memory(facts):
        blockers.append(t("IEUStudia needs at least {min} GB of memory; this PC has {ram} GB.", min=num(MIN_RAM_GB),
                          ram=num(ram, 1)))
    free = float(facts.get("disk", {}).get("data_free_gb") or 0)
    if need_gb and free < need_gb + 1:
        blockers.append(t("Not enough free disk space: the downloads need about {need} GB plus room for your lectures, "
                          "but drive {drive} has {free} GB free.", need=num(need_gb, 1, group=False),
                          drive=Path(facts["disk"]["data_dir"]).anchor or "C:", free=num(free, 1, group=False)))
    if not facts.get("webview2"):
        warnings.append(t("The Microsoft Edge WebView2 Runtime wasn't found. IEUStudia's window needs it (it comes with "
                          "Windows 11 and up-to-date Windows 10)."))
    driver = _driver_number(gpu.get("driver", ""))
    cuda = bool(gpu.get("present")) and driver >= MIN_CUDA_DRIVER and int(gpu.get("vram_mb") or 0) >= 3500
    if gpu.get("present") and not cuda:
        if driver and driver < MIN_CUDA_DRIVER:
            warnings.append(t("Your NVIDIA driver ({driver}) is too old for transcribing on the graphics card. Update it "
                              "from nvidia.com; until then the processor is used.", driver=gpu["driver"]))
        else:
            warnings.append(t("The {name} has too little memory for the speech model; the processor is used.",
                              name=gpu["name"]))
    if not cuda:
        warnings.append(t("Without a suitable NVIDIA graphics card, the processor transcribes: with the small model an "
                          "hour of lecture takes roughly 20 minutes to an hour, depending on the processor."))
    if not cpu.get("avx2") and cpu.get("avx"):
        warnings.append(t("This is an older type of processor (no AVX2), so transcription on it will be slow."))
    eight = eight_gb(facts)
    if ram and not eight:
        warnings.append(t("With less than 8 GB of memory, use the small speech model and close other apps while "
                          "transcribing."))
    vram = int(gpu.get("vram_mb") or 0)
    # On the processor only the small model is practical: measured on a recent laptop CPU, small took 15-30 min
    # per hour of speech, medium more than 4 hours (every passage is decoded in English and in Turkish).
    model = "large-v3-turbo" if cuda else "small"
    llm = ""
    if cuda and vram >= 7000:                    # the 4B model needs the GPU; on a processor it is far too slow
        llm = "qwen3-4b"
    elif eight:
        llm = "qwen3-1.7b"
    models = {}
    for key in MODEL_PRESETS:
        ok = cuda or key in ("small", "medium")
        why = "" if ok else t("needs an NVIDIA graphics card (on the processor it would take hours per lecture)")
        if ok and key == "medium" and not eight:
            ok, why = False, t("needs 8 GB of memory")
        models[key] = {"ok": ok, "why": why}
    return {"ok": not blockers, "blockers": blockers, "warnings": warnings, "gpu": cuda,
            "recommended_model": model, "recommended_llm": llm, "models": models,
            "summary": _summary(facts, cuda)}


def _summary(facts: dict[str, Any], cuda: bool) -> list[dict[str, str]]:
    w, cpu, gpu = facts["windows"], facts["cpu"], facts["gpu"]
    cpu_value = (t("{name} · {n} threads", name=cpu["name"], n=cpu["cores"]) if cpu.get("avx2") else
                 t("{name} · {n} threads · older type, slower", name=cpu["name"], n=cpu["cores"]) if cpu.get("avx") else
                 t("{name} · {n} threads · can't transcribe (no AVX)", name=cpu["name"], n=cpu["cores"]))
    rows = [{"label": "Windows", "value": t("{name} {version} (build {build})", name=w["name"], version=w.get("version") or "",
                                             build=w["build"]).replace("  ", " "),
             "ok": "yes" if w.get("x64") and w["build"] >= MIN_BUILD else "no"},
            {"label": t("Processor"), "value": cpu_value,
             "ok": "yes" if cpu.get("avx2") else ("warn" if cpu.get("avx") else "no")},
            {"label": t("Memory"), "value": t("{gb} GB", gb=num(facts.get("ram_installed_gb") or facts["ram_gb"])),
             "ok": "yes" if eight_gb(facts) else ("warn" if enough_memory(facts) else "no")},
            {"label": t("Graphics"), "value": (t("{name} · {gb} GB · driver {driver}", name=gpu["name"], gb=num(gpu["vram_mb"] / 1024),
                                                 driver=gpu["driver"]) if gpu.get("present")
                                               else t("No NVIDIA graphics card (the processor transcribes)")),
             "ok": "yes" if cuda else "warn"},
            {"label": t("Free disk space"), "value": t("{gb} GB on {drive}", gb=num(facts["disk"]["data_free_gb"], group=False),
                                                       drive=Path(facts["disk"]["data_dir"]).anchor),
             "ok": "yes" if facts["disk"]["data_free_gb"] >= 10 else ("warn" if facts["disk"]["data_free_gb"] >= 4 else "no")}]
    return rows
