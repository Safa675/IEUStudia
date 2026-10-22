"""Chat with Claude: material, one process per conversation, streaming, stop, restarts, errors (fake CLI)."""

import datetime as dt
import json
import sys
import tempfile
import time
import types
from pathlib import Path

import pytest

from studia import ai, chat as chat_mod, store
from studia.chat import ChatService, replay_text
from studia.config import Settings
from studia.domain import Course, Recording, Segment, Transcript
from studia.library import Library
from studia.services import Services
from studia.slides import Slide

FAKE = str(Path(__file__).with_name("fake_claude.py"))
DAY1, DAY2 = "c|2026-10-13|lecture", "c|2026-10-20|lecture"


def wait_for(cond, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))          # Claude work folders stay in the test dir
    monkeypatch.setattr(ai, "_require_claude", lambda: sys.executable)
    monkeypatch.setattr(ai, "_command", lambda model, effort, sp, partial=False: [sys.executable, FAKE, str(sp)])
    with ai._limits_lock:
        ai._limits.clear()
    lib = Library(tmp_path / "lib.db")
    lib.upsert_courses([Course(id="c", title="t", code="MATH 240", name="Probability", instructor="Dr X")])
    recs = [Recording(delivery_id=f"r{i}", course_id="c", folder_id="f", folder_path="", name="n",
                      start=dt.datetime(2026, 10, d, h, 30), duration_s=2400, viewer_url=f"https://v/r{i}")
            for i, (d, h) in enumerate([(13, 8), (13, 9), (20, 8)])]
    lib.merge_recordings("c", recs, first_sync=True)
    for i, r in enumerate(recs):
        t = Transcript(delivery_id=r.delivery_id, segments=[Segment(0, 5, f"Lecture text {i}.", "en", 0.9),
                                                            Segment(9, 12, "This will be on the quiz.", "en", 0.9)],
                       duration_s=2400, speech_s=10, language_seconds={"en": 10}, model="m", device="cpu",
                       engine_version="t", created_at="2026-10-20T10:00:00")
        lib.save_transcript(t, store.save_transcript(t))
        lib.set_status(r.delivery_id, "done")
    events = []
    ns = types.SimpleNamespace(lib=lib, emit=events.append, output_root=tmp_path / "out", settings=Settings())
    ns.day = types.MethodType(Services.day, ns)
    svc = ChatService(ns)
    yield types.SimpleNamespace(svc=svc, lib=lib, events=events, tmp=tmp_path, mp=monkeypatch)
    svc.shutdown()


def final(events, chat_id, seq):
    done = [e for e in events if e.get("type") == "chat" and e["chat_id"] == chat_id and e["seq"] == seq
            and e["state"] != "streaming"]
    return done[-1] if done else None


def ask(env, chat_id, text):
    seq = env.svc.send(chat_id, text)["seq"]
    assert wait_for(lambda: final(env.events, chat_id, seq) is not None), "no answer"
    return final(env.events, chat_id, seq)


def test_material_is_ordered_labelled_and_can_be_highlights_only(env):
    assert env.svc.ordered_days("c", [DAY2, DAY1, DAY2, "c|2026-10-01|lecture"]) == [DAY1, DAY2]
    ctx = env.svc.context("c", [DAY1, DAY2])
    assert "# L1 · MATH 240 – Probability · Lecture · Tuesday, 13 October 2026" in ctx["system"]
    assert "# L2 · MATH 240" in ctx["system"] and "[L2 P1 05:10]" in ctx["system"]
    assert "## Part 2 (09:30–10:10)" in ctx["system"] and "Lecture text 1." in ctx["system"]
    one = env.svc.context("c", [DAY1])
    assert "# MATH 240" in one["system"] and "[P2 14:05]" in one["system"] and "L1" not in one["system"]
    hl = env.svc.context("c", [DAY1, DAY2], focus="highlights")
    assert "⭐ This will be on the quiz." in hl["system"] and "Lecture text" not in hl["system"]
    body = lambda c: c["system"].split("=== LECTURES ===")[1]   # noqa: E731
    assert len(body(hl)) < len(body(ctx))
    opts = env.svc.scope_options("c")
    assert [d["day_key"] for d in opts["days"]] == [DAY2, DAY1]          # newest first
    assert opts["days"][1]["parts"] == 2 and opts["days"][1]["stars"] == 2


