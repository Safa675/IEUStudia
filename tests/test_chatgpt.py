"""'Continue with ChatGPT' against a fake OpenAI: the browser round trip to the loopback callback, the code exchange,
the ID token checks (signature, issuer, audience, nonce), the encrypted sign-in, refreshing, streaming answers and
their errors, signing out. No network: requests are answered by a fake session; the "browser" is a local GET."""

import base64
import hashlib
import json
import random
import threading
import time
import urllib.parse
import urllib.request

import pytest

from studia import ai, ai_setup, chatgpt


# ----------------------------------------------------------------------------- a small RSA key for signing ID tokens
def _is_prime(n, rng):
    if n < 4:
        return n in (2, 3)
    for p in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
        if n % p == 0:
            return n == p
    d, r = n - 1, 0
    while d % 2 == 0:
        d, r = d // 2, r + 1
    for _ in range(20):
        x = pow(rng.randrange(2, n - 1), d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _keypair(bits=1024, seed=7):
    rng = random.Random(seed)
    e = 65537

    def prime():
        while True:
            p = rng.getrandbits(bits // 2) | (1 << (bits // 2 - 1)) | 1
            if _is_prime(p, rng) and (p - 1) % e:
                return p

    p, q = prime(), prime()
    n = p * q
    return n, e, pow(e, -1, (p - 1) * (q - 1))


N, E, D = _keypair()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _int_b64(i: int) -> str:
    return _b64(i.to_bytes((i.bit_length() + 7) // 8, "big"))


def _sign(claims: dict, kid="k1", d=D) -> str:
    head = _b64(json.dumps({"alg": "RS256", "kid": kid}).encode())
    body = _b64(json.dumps(claims).encode())
    k = (N.bit_length() + 7) // 8
    digest_info = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(f"{head}.{body}".encode()).digest()
    em = b"\x00\x01" + b"\xff" * (k - len(digest_info) - 3) + b"\x00" + digest_info
    sig = pow(int.from_bytes(em, "big"), d, N).to_bytes(k, "big")
    return f"{head}.{body}.{_b64(sig)}"


# ----------------------------------------------------------------------------- a fake OpenAI
class _Resp:
    def __init__(self, status=200, body=None, lines=None):
        self.status_code, self._body, self._lines = status, body if body is not None else {}, lines or []
        self.headers = {}
        self.closed = False

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def iter_lines(self):
        yield from self._lines

    def close(self):
        self.closed = True


def _sse(*events):
    out = []
    for ev in events:
        out += [f"event: {ev['type']}".encode(), b"data: " + json.dumps(ev).encode(), b""]
    return out


class FakeOpenAI:
    def __init__(self):
        self.calls = []
        self.issued = "oaiapp_123"
        self.nonce = ""
        self.scope = "chatgpt.tokens.use.direct email offline_access openid profile resource.invoke"
        self.claims = {}
        self.refresh_error = None
        self.stream_answers = []          # (status, body or SSE lines)

    def get(self, url, timeout=0, headers=None, **kw):
        self.calls.append(("GET", url, None))
        if url == chatgpt.CONFIG_URL:
            return _Resp(body={"jwks_uri": "https://auth.openai.com/jwks", "revocation_endpoint": "https://auth.openai.com/revoke"})
        if url.endswith("/jwks"):
            return _Resp(body={"keys": [{"kty": "RSA", "kid": "k1", "n": _int_b64(N), "e": _int_b64(E), "alg": "RS256"}]})
        if url == f"{chatgpt.API}/models":
            return _Resp(body={"models": [{"slug": "gpt-6.1-sol", "display_name": "GPT-6.1", "visibility": "list"},
                                          {"slug": "hidden", "visibility": "hide"}]})
        raise AssertionError(url)

    def post(self, url, data=None, json=None, headers=None, stream=False, timeout=0, **kw):
        self.calls.append(("POST", url, data if data is not None else json))
        if url == chatgpt.TOKEN_URL and data["grant_type"] == "authorization_code":
            claims = {"iss": chatgpt.AUTH, "aud": self.issued, "sub": "user-1", "email": "student@example.com",
                      "exp": time.time() + 3600, "nonce": self.nonce, **self.claims}
            return _Resp(body={"access_token": "at-1", "refresh_token": "rt-1", "id_token": _sign(claims),
                               "token_type": "Bearer", "expires_in": 3600, "scope": self.scope})
        if url == chatgpt.TOKEN_URL and data["grant_type"] == "refresh_token":
            if self.refresh_error:
                return _Resp(400, {"error": self.refresh_error})
            return _Resp(body={"access_token": "at-2", "refresh_token": "rt-2", "expires_in": 3600, "scope": self.scope})
        if url.endswith("/revoke"):
            return _Resp(200, {})
        if url == f"{chatgpt.API}/responses":
            status, payload = self.stream_answers.pop(0)
            if isinstance(payload, _Resp):
                return payload
            return _Resp(status, payload if isinstance(payload, dict) else {}, payload if isinstance(payload, list) else None)
        raise AssertionError(url)


@pytest.fixture
def openai(monkeypatch):
    fake = FakeOpenAI()
    monkeypatch.setattr(chatgpt, "_session", lambda: fake)
    chatgpt._jwks.clear()
    chatgpt._attempt.clear()
    yield fake
    chatgpt.cancel_sign_in()


def _browser(fake, params=None, *, wrong_state=False):
    """A browser that signs in at once and follows the redirect to IEUStudia's loopback callback."""
    def open_url(url):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        fake.nonce, fake.authorize = q["nonce"], q

        def go():
            back = {"code": "c0de", "state": "nope" if wrong_state else q["state"], "client_id": fake.issued, **(params or {})}
            try:
                urllib.request.urlopen(f"{q['redirect_uri']}?{urllib.parse.urlencode(back)}", timeout=5).read()
            except Exception:  # noqa: BLE001 - a 400 for a wrong state
                pass
        threading.Thread(target=go, daemon=True).start()
        return True
    return open_url


def _sign_in(fake, **kw):
    events, done = [], threading.Event()
    chatgpt.start_sign_in(lambda e: (events.append(e), done.set()), open_browser=_browser(fake, **kw.pop("browser", {})), **kw)
    assert done.wait(10), "no outcome"
    return events[-1]


def test_continue_with_chatgpt_registers_signs_in_and_keeps_it_encrypted(openai):
    e = _sign_in(openai)
    assert e == {"type": "chatgpt", "state": "done", "error": "", "email": "student@example.com"}
    q = openai.authorize
    assert q["client_id"] == "dynamic_agent_client" and q["agent_name_hint"] == "IEUStudia"
    assert q["redirect_uri"].startswith("http://127.0.0.1:") and q["redirect_uri"].endswith("/auth/callback")
    assert q["scope"] == chatgpt.SCOPES and q["resource"] == chatgpt.API and q["code_challenge_method"] == "S256"
    assert q["ext_agent_host_id"].startswith("urn:uuid:") and q["ext_agent_host_id"] == chatgpt.host_id()
    exchange = next(c for c in openai.calls if c[0] == "POST" and c[2].get("grant_type") == "authorization_code")[2]
    assert exchange["client_id"] == "oaiapp_123" and exchange["redirect_uri"] == q["redirect_uri"]
    assert base64.urlsafe_b64encode(hashlib.sha256(exchange["code_verifier"].encode()).digest()).rstrip(b"=").decode() == q["code_challenge"]
    assert chatgpt.signed_in() and chatgpt.status()["email"] == "student@example.com"
    raw = (chatgpt._dir() / "chatgpt.bin").read_bytes()
    assert b"rt-1" not in raw and b"student@example.com" not in raw          # encrypted with DPAPI
    # the next sign-in reuses the issued client id and the hints, without the name hint
    _sign_in(openai)
    q = openai.authorize
    assert q["client_id"] == "oaiapp_123" and "agent_name_hint" not in q and q["login_hint"] == "student@example.com"
    assert q["id_token_hint"]


def test_declined_or_forged_sign_ins_are_not_kept(openai, monkeypatch):
    e = _sign_in(openai, browser={"params": {"error": "access_denied"}})
    assert e["state"] == "error" and "didn't allow" in e["error"] and not chatgpt.signed_in()
    openai.claims = {"aud": "someone-else"}
    assert "not meant for IEUStudia" in _sign_in(openai)["error"]
    openai.claims = {"nonce": "replayed"}
    assert "does not belong" in _sign_in(openai)["error"]
    openai.claims = {}
    monkeypatch.setattr(chatgpt, "_verify_rs256", lambda *a: False)
    assert "could not be verified" in _sign_in(openai)["error"] and not chatgpt.signed_in()


def test_a_callback_with_another_state_is_refused(openai, monkeypatch):
    monkeypatch.setattr(chatgpt, "SIGN_IN_TIMEOUT_S", 1.5)
    e = _sign_in(openai, browser={"wrong_state": True})
    assert e["state"] == "error" and "in time" in e["error"]
    assert not any(c[0] == "POST" for c in openai.calls)                        # no code exchanged


def test_signed_in_without_plan_use_is_said(openai):
    openai.scope = "email openid profile offline_access"
    assert _sign_in(openai)["state"] == "done"
    st = chatgpt.status()
    assert st["signed_in"] and not st["plan_allowed"] and not chatgpt.signed_in()
    with pytest.raises(RuntimeError, match="needs ChatGPT Plus or Pro"):
        chatgpt.access_token()


def test_tokens_are_refreshed_and_an_ended_sign_in_is_forgotten(openai):
    _sign_in(openai)
    assert chatgpt.access_token() == "at-1"
    rec = chatgpt._load()
    rec["expires_at"] = time.time() + 30
    chatgpt._save(rec)
    assert chatgpt.access_token() == "at-2" and chatgpt._load()["refresh_token"] == "rt-2"
    refresh = [c[2] for c in openai.calls if c[0] == "POST" and c[2].get("grant_type") == "refresh_token"][0]
    assert refresh == {"grant_type": "refresh_token", "client_id": "oaiapp_123", "refresh_token": "rt-1", "resource": chatgpt.API}
    rec = chatgpt._load()
    rec["expires_at"] = 0
    chatgpt._save(rec)
    openai.refresh_error = "refresh_token_reused"
    with pytest.raises(RuntimeError, match="has ended"):
        chatgpt.access_token()
    assert not chatgpt.signed_in()


def test_answers_stream_and_errors_are_explained(openai):
    _sign_in(openai)
    openai.stream_answers = [(200, _sse({"type": "response.output_text.delta", "delta": "Hel"},
                                        {"type": "response.output_text.delta", "delta": "lo"},
                                        {"type": "response.completed", "response": {"usage": {
                                            "input_tokens": 1200, "output_tokens": 3, "input_tokens_details": {"cached_tokens": 1000}}}}))]
    seen = []
    r = chatgpt.one_shot("Be brief.", [{"type": "text", "text": "Say hello"},
                                       {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "AAAA"}}],
                         model="opus", on_text=seen.append)
    assert r["text"] == "Hello" and seen[-1] == "Hello" and r["model"] == "gpt-6.1-sol"
    assert r["usage"] == {"input_tokens": 1200, "output_tokens": 3, "cache_read_input_tokens": 1000}
    body = [c[2] for c in openai.calls if c[1].endswith("/responses")][0]
    assert body["store"] is False and body["stream"] is True and body["instructions"] == "Be brief."
    assert body["reasoning"] == {"effort": "high"} and body["input"][0]["content"][1]["image_url"] == "data:image/jpeg;base64,AAAA"
    # a model without an effort setting: asked again without it
    openai.stream_answers = [(400, {"error": {"code": "subscription_sharing_unsupported_capability", "param": "reasoning.effort",
                                              "message": "unsupported"}}),
                             (200, _sse({"type": "response.output_text.delta", "delta": "ok"}, {"type": "response.completed", "response": {}}))]
    assert chatgpt.one_shot("x", [{"type": "text", "text": "y"}])["text"] == "ok"
    assert "reasoning" not in [c[2] for c in openai.calls if c[1].endswith("/responses")][-1]
    openai.stream_answers = [(429, {"error": {"code": "subscription_sharing_usage_limit_exceeded"}})]
    with pytest.raises(RuntimeError, match="usage limit"):
        chatgpt.one_shot("x", [{"type": "text", "text": "y"}])
    openai.stream_answers = [(200, _sse({"type": "response.output_text.delta", "delta": "par"},
                                        {"type": "response.failed", "response": {"error": {"code": "subscription_sharing_user_not_eligible"}}}))]
    with pytest.raises(RuntimeError, match="Plus or Pro"):
        chatgpt.one_shot("x", [{"type": "text", "text": "y"}])
    openai.stream_answers = [(200, _sse({"type": "response.output_text.delta", "delta": "half"}))]   # cut off
    with pytest.raises(RuntimeError, match="ended before"):
        chatgpt.one_shot("x", [{"type": "text", "text": "y"}])


def test_stop_ends_a_quiet_answer_at_once(openai):
    from studia.cancel import Cancelled

    _sign_in(openai)

    class Quiet(_Resp):                       # ChatGPT is thinking: nothing arrives until the stream is closed
        def __init__(self):
            super().__init__(200, {})
            self.gone = threading.Event()

        def iter_lines(self):
            self.gone.wait(10)
            raise ConnectionError("closed")
            yield b""                         # noqa: unreachable - makes this a generator

        def close(self):
            self.gone.set()

    openai.stream_answers = [(200, Quiet())]
    stop, out = threading.Event(), {}

    def ask():
        try:
            chatgpt.one_shot("x", [{"type": "text", "text": "y"}], cancel=stop)
        except BaseException as exc:  # noqa: BLE001
            out["exc"] = exc

    t = threading.Thread(target=ask)
    t.start()
    time.sleep(0.4)
    started = time.monotonic()
    stop.set()
    t.join(5)
    assert isinstance(out.get("exc"), Cancelled) and time.monotonic() - started < 2


def _no_cli():
    return {"available": False, "path": "", "version": ""}


_no_cli.cache_clear = lambda: None


def test_the_app_uses_it_for_chatgpt_and_signing_out_forgets_it(openai, monkeypatch):
    from studia import appconfig

    appconfig.update({"ai_provider": "chatgpt"})
    monkeypatch.setattr(ai, "codex_cli_info", _no_cli)
    assert not ai.ai_info()["available"]
    _sign_in(openai)
    info = ai.ai_info()
    assert info["available"] and info["label"] == "ChatGPT" and info["can_plans"] and info["can_images"]
    openai.stream_answers = [(200, _sse({"type": "response.output_text.delta", "delta": "OK"}, {"type": "response.completed", "response": {}}))]
    assert ai_setup.test("chatgpt")["ok"] and ai_setup.last_test("codex") == {}
    s = ai.ai_status("chatgpt")
    assert "claude" not in s and "codex" not in s
    s = s["chatgpt"]
    assert s["signed_in"] and s["plan_allowed"] and s["email"] == "student@example.com" and s["models"] == ["gpt-6.1-sol"]
    openai.stream_answers = [(200, _sse({"type": "response.output_text.delta", "delta": "Hi"}, {"type": "response.completed", "response": {}}))]
    assert ai.new_session("ctx").ask([{"type": "text", "text": "q"}])["text"] == "Hi"
    out = chatgpt.sign_out()
    assert out["revoked"] and not chatgpt.signed_in() and not (chatgpt._dir() / "chatgpt.bin").exists()
    revoke = [c[2] for c in openai.calls if c[1].endswith("/revoke")][0]
    assert revoke == {"token": "rt-1", "token_type_hint": "refresh_token", "client_id": "oaiapp_123"}


def test_the_codex_cli_choice_never_uses_the_app_sign_in(openai, monkeypatch):
    from studia import appconfig

    _sign_in(openai)
    appconfig.update({"ai_provider": "codex"})
    monkeypatch.setattr(ai, "codex_cli_info", _no_cli)
    assert not ai.ai_info()["available"]
    with pytest.raises(RuntimeError, match="Codex CLI is not installed"):
        ai.one_shot("x", [{"type": "text", "text": "y"}])
    assert not [c for c in openai.calls if c[1].endswith("/responses")]


def test_an_earlier_chatgpt_choice_moves_to_the_app_sign_in_once(openai):
    from studia import appconfig

    appconfig.save(appconfig.AppConfig(ai_provider="codex"))              # as an earlier version stored it
    _sign_in(openai)
    appconfig.migrate()
    assert appconfig.load().ai_provider == "chatgpt" and appconfig.load().version == 1
    appconfig.update({"ai_provider": "codex"})                            # chosen on purpose afterwards: kept
    appconfig.migrate()
    assert appconfig.load().ai_provider == "codex"


def test_an_earlier_codex_cli_choice_stays(openai, monkeypatch):
    from studia import appconfig

    monkeypatch.setattr(ai, "_codex_candidates", lambda: ["C:/tools/codex.exe"])
    appconfig.save(appconfig.AppConfig(ai_provider="codex"))              # the CLI, never signed in the other way
    appconfig.migrate()
    assert appconfig.load().ai_provider == "codex" and appconfig.load().version == 1


def test_an_earlier_chatgpt_choice_without_the_cli_moves_too(openai, monkeypatch):
    from studia import appconfig

    monkeypatch.setattr(ai, "_codex_candidates", lambda: [])
    appconfig.save(appconfig.AppConfig(ai_provider="codex"))              # chose ChatGPT but never signed in yet
    appconfig.migrate()
    assert appconfig.load().ai_provider == "chatgpt"


def test_rs256_verification():
    msg = b"header.payload"
    k = (N.bit_length() + 7) // 8
    digest_info = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(msg).digest()
    em = b"\x00\x01" + b"\xff" * (k - len(digest_info) - 3) + b"\x00" + digest_info
    sig = pow(int.from_bytes(em, "big"), D, N).to_bytes(k, "big")
    assert chatgpt._verify_rs256(msg, sig, N, E)
    assert not chatgpt._verify_rs256(b"header.other", sig, N, E)
    assert not chatgpt._verify_rs256(msg, sig[:-1], N, E)
    assert not chatgpt._verify_rs256(msg, (int.from_bytes(sig, "big") ^ 1).to_bytes(k, "big"), N, E)
