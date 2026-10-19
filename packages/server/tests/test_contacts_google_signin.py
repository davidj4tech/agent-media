"""Google, per book (contract §6.25 "Google, per book"): the app's sign-in —
`POST /contacts/google/login`, `/finish`, `/signout`, `GET /contacts/google`
— lands each account's token in its own book, never another's; a refresh
Google refuses (`invalid_grant`, the testing-mode 7 days) marks the book's
Google expired and keeps its last copy. Over real HTTP with real device
tokens, in a throwaway state dir, against a fake Google (no network)."""

from __future__ import annotations

import base64
import json
import os
import urllib.parse

import pytest

from agent_media_server import auth, contacts, devices, matrix
from test_contract import call, server, typed  # noqa: F401

ISS = "https://cms.test"


def _id_token(email):
    seg = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")  # noqa: E731
    return f"{seg({'alg': 'none'})}.{seg({'email': email})}.sig"


class FakeGoogle:
    """The token endpoint, the People API and revoke, as `_request` sees them."""

    def __init__(self):
        self.calls = []
        self.codes = {}            # code -> (email, refresh token)
        self.refusing = set()      # refresh tokens answered invalid_grant
        self.people = [{"resourceName": "people/c1", "etag": "e1",
                        "names": [{"displayName": "Lionel Smith"}],
                        "phoneNumbers": [{"value": "0412 345 678"}]}]

    def __call__(self, method, url, *, form=None, headers=None, timeout=20.0):
        self.calls.append((method, url, dict(form or {})))
        if url.endswith("/token"):
            if form["grant_type"] == "authorization_code":
                if form["code"] not in self.codes or not form.get("code_verifier"):
                    return 400, {"error": "invalid_grant"}
                email, refresh = self.codes[form["code"]]
                return 200, {"access_token": f"at-{refresh}", "expires_in": 3600,
                             "refresh_token": refresh, "id_token": _id_token(email),
                             "scope": contacts.LOGIN_SCOPE}
            if form["refresh_token"] in self.refusing:
                return 400, {"error": "invalid_grant"}
            return 200, {"access_token": f"at-{form['refresh_token']}", "expires_in": 3600}
        if "/revoke" in url:
            return 200, {}
        if "people/me/connections" in url:
            return 200, {"connections": self.people, "nextSyncToken": "st1"}
        raise AssertionError(f"unexpected Google request {method} {url}")


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for k in list(os.environ):
        if k.startswith(("MATRIX_", "MEDIA_")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("MEDIA_CONTACTS_ADB", str(tmp_path / "no-such-adb") + " shell")
    monkeypatch.setattr(matrix, "_ROOMS", {})
    contacts._reset_for_tests()
    yield
    auth._REQ.__dict__.clear()
    contacts._reset_for_tests()


@pytest.fixture
def google(monkeypatch):
    fake = FakeGoogle()
    monkeypatch.setattr(contacts, "_request", fake)
    path = contacts.client_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"installed": {"client_id": "cid.apps", "client_secret": "s"}}))
    # The sync after a sign-in, run inline so a test can look at its result.
    def inline(key):
        with contacts.as_account(key):
            contacts.sync_google(full=True)
    monkeypatch.setattr(contacts, "_kick_sync", inline)
    return fake


def _enrol(sub, email):
    return devices.enrol_account({"iss": ISS, "sub": sub, "email": email}, "phone")["token"]


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


def _sign_in(server, tok, google, email, refresh, port=48123):
    res, obj = call(server, "POST", "/contacts/google/login", {"port": port}, headers=H(tok))
    assert res.status == 200, obj
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(obj["url"]).query)
    assert q["redirect_uri"] == [f"http://127.0.0.1:{port}"]
    assert q["state"] == [obj["state"]] and q["code_challenge_method"] == ["S256"]
    assert "email" in q["scope"][0].split() and contacts.SCOPE in q["scope"][0].split()
    code = f"4/code-{refresh}"
    google.codes[code] = (email, refresh)
    return obj["state"], code


