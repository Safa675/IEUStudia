"""Signing in to IEULearn against a fake site: a wrong password, a working sign-in, and IEULearn being down."""

import types

import pytest

from studia.ieulearn import BASE, AuthError, IEULearn
from studia.net import NetworkError

FORM = ('<form id="loginForm" method="post" action="/login"><input name="csrf_token" value="tok">'
        '<input name="email"><input name="password" type="password"></form>')


class Site:
    def __init__(self, get, post):
        self.answers = {"GET": get, "POST": post}
        self.posted = []

    def _answer(self, method, url, **kw):
        status, body, where = self.answers[method]
        if method == "POST":
            self.posted.append(kw.get("data"))
        return types.SimpleNamespace(status_code=status, text=body, url=f"{BASE}{where}", ok=status < 400, headers={})

    def get(self, url, **kw):
        return self._answer("GET", url, **kw)

    def post(self, url, **kw):
        return self._answer("POST", url, **kw)


def test_a_wrong_password_is_said_with_ieulearns_own_words():
    site = Site((200, FORM, "/login"), (200, FORM + '<div class="alert alert-danger">Invalid credentials.</div>', "/login"))
    with pytest.raises(AuthError, match="Invalid credentials"):
        IEULearn(site).login("student", "wrong")
    assert site.posted[0]["csrf_token"] == "tok"


def test_signing_in_works():
    IEULearn(Site((200, FORM, "/login"), (200, "<h1>Dashboard</h1>", "/student"))).login("student", "right")


@pytest.mark.parametrize("get,post", [((503, "<h1>Service Unavailable</h1>", "/login"), (503, "", "/login")),
                                      ((200, FORM, "/login"), (502, "<h1>Bad Gateway</h1>", "/login"))])
def test_ieulearn_being_down_is_not_a_wrong_password(get, post):
    with pytest.raises(NetworkError, match="isn't responding right now.*not rejected"):
        IEULearn(Site(get, post)).login("student", "right")


@pytest.mark.parametrize("post", [(429, "<h1>Too Many Requests</h1>", "/login"), (403, "<h1>Forbidden</h1>", "/login"),
                                  (200, "<h1>We'll be back soon</h1>", "/maintenance")])
def test_an_answer_without_the_sign_in_form_is_not_a_refused_password(post):
    with pytest.raises(NetworkError, match="not rejected"):
        IEULearn(Site((200, FORM, "/login"), post)).login("student", "right")


def test_ieulearns_own_message_on_its_sign_in_form_is_a_refusal():
    site = Site((200, FORM, "/login"), (200, FORM + '<div class="alert">Your account is locked for 15 minutes.</div>', "/login"))
    with pytest.raises(AuthError, match="locked"):
        IEULearn(site).login("student", "right")
