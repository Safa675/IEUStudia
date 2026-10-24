"""The offline AI option: a small instruct model (Qwen3, Apache-2.0) run by CTranslate2 — the engine that already runs
the speech model, so nothing else is installed. Good enough for short study notes, a weekly summary and simple
questions about a lecture; study plans and prep packs need ChatGPT or Claude.

Long transcripts are summarised in parts first (the small model keeps a few thousand tokens in view); a chat gets, for
each question, the lecture passages that match it. On an NVIDIA GPU it writes ~40 words/s; on the processor a few words
per second, so notes for a lecture take minutes there.
"""

from __future__ import annotations

import logging
import os
import re
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


CHAT_SYSTEM = ("You answer a university student's questions about their own lectures. Below are the passages of the "
               "lectures that match the question: speech-recognition transcripts, mostly English with Turkish passages "
               "tagged [TR], each paragraph starting with [mm:ss]. Answer briefly from them and cite the moments you use "
               "like [P2 14:05]. If they don't cover the question, say so.")
_STOP = set("""the and for are was were with what which who how why when where does did can could would should will
about there their they you your our this that these those from into than then have has had not but also just
bir ile için gibi daha çok var yok ama şey nasıl neden nedir""".split())


def _keywords(text: str) -> set[str]:
    return {w for w in re.findall(r"[\wçğıöşü]+", text.replace("İ", "i").lower()) if len(w) >= 3 and w not in _STOP}


def lecture_excerpts(lectures: str, question: str, key: str, max_tokens: int) -> str:
    """The paragraphs of the chat's lectures that match the question best, in lecture order under their headings, in
    at most ``max_tokens``. A question that matches nothing gets the emphasised (⭐) moments, then an even spread."""
    blocks: list[tuple[str, str, str]] = []          # (lecture heading, part heading, paragraph)
    day = part = ""
    for line in lectures.splitlines():
        if line.startswith("# "):
            day, part = line, ""
        elif line.startswith("## "):
            part = line
        elif line.strip():
            blocks.append((day, part, line))
    if not blocks:
        return ""
    want = _keywords(question)
    scores = [len(want & _keywords(p)) + (0.5 if "⭐" in p else 0.0) for _, _, p in blocks]
    if max(scores) >= 1:
        ranked = [i for i in sorted(range(len(blocks)), key=lambda i: -scores[i]) if scores[i] > 0]
    else:
        ranked = [i for i, b in enumerate(blocks) if "⭐" in b[2]]
        ranked += [i for i in range(0, len(blocks), max(1, len(blocks) // 40)) if i not in ranked]
    chosen: set[int] = set()
    used = 0
    for i in ranked:
        t = count_tokens(blocks[i][2], key) + 1
        if used + t <= max_tokens:
            chosen.add(i)
            used += t
        if used >= max_tokens - 20:
            break
    out: list[str] = []
    last_day = last_part = None
    for i in sorted(chosen):
        d, p, para = blocks[i]
        if d != last_day:
            out.append(d)
            last_day, last_part = d, None
        if p and p != last_part:
            out.append(p)
            last_part = p
        out.append(para)
    return "\n".join(out)


def _fit(text: str, key: str, max_tokens: int) -> str:
    n = count_tokens(text, key)
    if n <= max_tokens:
        return text
    return text[:max(0, int(len(text) * max_tokens / max(1, n)) - 20)].rsplit("\n", 1)[0] + "\n…"


def _course_essentials(brief: str) -> str:
    """The course line and the announced assessments of a chat's course brief (the rest is too much for the model)."""
    lines = brief.splitlines()
    out = lines[:1]
    for i, line in enumerate(lines):
        if line.startswith("Assessments the lecturer announced"):
            out += [line] + [x for x in lines[i + 1:] if x.startswith("  ")][:10]
            break
    return "\n".join(out)


class LocalSession:
    """A chat with the offline model. Its window holds a few thousand tokens, so instead of the whole lectures it gets
    short instructions, the course's announced assessments and, for each question, the lecture passages that match it
    best; earlier turns are kept short."""

    def __init__(self, system_prompt: str, key: str) -> None:
        self.key_name = key
        self.model = key
        self.key: Any = None
        head, _, self._lectures = system_prompt.partition("\n\n=== LECTURES ===\n\n")
        self._course = _course_essentials(head.partition("\n\n=== COURSE ===\n\n")[2])
        self.history: list[dict[str, str]] = []
        self.last_used = time.monotonic()
        self._turn = threading.Lock()

    def alive(self) -> bool:
        return True

    def busy(self) -> bool:
        return self._turn.locked()

    def seed(self, history: list[dict[str, Any]]) -> None:
        """The conversation so far, from the library (a new session for an earlier chat)."""
        self.history = [{"role": m["role"], "content": (m.get("text") or "")[-1500:]} for m in history
                        if m.get("role") in ("user", "assistant") and (m.get("text") or "").strip()][-4:]

    def _recent(self) -> list[dict[str, str]]:
        """The last exchange, shortened: the model's window is small."""
        return [{"role": m["role"], "content": m["content"][:800]} for m in self.history[-2:]]

    def _system(self, question: str, extra: str) -> str:
        k = self.key_name
        fixed = f"{CHAT_SYSTEM}\n{ENGLISH}" + (f"\n\n=== COURSE ===\n{self._course}" if self._course else "")
        used = count_tokens(fixed, k) + sum(count_tokens(m["content"], k) for m in self._recent())
        room = max(300, budget(k) - used - count_tokens(question, k) - 50)
        recent = " ".join(m["content"] for m in self.history[-2:] if m["role"] == "user")    # follow-up questions
        lectures = lecture_excerpts(self._lectures, f"{question} {recent}", k, int(room * (0.75 if extra else 1.0)))
        more = _fit(extra, k, room - count_tokens(lectures, k)) if extra else ""
        return (fixed + (f"\n\n=== LECTURE PASSAGES ===\n{lectures}" if lectures else "")
                + (f"\n\n=== OTHER MATCHING PASSAGES ===\n{more}" if more.strip() else ""))

    def ask(self, content: list[dict[str, Any]], on_text: Optional[Callable[[str], None]] = None,
            stop: Optional[threading.Event] = None, timeout_s: float = 900) -> dict[str, Any]:
        texts = [b["text"] for b in content if b.get("type") == "text" and (b.get("text") or "").strip()]
        question = texts[-1].strip() if texts else ""
        extra = "\n\n".join(texts[:-1])           # matching passages of course files and of other lectures
        with self._turn:
            msgs = [{"role": "system", "content": self._system(question, extra)}]
            msgs += self._recent()
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
