"""What IEUStudia downloads on first start, and what it takes on disk.

- the speech model (``engine.models.MODEL_PRESETS``, from Hugging Face),
- on PCs with an NVIDIA GPU: the CUDA runtime libraries the GPU needs (cuBLAS, from NVIDIA's official Python wheels
  on PyPI; only the DLLs are kept, in ``<app data>\\cuda``),
- optionally a small offline AI model (``LLM_PRESETS``) for basic notes and questions without a subscription.

All downloads resume after an interruption and are checked (sizes from Hugging Face, SHA-256 from PyPI).
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
import re
import shutil
import threading
import zipfile
from pathlib import Path
from typing import Any, Callable, Optional

import requests

from . import httpdl
from .cancel import check

log = logging.getLogger("studia")
MB = 1024 * 1024
Progress = Callable[[int, int], None]

# Apache-2.0 models, converted to CTranslate2 int8 (runs with the speech engine already installed).
LLM_PRESETS: dict[str, dict[str, Any]] = {
    "qwen3-1.7b": {"repo": "jncraton/Qwen3-1.7B-ct2-int8", "label": "Basic", "size_mb": 1745,
                   "description": "Short notes and simple questions; runs on most PCs with 8 GB of memory (Qwen3 1.7B).",
                   "template": "qwen3"},
    "qwen3-4b": {"repo": "jncraton/Qwen3-4B-Instruct-2507-ct2-int8", "label": "Better", "size_mb": 4045,
                 "description": "Clearly better notes and answers; needs an NVIDIA graphics card with 8 GB of memory (Qwen3 4B).",
                 "template": "qwen3-instruct"},
}
LLM_FILES = ["config.json", "generation_config.json", "model.bin", "tokenizer.json", "tokenizer_config.json",
             "vocabulary.*", "vocab.json"]
CUDA_WHEELS = [("nvidia-cublas-cu12", "12.8.", "nvidia/cublas/bin/"),
               ("nvidia-cuda-runtime-cu12", "12.8.", "nvidia/cuda_runtime/bin/")]
CUDA_DLLS = ("cublas64_12.dll", "cublasLt64_12.dll")
CUDA_SIZE_MB = 560                # download (the cuBLAS wheel); about 780 MB once unpacked


def _session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = "IEUStudia (+https://github.com/mirzaisi/IEUStudia)"
    return s


# ----------------------------------------------------------------------------- locations
def app_root() -> Path:
    from .paths import app_dir

    return app_dir()


def cuda_dir() -> Path:
    return app_root() / "cuda"


def llm_dir(key: str) -> Path:
    return app_root() / "llm" / key


def _dir_size(p: Path) -> int:
    total = 0
    for root, _, files in os.walk(p):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


# ----------------------------------------------------------------------------- CUDA runtime (cuBLAS)
def cuda_sources() -> list[str]:
    """Folders that already provide cuBLAS for CUDA 12 (ours, the CUDA toolkit, or pip ``nvidia-*`` wheels)."""
    found = []
    ours = cuda_dir()
    if (ours / ".complete").is_file() and all((ours / d).is_file() for d in CUDA_DLLS):   # not a download cut short
        found.append(str(ours))
    for key, val in os.environ.items():
        if key.upper().startswith("CUDA_PATH") and val:
            b = Path(val) / "bin"
            if all((b / d).is_file() for d in CUDA_DLLS):
                found.append(str(b))
    try:
        import nvidia  # type: ignore

        for base in nvidia.__path__:
            b = Path(base) / "cublas" / "bin"
            if all((b / d).is_file() for d in CUDA_DLLS):
                found.append(str(b))
    except ImportError:
        pass
    return found


def cuda_ready() -> bool:
    return bool(cuda_sources())


def _pypi_wheel(package: str, prefix: str, session: requests.Session) -> dict[str, Any]:
    """The newest ``win_amd64`` wheel of ``package`` whose version starts with ``prefix``: url, size, sha256."""
    r = session.get(f"https://pypi.org/pypi/{package}/json", timeout=30)
    r.raise_for_status()
    releases = r.json().get("releases", {})

    def key(v: str) -> list[int]:
        return [int(x) for x in re.findall(r"\d+", v)]

    for version in sorted((v for v in releases if v.startswith(prefix)), key=key, reverse=True):
        for f in releases[version]:
            if f["filename"].endswith("win_amd64.whl") and not f.get("yanked"):
                return {"version": version, "url": f["url"], "size": int(f["size"]), "sha256": f["digests"]["sha256"],
                        "filename": f["filename"]}
    raise RuntimeError(f"No Windows build of {package} {prefix}x was found on PyPI.")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(4 * MB), b""):
            h.update(chunk)
    return h.hexdigest()


def download_cuda(progress: Progress, cancel: Optional[threading.Event] = None) -> Path:
    """cuBLAS + the CUDA runtime DLLs from NVIDIA's wheels on PyPI into ``<app data>\\cuda`` (resumable, verified)."""
    session = _session()
    wheels = [(_pypi_wheel(pkg, prefix, session), member) for pkg, prefix, member in CUDA_WHEELS]
    total = sum(w["size"] for w, _ in wheels)
    target = cuda_dir()
    target.mkdir(parents=True, exist_ok=True)
    tmp = target / "download"
    tmp.mkdir(exist_ok=True)
    done = 0
    for w, member in wheels:
        check(cancel)
        dest = tmp / w["filename"]
        base = done

        def cb(d: int, n: int, base: int = base) -> None:
            progress(base + d, total)

        if not (dest.is_file() and dest.stat().st_size == w["size"]):
            httpdl.download(w["url"], dest, session=session, progress=cb, cancel=cancel, parts=8)
        if _sha256(dest) != w["sha256"]:
            dest.unlink(missing_ok=True)
            raise RuntimeError(f"The download of {w['filename']} was damaged. Try again.")
        with zipfile.ZipFile(dest) as z:
            for info in z.infolist():
                if info.filename.startswith(member) and info.filename.lower().endswith(".dll"):
                    out = target / Path(info.filename).name
                    part = out.with_name(out.name + ".part")
                    with z.open(info) as src, open(part, "wb") as dst:
                        shutil.copyfileobj(src, dst, 4 * MB)
                    os.replace(part, out)             # never a half-written DLL under its real name
        dest.unlink(missing_ok=True)
        done += w["size"]
        progress(done, total)
    shutil.rmtree(tmp, ignore_errors=True)
    (target / ".complete").write_text(json.dumps({w["filename"]: w["version"] for w, _ in wheels}), encoding="utf-8")
    from .engine.models import reset_gpu_cache

    reset_gpu_cache()
    return target