def test_an_account_signs_in_to_its_own_book(server, google):
    kat = _enrol("kat", "k@x.test")
    state, code = _sign_in(server, kat, google, "kat@gmail.test", "rt-kat")
    redirect = f"http://127.0.0.1:48123/?state={state}&code={urllib.parse.quote(code)}&scope=x"
    res, obj = call(server, "POST", "/contacts/google/finish",
                    {"state": state, "url": redirect}, headers=H(kat))
    assert res.status == 200, obj
    g = obj["google"]
    assert g["signed_in"] and not g["expired"] and g["email"] == "kat@gmail.test"
    assert g["contacts"] == 1 and g["last_sync"]
    key = auth.account_key_of(ISS, "kat")
    with contacts.as_account(key):
        assert contacts.load_token()["refresh_token"] == "rt-kat"
        assert oct(contacts.token_path().stat().st_mode & 0o777) == "0o600"
    # The owner's book is untouched.
    assert contacts.load_token() is None
    res, obj = call(server, "GET", "/contacts", headers=H(kat))
    assert [c["name"] for c in obj["contacts"]] == ["Lionel Smith"]


def test_a_bare_code_finishes_too(server, google):
    kat = _enrol("kat", "k@x.test")
    state, code = _sign_in(server, kat, google, "kat@gmail.test", "rt-kat")
    res, obj = call(server, "POST", "/contacts/google/finish",
                    {"state": state, "code": code}, headers=H(kat))
    assert res.status == 200 and obj["google"]["signed_in"]


def test_another_accounts_state_is_refused(server, google):
    kat, sam = _enrol("kat", "k@x.test"), _enrol("sam", "s@x.test")
    state, code = _sign_in(server, kat, google, "kat@gmail.test", "rt-kat")
    res, obj = call(server, "POST", "/contacts/google/finish",
                    {"state": state, "code": code}, headers=H(sam))
    assert res.status == 403
    assert not [c for c in google.calls if c[2].get("grant_type") == "authorization_code"]
    with contacts.as_account(auth.account_key_of(ISS, "sam")):
        assert contacts.load_token() is None
    # Still Kat's to finish: the refusal did not spend it.
    res, obj = call(server, "POST", "/contacts/google/finish",
                    {"state": state, "code": code}, headers=H(kat))
    assert res.status == 200 and obj["google"]["signed_in"]


def test_a_state_is_one_use_and_expires(server, google, monkeypatch):
    kat = _enrol("kat", "k@x.test")
    state, code = _sign_in(server, kat, google, "kat@gmail.test", "rt-kat")
    assert call(server, "POST", "/contacts/google/finish",
                {"state": state, "code": code}, headers=H(kat))[0].status == 200
    res, obj = call(server, "POST", "/contacts/google/finish",
                    {"state": state, "code": code}, headers=H(kat))
    assert res.status == 400 and "start again" in obj["error"]
    state, code = _sign_in(server, kat, google, "kat@gmail.test", "rt-kat2")
    contacts._PENDING[state]["expires"] = 0
    res, _ = call(server, "POST", "/contacts/google/finish",
                  {"state": state, "code": code}, headers=H(kat))
    assert res.status == 400
    res, _ = call(server, "POST", "/contacts/google/finish",
                  {"state": "made-up", "code": code}, headers=H(kat))
    assert res.status == 400


def test_an_address_from_another_sign_in_is_refused(server, google):
    kat = _enrol("kat", "k@x.test")
    state, code = _sign_in(server, kat, google, "kat@gmail.test", "rt-kat")
    res, obj = call(server, "POST", "/contacts/google/finish",
                    {"state": state, "url": f"http://127.0.0.1:1/?state=other&code={code}"},
                    headers=H(kat))
    assert res.status == 400 and "different sign-in" in obj["error"]


def test_login_needs_a_port_and_a_client(server, google):
    kat = _enrol("kat", "k@x.test")
    assert call(server, "POST", "/contacts/google/login", {}, headers=H(kat))[0].status == 400
    assert call(server, "POST", "/contacts/google/login", {"port": 80},
                headers=H(kat))[0].status == 400
    contacts.client_path().unlink()
    res, obj = call(server, "POST", "/contacts/google/login", {"port": 48123}, headers=H(kat))
    assert res.status == 503 and obj["no_client"]
    res, obj = call(server, "GET", "/contacts/google", headers=H(kat))
    assert res.status == 200 and obj["google"]["available"] is False
    # Where each export is requested, whatever Google's state (Import a file).
    ex = obj["google"]["exports"]
    assert [(x["network"], x["label"]) for x in ex] == [
        ("facebook", "Facebook friends"), ("instagram", "Instagram followers and following")]
    assert {x["url"] for x in ex} == {"https://accountscentre.facebook.com/info_and_permissions/dyi"}
    assert "Friends and followers" in ex[0]["steps"] and "JSON" in ex[0]["steps"]
    assert "Followers and following" in ex[1]["steps"] and "All time" in ex[1]["steps"]


