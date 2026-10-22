"""The offline AI option: a small instruct model (Qwen3, Apache-2.0) run by CTranslate2 — the engine that already runs
the speech model, so nothing else is installed. Good enough for short study notes, a weekly summary and simple
questions about a lecture; study plans and prep packs need ChatGPT or Claude.

Long transcripts are summarised in parts first (the small model keeps a few thousand tokens in view). On an NVIDIA GPU
it writes ~40 words/s; on the processor a few words per second, so notes for a lecture take minutes there.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable, Optional

from .cancel import Cancelled

log = logging.getLogger("studia")
IDLE_UNLOAD_S = 600
_lock = threading.Lock()
_loaded: dict[str, Any] = {}          # key -> {"gen", "tok", "used", "device"}


def _preset(key: str) -> dict[str, Any]:
    from .components import LLM_PRESETS

    return LLM_PRESETS[key]


def _load(key: str) -> dict[str, Any]:
    from .components import llm_dir, llm_ready
    from .engine.models import resolve_device, setup_cuda_dlls

    with _lock:
        hit = _loaded.get(key)
        if hit:
            hit["used"] = time.monotonic()
            return hit
        if not llm_ready(key):
            raise RuntimeError("The offline AI model is not downloaded yet (Settings → AI).")
        setup_cuda_dlls()
        import ctranslate2
        from tokenizers import Tokenizer

        device, _ = resolve_device("auto")
        ctype = "int8_float16" if device == "cuda" else "int8"
        threads = max(1, (os.cpu_count() or 4) // 2)
        started = time.monotonic()
        gen = ctranslate2.Generator(str(llm_dir(key)), device=device, compute_type=ctype,
                                    intra_threads=0 if device == "cuda" else threads)
        tok = Tokenizer.from_file(str(llm_dir(key) / "tokenizer.json"))
        log.info("Offline AI model %s loaded on %s in %.1fs", key, device, time.monotonic() - started)
        _loaded.clear()                      # one model at a time
        _loaded[key] = {"gen": gen, "tok": tok, "used": time.monotonic(), "device": device}
        threading.Thread(target=_idle_unloader, args=(key,), daemon=True).start()
        return _loaded[key]


def _idle_unloader(key: str) -> None:
    while True:
        time.sleep(60)
        with _lock:
            hit = _loaded.get(key)
            if hit is None:
                return
            if time.monotonic() - hit["used"] > IDLE_UNLOAD_S:
                _loaded.pop(key, None)
                log.info("Offline AI model unloaded (idle)")
                return


def device_of(key: str) -> str:
    with _lock:
        hit = _loaded.get(key)
    return hit["device"] if hit else ""


def _prompt(messages: list[dict[str, str]], template: str) -> str:
    """ChatML (Qwen). The hybrid Qwen3 gets an empty think block so it answers directly."""
    out = []
    for m in messages:
        out.append(f"<|im_start|>{m['role']}\n{m['content'].strip()}<|im_end|>\n")
    out.append("<|im_start|>assistant\n")
    if template == "qwen3":
        out.append("<think>\n\n</think>\n\n")
    return "".join(out)


def count_tokens(text: str, key: str) -> int:
    try:
        return len(_load(key)["tok"].encode(text).ids)
    except Exception:  # noqa: BLE001
        return int(len(text) / 3.5)


def generate(messages: list[dict[str, str]], key: str, max_tokens: int = 900,
             on_text: Optional[Callable[[str], None]] = None, cancel: Optional[threading.Event] = None) -> dict[str, Any]:
    m = _load(key)
    tok, gen = m["tok"], m["gen"]
    prompt = _prompt(messages, _preset(key).get("template", "qwen3"))
    tokens = tok.encode(prompt).tokens
    ids: list[int] = []
    started = time.monotonic()
    for step in gen.generate_tokens(tokens, max_length=max_tokens, sampling_temperature=0.3, sampling_topk=20,
                                    end_token=["<|im_end|>", "<|endoftext|>"]):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        ids.append(step.token_id)
        if on_text and len(ids) % 6 == 0:
            on_text(tok.decode(ids))
    with _lock:
        m["used"] = time.monotonic()
    text = tok.decode(ids).replace("<think>", "").replace("</think>", "").strip()
    if on_text:
        on_text(text)
    return {"text": text, "usage": {"input_tokens": len(tokens), "output_tokens": len(ids)},
            "seconds": round(time.monotonic() - started, 1), "model": key}


def budget(key: str) -> int:
    """How many input tokens one request may carry (the processor is slow with long prompts)."""
    with _lock:
        hit = _loaded.get(key)
    on_gpu = (hit or {}).get("device") == "cuda"
    if not hit:
        from .engine.models import resolve_device

        on_gpu = resolve_device("auto")[0] == "cuda"
    return 6000 if on_gpu else 2500


def _chunks(text: str, key: str, size: int) -> list[str]:
    parts, cur, n = [], [], 0
    for para in text.split("\n"):
        t = count_tokens(para, key) + 1
        if cur and n + t > size:
            parts.append("\n".join(cur))
            cur, n = [], 0
        cur.append(para)
        n += t
    if cur:
        parts.append("\n".join(cur))
    return parts


ENGLISH = "Write in English (keep technical terms as they are)."


def one_shot(system_prompt: str, text: str, key: str, cancel: Optional[threading.Event] = None,
             on_text: Optional[Callable[[str], None]] = None, max_tokens: int = 1200) -> dict[str, Any]:
    """One request; a text longer than the model's comfortable size is condensed part by part first."""
    limit = budget(key)
    usage = {"input_tokens": 0, "output_tokens": 0}
    if count_tokens(text, key) > limit:
        notes = []
        parts = _chunks(text, key, limit - 400)
        for i, part in enumerate(parts, 1):
            if on_text:
                on_text(f"*Reading part {i} of {len(parts)}…*")
            r = generate([{"role": "system", "content": "You condense lecture transcripts into dense, factual bullet "
                                                        "notes: every definition, formula, example and announcement "
                                                        "(quizzes, exams, homework). " + ENGLISH},
                          {"role": "user", "content": part}], key, 500, None, cancel)
            notes.append(r["text"])
            for k in usage:
                usage[k] += r["usage"][k]
        text = "Condensed notes of the lecture, in order:\n\n" + "\n\n".join(notes)
    r = generate([{"role": "system", "content": f"{system_prompt}\n{ENGLISH}"}, {"role": "user", "content": text}],
                 key, max_tokens, on_text, cancel)
    for k in usage:
        r["usage"][k] += usage[k]
    return r


