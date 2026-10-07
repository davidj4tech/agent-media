"""The quick tunnel and the sasonica.com lookup (tunnel.py).

No network: cloudflared is a stand-in script and the lookup a local HTTP
server checking what the Worker checks (deploy/lookup/src/index.ts)."""

from __future__ import annotations

import base64
import json
import os
import stat
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent_media_core._ed25519 import verify
from agent_media_server import devices, tunnel

from test_contract import call, server, typed  # noqa: F401


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


@pytest.fixture()
def lookup(monkeypatch):
    """A lookup that verifies writes as the Worker does and keeps them."""
    kept: dict[str, dict] = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_PUT(self):  # noqa: N802
            iid = self.path.rsplit("/", 1)[1]
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            body = json.loads(raw)
            pub = _unb64(body["pub"])
            ok = (tunnel.install_id_of(pub) == iid
                  and verify(pub, raw, _unb64(self.headers["X-Sasonica-Signature"])))
            if ok:
                kept[iid] = body
            self.send_response(200 if ok else 401)
            self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("MEDIA_LOOKUP_URL", f"http://127.0.0.1:{srv.server_address[1]}/r")
    yield kept
    srv.shutdown()


def test_the_install_key_is_made_once_and_private():
    seed, pub, iid = tunnel.identity()
    assert len(iid) == 26 and set(iid) <= set("abcdefghijklmnopqrstuvwxyz234567")
    assert tunnel.identity() == (seed, pub, iid)
    assert stat.S_IMODE(tunnel.key_path().stat().st_mode) == 0o600


def test_publish_is_signed_by_the_key_its_id_names(lookup):
    ok, what = tunnel.publish("https://abc-def.trycloudflare.com")
    assert ok, what
    (iid, body), = lookup.items()
    assert iid == tunnel.identity()[2] and body["url"] == "https://abc-def.trycloudflare.com"


def test_lookup_off(monkeypatch):
    monkeypatch.setenv("MEDIA_LOOKUP_URL", "-")
    assert tunnel.publish("https://a.trycloudflare.com") == (False, "lookup off")
    assert tunnel.lookup_url() == ""


def test_run_writes_the_url_publishes_it_and_clears_it(lookup, monkeypatch, tmp_path):
    fake = tmp_path / "cloudflared"
    # What cloudflared prints, give or take: the API host first, then its own.
    fake.write_text(f"""#!{sys.executable}
import sys, time, json, os
print(json.dumps(sys.argv[1:]), file=open(os.environ["ARGS_OUT"], "w"))
print("INF Requesting new quick Tunnel on trycloudflare.com...", file=sys.stderr, flush=True)
print("INF |  https://quiet-river-a1b2.trycloudflare.com  |", file=sys.stderr, flush=True)
time.sleep(1.5)
""")
    fake.chmod(0o755)
    monkeypatch.setenv("MEDIA_CLOUDFLARED", str(fake))
    monkeypatch.setenv("ARGS_OUT", str(tmp_path / "args"))
    monkeypatch.setenv("MEDIA_VISUAL_PUBLIC", "127.0.0.1:18789")
    seen = []
    t = threading.Thread(target=lambda: seen.append(tunnel.run()))
    t.start()
    for _ in range(100):
        if tunnel.current_url() and lookup:
            break
        threading.Event().wait(0.05)
    assert tunnel.current_url() == "https://quiet-river-a1b2.trycloudflare.com"
    assert tunnel.lookup_url().endswith("/r/" + tunnel.identity()[2])
    t.join(5)
    assert seen == [0]
    assert list(lookup.values())[0]["url"] == "https://quiet-river-a1b2.trycloudflare.com"
    assert tunnel.current_url() == ""                       # gone with its runner
    args = json.loads((tmp_path / "args").read_text())
    assert args[-2:] == ["--url", "http://127.0.0.1:18789"] and "--config" in args


def test_pair_answer_names_the_lookup_over_the_tunnel(server, monkeypatch):  # noqa: F811
    monkeypatch.setenv("MEDIA_LOOKUP_URL", "https://lookup.example/r")
    tunnel._write_state("https://abc-def.trycloudflare.com")
    code, _ = devices.mint_code("Pixel 8a")
    res, body = call(server, "POST", "/pair", {"code": code, "device": "x"},
                     {"Host": "abc-def.trycloudflare.com", "X-Forwarded-Proto": "https"})
    assert res.status == 200
    assert body["server"]["base"] == "https://abc-def.trycloudflare.com"
    assert body["server"]["lookup"] == f"https://lookup.example/r/{tunnel.identity()[2]}"
    # Not over the tailnet: that address does not change.
    code, _ = devices.mint_code("Tab")
    res, body = call(server, "POST", "/pair", {"code": code, "device": "y"})
    assert res.status == 200 and "lookup" not in body["server"]


def test_platform_assets_are_pinned():
    for name, sha in tunnel.CLOUDFLARED_ASSETS.values():
        assert name.startswith("cloudflared-") and len(sha) == 64
    if sys.platform.startswith("linux") and os.uname().machine in ("x86_64", "aarch64"):
        assert tunnel.platform_asset() is not None
