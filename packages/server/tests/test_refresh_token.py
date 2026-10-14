"""An account's refresh token is never lost (devices.py, app._account_access).

The issuer (Drupal simple_oauth at cms.sasonica.com) rotates the refresh
token on every use and refuses the old one. On 14 Oct 2026 the phone's
device row ended up holding a spent one and every refresh after answered
400. These pin what keeps that from happening again: one refresh path, under
one lock that holds across processes; the rotated token written before
anything else can fail; and every write of devices.json re-reading the file
under a file lock instead of saving a copy taken before another writer's
change. No network, no real state (conftest points XDG_STATE_HOME at a
throwaway dir; the issuer is stood in for).
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import pytest

from agent_media_server import app, devices, oidc

ISS = "https://cms.sasonica.test"


@pytest.fixture(autouse=True)
def _fresh():
    devices._reset_for_tests()
    app._ACCESS.clear()
    app._REFRESHING.clear()
    yield
    app._ACCESS.clear()


class Issuer:
    """A rotating issuer: each refresh token works once."""

    def __init__(self, rt: str):
        self.valid, self.n, self.spent = rt, 0, []
        self._lock = threading.Lock()

    def __call__(self, iss, rt):
        with self._lock:
            if rt != self.valid:
                self.spent.append(rt)
                raise oidc.OidcError("sign_in_again", "the issuer would not refresh: HTTP Error 400: Bad Request")
            self.n += 1
            self.valid = f"rt-{self.n}"
            got = (f"at-{self.n}", self.valid, 300.0)
        time.sleep(0.005)         # the round trip: room for a second caller to slip in
        return got


def _account(rt="rt-0"):
    return devices.enrol_account({"iss": ISS, "sub": "1", "email": "a@example.com"}, "phone",
                                 refresh_token=rt, username="a")


def test_a_failed_userinfo_keeps_the_rotated_token(monkeypatch):
    """The profile refresh behind /me spent the refresh token and then asked
    userinfo; a userinfo that failed took the new token down with it."""
    got = _account()
    issuer = Issuer("rt-0")
    monkeypatch.setattr(oidc, "access_token", issuer)

    def no_userinfo(iss, access):
        raise oidc.OidcError("bad_id_token", "the issuer would not say who this is: timed out")
    monkeypatch.setattr(oidc, "userinfo", no_userinfo)
    monkeypatch.setattr(app, "_in_background", lambda fn: fn())
    dev = {**devices.lookup(got["token"]), "profile_at": 0}
    app._refresh_profile_soon(dev)
    assert devices.refresh_token_of(got["device_id"])[1] == "rt-1" == issuer.valid
    app._ACCESS.clear()
    assert app._account_access(got["device_id"], ISS) == "at-2", "the kept token still works"


def test_a_refused_access_token_at_userinfo_retries_once_fresh(monkeypatch):
    got = _account()
    monkeypatch.setattr(oidc, "access_token", Issuer("rt-0"))
    asked = []

    def userinfo(iss, access):
        asked.append(access)
        if access == "at-1":
            raise oidc.OidcError("unauthorized", "401")
        return {"sub": "1", "email": "new@example.com", "preferred_username": "b"}
    monkeypatch.setattr(oidc, "userinfo", userinfo)
    monkeypatch.setattr(app, "_in_background", lambda fn: fn())
    app._refresh_profile_soon({**devices.lookup(got["token"]), "profile_at": 0})
    assert asked == ["at-1", "at-2"]
    assert devices.refresh_token_of(got["device_id"])[1] == "rt-2"
    assert devices.list_devices()[0]["account"] == "new@example.com"


def test_an_answer_without_an_access_token_still_keeps_the_new_refresh_token(monkeypatch):
    got = _account()
    monkeypatch.setattr(oidc, "_token_request", lambda iss, params: {"refresh_token": "rt-new"})
    monkeypatch.setenv("MEDIA_OIDC_ISSUERS", ISS)
    with pytest.raises(oidc.OidcError):
        app._account_access(got["device_id"], ISS)
    assert devices.refresh_token_of(got["device_id"])[1] == "rt-new"


def test_update_profile_never_writes_the_refresh_token():
    """/me/account read the token, then wrote it back with the profile: a
    refresh in between was undone."""
    got = _account()
    devices.set_refresh_token(got["device_id"], "rt-9")
    assert devices.update_profile(got["device_id"], "b@example.com", "b", "")
    assert devices.refresh_token_of(got["device_id"])[1] == "rt-9"


def test_threads_never_spend_the_same_token_twice(monkeypatch):
    """The servers report, the profile refresh and the profile sheet all
    refresh; at once, each must spend the token the last one kept."""
    got = _account()
    issuer = Issuer("rt-0")
    monkeypatch.setattr(oidc, "access_token", issuer)
    errors = []

    def worker():
        for _ in range(8):
            try:
                app._account_access(got["device_id"], ISS, fresh=True)
            except oidc.OidcError as e:
                errors.append(e)
    ts = [threading.Thread(target=worker) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors and not issuer.spent
    assert issuer.n == 32 and devices.refresh_token_of(got["device_id"])[1] == issuer.valid


def test_a_stale_cached_copy_does_not_put_a_spent_token_back():
    """The last_seen stamp saved the rows the bearer was matched against —
    the mtime cache's copy. Another writer's rotation that the cache missed
    (same size, same mtime tick) was reverted by it."""
    got = _account("rt-aaaa")
    p = devices.devices_path()
    devices.list_devices()                            # the cache now holds rt-aaaa
    st = p.stat()
    # Another process rotates it: same length, and the same mtime stamp.
    text = p.read_text().replace("rt-aaaa", "rt-bbbb")
    p.write_text(text)
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert p.stat().st_size == st.st_size
    rows = devices._load()
    rows[0]["last_seen"] = 0                          # due a stamp on the next request
    assert devices.lookup(got["token"], "1.2.3.4")
    assert devices.refresh_token_of(got["device_id"])[1] == "rt-bbbb"


_CHILD = r"""
import sys, time
from agent_media_server import devices
mode = sys.argv[1]
if mode == "hold":
    with devices.refresh_lock():
        print("held", flush=True)
        time.sleep(1.0)
        devices.set_refresh_token(sys.argv[2], "rt-child")
