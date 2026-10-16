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
    if os.name != "nt":   # Windows has no mode bits; the profile's ACL guards it
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


def _fake_cloudflared(tmp_path, body: str):
    """A stand-in cloudflared running `body` (Python): a script with a #!
    line, or on Windows a .cmd that runs it with this Python."""
    script = tmp_path / "cloudflared.py"
    script.write_text(f"#!{sys.executable}\n" + body)
    if os.name == "nt":
        fake = tmp_path / "cloudflared.cmd"
        fake.write_text(f'@"{sys.executable}" "{script}" %*\r\n')
        return fake
    script.chmod(0o755)
    return script


def test_run_writes_the_url_publishes_it_and_clears_it(lookup, monkeypatch, tmp_path):
    # What cloudflared prints, give or take: the API host first, then its own.
    fake = _fake_cloudflared(tmp_path, """
import sys, time, json, os
print(json.dumps(sys.argv[1:]), file=open(os.environ["ARGS_OUT"], "w"))
print("INF Requesting new quick Tunnel on trycloudflare.com...", file=sys.stderr, flush=True)
print("INF |  https://quiet-river-a1b2.trycloudflare.com  |", file=sys.stderr, flush=True)
time.sleep(1.5)
""")
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
    if sys.platform == "win32" and os.environ.get("PROCESSOR_ARCHITECTURE") == "AMD64":
        assert tunnel.platform_asset()[0] == "cloudflared-windows-amd64.exe"


@pytest.mark.parametrize("plat,machine,want", [
    ("win32", "AMD64", "cloudflared-windows-amd64.exe"),
    ("win32", "ARM64", None),                       # Cloudflare builds none
    ("linux", "x86_64", "cloudflared-linux-amd64"),
    ("linux", "aarch64", "cloudflared-linux-arm64"),
    ("darwin", "arm64", "cloudflared-darwin-arm64.tgz"),
])
def test_platform_asset_per_platform(monkeypatch, plat, machine, want):
    monkeypatch.setattr(tunnel.sys, "platform", plat)
    monkeypatch.setattr(tunnel.platform, "machine", lambda: machine)
    got = tunnel.platform_asset()
    assert (got[0] if got else None) == want


def test_windows_fetches_cloudflared_exe(monkeypatch, tmp_path):
    monkeypatch.setattr(tunnel.sys, "platform", "win32")
    monkeypatch.setattr(tunnel.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(tunnel.shutil, "which", lambda name: None)
    path, what = tunnel.fetch_cloudflared(tmp_path, dry_run=True)
    assert path == str(tmp_path / "cloudflared.exe")
    assert what.endswith("/2026.9.3/cloudflared-windows-amd64.exe")
    (tmp_path / "cloudflared.exe").write_bytes(b"")
    assert tunnel.fetch_cloudflared(tmp_path)[1].endswith("(already there)")


def test_cloudflared_is_tied_to_its_runner_on_windows_only():
    if os.name == "nt":
        import subprocess
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            assert tunnel._die_with_me(p) is not None
        finally:
            p.kill()
            p.wait()
    else:
        assert tunnel._die_with_me(None) is None