# ----------------------------------------------------------------------------- offline AI models
def llm_ready(key: str) -> bool:
    d = llm_dir(key)
    return (d / ".complete").is_file() and (d / "model.bin").is_file() and (d / "tokenizer.json").is_file()


def download_hf(repo: str, target: Path, patterns: list[str], progress: Progress,
                cancel: Optional[threading.Event] = None, fallback_mb: int = 0) -> Path:
    """The files of a Hugging Face repo matching ``patterns`` into ``target`` (resumable)."""
    session = _session()
    target.mkdir(parents=True, exist_ok=True)
    info = session.get(f"https://huggingface.co/api/models/{repo}", params={"blobs": "true"}, timeout=30)
    info.raise_for_status()
    files = [(s["rfilename"], int(s.get("size") or 0)) for s in info.json().get("siblings", [])
             if any(fnmatch.fnmatch(s["rfilename"], p) for p in patterns)]
    if not any(name == "model.bin" for name, _ in files):
        raise RuntimeError(f"The model files of {repo} were not found on Hugging Face.")
    total = sum(size for _, size in files) or fallback_mb * MB
    done = 0
    for name, size in sorted(files, key=lambda f: f[1]):
        check(cancel)
        base = done

        def cb(d: int, n: int, base: int = base) -> None:
            progress(base + d, total)

        dest = target / name
        if not (size and dest.is_file() and dest.stat().st_size == size):
            httpdl.download(f"https://huggingface.co/{repo}/resolve/main/{name}", dest, session=session,
                            progress=cb, cancel=cancel, parts=8)
        done += size or dest.stat().st_size
    (target / ".complete").write_text(json.dumps({"repo": repo}), encoding="utf-8")
    progress(total, total)
    return target