def test_turns_stream_reuse_one_process_and_persist(env):
    chat = env.svc.create("c", [DAY1], model="haiku")
    assert chat["title"] == "New chat" and chat["context_tokens"] > 0 and chat["days"][0]["label"] == "Tue 13 Oct"
    e1 = ask(env, chat["id"], "What is on the quiz?")
    assert e1["state"] == "done" and e1["text"] == "Answer 1 (images 0, replay False, lectures 1): What is on the quiz?"
    assert any(e["state"] == "streaming" and e["text"] for e in env.events if e.get("type") == "chat")
    e2 = ask(env, chat["id"], "And the homework?")
    assert e2["text"].startswith("Answer 2 (images 0, replay False")       # same process remembers the chat
    assert e2["usage"]["cache_read_input_tokens"] == 100
    v = env.svc.view(chat["id"])
    assert [(m["role"], m["status"]) for m in v["messages"]] == [("user", "done"), ("assistant", "done")] * 2
    assert v["title"] == "What is on the quiz?" and v["limits"]["five_hour"]["used"] == 0.25
    assert json.loads(env.lib.get_meta("claude_limits"))["seven_day"]["used"] == 0.5
    assert [c["id"] for c in env.svc.list()] == [chat["id"]]


def test_new_process_gets_the_earlier_conversation(env):
    chat = env.svc.create("c", [DAY1, DAY2])
    ask(env, chat["id"], "First question")
    env.svc._live[chat["id"]].terminate()                 # e.g. closed after being idle, or crashed
    e = ask(env, chat["id"], "Second question")
    assert e["text"].startswith("Answer 1 (images 0, replay True, lectures 2)")
    text = replay_text(env.lib.chat_messages(chat["id"])[:2])
    assert "Student: First question" in text and "You: Answer 1" in text


def test_stop_keeps_the_partial_answer_and_the_conversation(env):
    chat = env.svc.create("c", [DAY1])
    seq = env.svc.send(chat["id"], "SLOW please")["seq"]
    assert wait_for(lambda: any(e.get("seq") == seq and e["text"] for e in env.events if e.get("type") == "chat"))
    env.svc.stop(chat["id"])
    assert wait_for(lambda: final(env.events, chat["id"], seq) is not None)
    f = final(env.events, chat["id"], seq)
    assert f["state"] == "stopped" and f["text"].startswith("word") and len(f["text"]) < 1500
    assert env.lib.chat_messages(chat["id"])[1]["status"] == "stopped"
    assert ask(env, chat["id"], "next")["text"].startswith("Answer 2 (images 0, replay False")


def test_stop_before_a_new_process_got_its_first_message(env):
    chat = env.svc.create("c", [DAY1])
    ask(env, chat["id"], "First question")
    env.svc._live[chat["id"]].terminate()                 # next question needs a new process + replay
    original = env.svc._session

    def session_then_stop(c):                             # Stop pressed while the new process starts
        res = original(c)
        env.svc.stop(c["id"])
        return res

    env.mp.setattr(env.svc, "_session", session_then_stop)
    seq = env.svc.send(chat["id"], "Second")["seq"]
    assert wait_for(lambda: final(env.events, chat["id"], seq) is not None)
    assert final(env.events, chat["id"], seq)["state"] == "stopped"
    env.mp.setattr(env.svc, "_session", original)
    assert ask(env, chat["id"], "Third")["text"].startswith("Answer 1 (images 0, replay True")


def test_partial_answer_is_saved_and_delete_stops_it(env):
    env.mp.setattr(chat_mod, "SAVE_EVERY_S", 0.05)
    chat = env.svc.create("c", [DAY1])
    seq = env.svc.send(chat["id"], "SLOW please")["seq"]
    assert wait_for(lambda: env.lib.chat_messages(chat["id"])[1]["text"].startswith("word"))   # saved while streaming
    env.svc.delete(chat["id"])
    assert wait_for(lambda: final(env.events, chat["id"], seq) is not None)
    assert final(env.events, chat["id"], seq)["state"] == "stopped"
    assert env.svc.list() == [] and env.lib.get_chat(chat["id"]) is None
    assert wait_for(lambda: not env.svc._live and not env.svc._running)