def test_the_routes_are_gated(server, google):
    for method, path in (("GET", "/contacts/google"), ("POST", "/contacts/google/login"),
                         ("POST", "/contacts/google/finish"),
                         ("POST", "/contacts/google/signout")):
        res, _ = call(server, method, path, {} if method == "POST" else None)
        assert res.status in (401, 403), path


def test_an_expired_sign_in_says_sign_in_again_and_keeps_the_copy(server, google):
    kat = _enrol("kat", "k@x.test")
    state, code = _sign_in(server, kat, google, "kat@gmail.test", "rt-kat")
    call(server, "POST", "/contacts/google/finish", {"state": state, "code": code}, headers=H(kat))
    key = auth.account_key_of(ISS, "kat")
    google.refusing.add("rt-kat")
    with contacts.as_account(key):
        contacts._access().clear()             # the hour is up: a refresh is needed
        with pytest.raises(contacts.SignInExpired):
            contacts.sync_google()
        assert "google" not in contacts.sources()      # not tried again until signed in
        assert contacts.sync()["errors"] == {}
    res, obj = call(server, "GET", "/contacts/google", headers=H(kat))
    g = obj["google"]
    assert g["expired"] and not g["signed_in"] and g["email"] == "kat@gmail.test"
    assert g["contacts"] == 1                  # the last good copy
    res, obj = call(server, "GET", "/contacts", headers=H(kat))
    assert [c["name"] for c in obj["contacts"]] == ["Lionel Smith"]
    # Signing in again clears it.
    state, code = _sign_in(server, kat, google, "kat@gmail.test", "rt-kat-new")
    res, obj = call(server, "POST", "/contacts/google/finish",
                    {"state": state, "code": code}, headers=H(kat))
    assert obj["google"]["signed_in"] and not obj["google"]["expired"]


def test_sign_out_forgets_google_in_that_book_only(server, google):
    kat, sam = _enrol("kat", "k@x.test"), _enrol("sam", "s@x.test")
    for tok, who in ((kat, "kat"), (sam, "sam")):
        state, code = _sign_in(server, tok, google, f"{who}@gmail.test", f"rt-{who}")
        call(server, "POST", "/contacts/google/finish", {"state": state, "code": code},
             headers=H(tok))
    res, obj = call(server, "POST", "/contacts/google/signout", {}, headers=H(kat))
    assert res.status == 200
    assert not obj["google"]["signed_in"] and obj["google"]["contacts"] == 0
    assert any("/revoke" in c[1] and c[2]["token"] == "rt-kat" for c in google.calls)
    assert call(server, "GET", "/contacts", headers=H(kat))[1]["contacts"] == []
    res, obj = call(server, "GET", "/contacts/google", headers=H(sam))
    assert obj["google"]["signed_in"] and obj["google"]["email"] == "sam@gmail.test"


def test_the_cli_login_writes_the_named_book(google, monkeypatch, capsys):
    """`media contacts --account WHO login`: the token in WHO's book."""
    _enrol("kat", "k@x.test")
    key = auth.account_key_of(ISS, "kat")
    seen = {}

    class FakeLogin:
        def __init__(self, client, port=0):
            self.redirect_uri, self.url = "http://127.0.0.1:1", "https://consent"
            import queue
            self.answers = queue.Queue()
            self.answers.put(("loopback", "/?code=4/code-cli&state=s"))
            seen["account"] = contacts.account()

        def finish(self, text):
            google.codes["4/code-cli"] = ("kat@gmail.test", "rt-cli")
            return contacts.exchange(contacts.load_client(), "4/code-cli", "v", self.redirect_uri)

        def close(self):
            pass
    monkeypatch.setattr(contacts, "Login", FakeLogin)
    monkeypatch.setattr(contacts, "_read_stdin", lambda q: None)
    assert contacts.main(["--account", "k@x.test", "login"]) == 0
    assert seen["account"] == key
    with contacts.as_account(key):
        tok = contacts.load_token()
        assert tok["refresh_token"] == "rt-cli" and tok["email"] == "kat@gmail.test"
    assert contacts.load_token() is None       # not the owner's