def download_llm(key: str, progress: Progress, cancel: Optional[threading.Event] = None) -> Path:
    p = LLM_PRESETS[key]
    return download_hf(p["repo"], llm_dir(key), LLM_FILES, progress, cancel, p["size_mb"])


def delete_llm(key: str) -> None:
    shutil.rmtree(llm_dir(key), ignore_errors=True)


# ----------------------------------------------------------------------------- the whole picture
def catalog(facts: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Every component with its size and whether it is on this PC (for the first-start screen and Settings)."""
    from .engine.models import MODEL_PRESETS, model_is_downloaded

    gpu = bool(facts and facts.get("gpu", {}).get("present"))
    return {
        "speech": [{"id": k, "label": v["label"], "description": v["description"], "size_mb": v["size_mb"],
                    "ready": model_is_downloaded(k)} for k, v in MODEL_PRESETS.items()],
        "cuda": {"needed": gpu, "ready": cuda_ready(), "size_mb": CUDA_SIZE_MB, "disk_mb": 780},
        "llm": [{"id": k, "label": v["label"], "description": v["description"], "size_mb": v["size_mb"],
                 "ready": llm_ready(k)} for k, v in LLM_PRESETS.items()],
    }


def plan(speech: str, llm: str = "", gpu: bool = False) -> list[dict[str, Any]]:
    """The downloads a choice needs (only what is missing): [{id, label, size_mb}]."""
    from .engine.models import MODEL_PRESETS, model_is_downloaded

    out = []
    if speech and not model_is_downloaded(speech):
        out.append({"id": f"speech:{speech}", "label": f"Speech model ({MODEL_PRESETS[speech]['label']})",
                    "size_mb": MODEL_PRESETS[speech]["size_mb"]})
    if gpu and not cuda_ready():
        out.append({"id": "cuda", "label": "NVIDIA graphics card support (from NVIDIA)", "size_mb": CUDA_SIZE_MB})
    if llm and not llm_ready(llm):
        out.append({"id": f"llm:{llm}", "label": f"Offline AI model ({LLM_PRESETS[llm]['label']})",
                    "size_mb": LLM_PRESETS[llm]["size_mb"]})
    return out


def download(component: str, progress: Progress, cancel: Optional[threading.Event] = None) -> None:
    """One component of :func:`plan` by its id."""
    from .engine.models import download_model

    kind, _, key = component.partition(":")
    if kind == "speech":
        download_model(key, progress, cancel)
    elif kind == "cuda":
        download_cuda(progress, cancel)
    elif kind == "llm":
        download_llm(key, progress, cancel)
    else:
        raise ValueError(f"Unknown component {component!r}")


def disk_usage(output_dir: Optional[Path] = None) -> dict[str, int]:
    """Bytes used by IEUStudia: speech models, GPU libraries, offline AI, library and transcripts, caches, outputs."""
    root = app_root()
    out = {"speech_models": _dir_size(root / "models"), "gpu_libraries": _dir_size(root / "cuda"),
           "offline_ai": _dir_size(root / "llm"), "cache": _dir_size(root / "cache") + _dir_size(root / "webview"),
           "library": 0}
    for f in root.glob("library.db*"):
        out["library"] += f.stat().st_size
    out["library"] += _dir_size(root / "transcripts") + _dir_size(root / "profiles")
    if output_dir is not None:
        out["your_files"] = _dir_size(Path(output_dir))
    return out