def test_errors_are_reported_and_the_chat_recovers(env):
    chat = env.svc.create("c", [DAY1])
    e = ask(env, chat["id"], "CRASH now")
    assert e["state"] == "error" and "boom" in e["error"]
    assert ask(env, chat["id"], "again")["text"].startswith("Answer 1 (images 0, replay True")
    e = ask(env, chat["id"], "LIMIT")
    assert e["state"] == "error" and "usage limit" in e["error"]
    with pytest.raises(RuntimeError, match="at least one"):
        env.svc.create("c", [])
    with pytest.raises(ValueError):
        env.svc.send(chat["id"], "   ")


def test_try_again_replaces_the_failed_answer(env):
    env.mp.setenv("FAKE_CLAUDE_FLAKY_MARKER", str(env.tmp / "flaky-once"))
    chat = env.svc.create("c", [DAY1])
    assert ask(env, chat["id"], "First")["state"] == "done"
    failed = ask(env, chat["id"], "FLAKY question")
    assert failed["state"] == "error"
    with pytest.raises(RuntimeError):
        env.svc.retry(chat["id"], 2)                      # not the last answer
    res = env.svc.retry(chat["id"], failed["seq"])
    assert res["question"] == "FLAKY question" and res["removed_from"] == 3
    assert wait_for(lambda: final(env.events, chat["id"], res["seq"]) and
                    final(env.events, chat["id"], res["seq"])["state"] == "done")
    msgs = env.lib.chat_messages(chat["id"])
    assert [(m["role"], m["status"]) for m in msgs] == [("user", "done"), ("assistant", "done")] * 2
    assert [m["text"] for m in msgs if m["role"] == "user"] == ["First", "FLAKY question"]
    assert "replay True" in msgs[-1]["text"]              # a fresh process got the clean history


def test_limit_messages_are_explained():
    assert ai.friendly_error("You've hit your session limit · resets 10:40am (Europe/Istanbul)") == \
        "You have reached your Claude usage limit — it resets 10:40am (Europe/Istanbul)."
    assert ai.friendly_error("Claude AI usage limit reached|1792568400").startswith("You have reached your Claude usage limit — it resets at ")
    assert ai.friendly_error("Invalid API key · Please run /login").startswith("Claude Code is not signed in")
    assert ai.friendly_error("something else") == "Claude: something else"


def test_too_much_material_is_refused(env):
    env.mp.setattr(chat_mod, "MAX_CONTEXT_TOKENS", 50)
    with pytest.raises(RuntimeError, match="too much"):
        env.svc.create("c", [DAY1, DAY2])


def test_slides_go_with_the_first_message_only(env):
    img = env.tmp / "s1.jpg"
    img.write_bytes(b"jpg")
    store.save_slides("r0", [Slide(1, 0.0, 8.0, "slides/x/s1.jpg", str(img)), Slide(2, 8.0, 20.0, "x", str(img)),
                             Slide(1, 20.0, 30.0, "slides/x/s1.jpg", str(img))])
    env.mp.setattr(ai, "image_block", lambda p: {"type": "image", "source": {"type": "base64",
                                                                               "media_type": "image/jpeg", "data": ""}})
    assert env.svc.context("c", [DAY1], slides=False)["slides_total"] == 2      # known even when not attached
    chat = env.svc.create("c", [DAY1], slides=True)
    assert chat["slides"] and chat["images"] == 2
    assert "(slide 1)" in env.svc.context("c", [DAY1], slides=True)["system"]
    assert ask(env, chat["id"], "q1")["text"].startswith("Answer 1 (images 2")
    assert ask(env, chat["id"], "q2")["text"].startswith("Answer 2 (images 0")


def test_export_rename_and_delete(env):
    chat = env.svc.create("c", [DAY1])
    ask(env, chat["id"], "Summarise")
    assert env.svc.update(chat["id"], {"title": "  Quiz   prep "})["title"] == "Quiz prep"
    path = env.svc.export(chat["id"])
    text = path.read_text(encoding="utf-8")
    assert path.parent.name == "AI chats" and path.name.endswith(f"Quiz prep [{chat['id'][:6]}].md")
    assert "**You:** Summarise" in text and "**Claude:**" in text and "Tuesday, 13 October 2026" in text
    env.svc.delete(chat["id"])
    assert env.svc.list() == [] and env.lib.chat_messages(chat["id"]) == []