class LocalSession:
    """A chat with the offline model: the lecture context is condensed to fit, and earlier turns are kept short."""

    def __init__(self, system_prompt: str, key: str) -> None:
        self.key_name = key
        self.model = key
        self.key: Any = None
        self.system = system_prompt
        self.history: list[dict[str, str]] = []
        self.last_used = time.monotonic()
        self._turn = threading.Lock()
        self._context: Optional[str] = None

    def alive(self) -> bool:
        return True

    def busy(self) -> bool:
        return self._turn.locked()

    def _fit_context(self) -> str:
        if self._context is None:
            limit = budget(self.key_name) - 900
            if count_tokens(self.system, self.key_name) > limit:
                cut = int(len(self.system) * limit / max(1, count_tokens(self.system, self.key_name)))
                self._context = self.system[:cut] + "\n[…the rest of the lectures is left out to fit the offline model]"
            else:
                self._context = self.system
        return self._context

    def ask(self, content: list[dict[str, Any]], on_text: Optional[Callable[[str], None]] = None,
            stop: Optional[threading.Event] = None, timeout_s: float = 900) -> dict[str, Any]:
        question = "\n\n".join(b.get("text", "") for b in content if b.get("type") == "text").strip()
        with self._turn:
            msgs = [{"role": "system", "content": self._fit_context() + "\n" + ENGLISH}]
            msgs += self.history[-4:]
            msgs.append({"role": "user", "content": question})
            try:
                r = generate(msgs, self.key_name, 700, on_text, stop)
                stopped = False
            except Cancelled:
                return {"text": "", "usage": {}, "stopped": True}
            self.history += [{"role": "user", "content": question[-1500:]},
                             {"role": "assistant", "content": r["text"][-1500:]}]
            self.last_used = time.monotonic()
            return {"text": r["text"], "usage": r["usage"], "stopped": stopped}

    def kill(self) -> None:
        pass

    def terminate(self) -> None:
        pass

    def close(self) -> None:
        pass