elif mode == "enrol-toggles":
    for i in range(int(sys.argv[3])):
        devices.set_enrol(sys.argv[2], i % 2 == 0)
"""


def _child(*args):
    env = {**os.environ}
    return subprocess.Popen([sys.executable, "-c", _CHILD, *args], env=env,
                            stdout=subprocess.PIPE, text=True)


def test_the_refresh_lock_holds_across_processes(monkeypatch):
    """A second process with the same state dir (a CLI, an old canvas still
    finishing a request) waits for the refresh in hand, and the token it
    spends is the one that refresh kept."""
    got = _account()
    proc = _child("hold", got["device_id"])
    try:
        assert proc.stdout.readline().strip() == "held"
        seen = []
        monkeypatch.setattr(oidc, "access_token", lambda iss, rt: (seen.append(rt) or ("at", "rt-next", 300.0)))
        t0 = time.monotonic()
        app._account_access(got["device_id"], ISS)
        assert time.monotonic() - t0 > 0.3, "waited for the other process"
        assert seen == ["rt-child"], "spent what the other process kept, not what it held before"
        assert devices.refresh_token_of(got["device_id"])[1] == "rt-next"
    finally:
        proc.wait(timeout=10)


def test_two_processes_writing_the_file_lose_nothing():
    """Rotations here while another process rewrites the same row (the enrol
    bit, from the CLI): every rotation survives."""
    got = _account()
    proc = _child("enrol-toggles", got["device_id"], "60")
    try:
        for i in range(60):
            devices.set_refresh_token(got["device_id"], f"rt-{i}")
            assert devices.refresh_token_of(got["device_id"])[1] == f"rt-{i}"
    finally:
        assert proc.wait(timeout=30) == 0
    assert devices.refresh_token_of(got["device_id"])[1] == "rt-59"
