"""The quick tunnel (tunnel.py). No network: cloudflared is a stand-in."""

from __future__ import annotations

import json
import os
import sys
import threading
import time

from agent_media_server import tunnel


def test_run_writes_the_url_and_clears_it(monkeypatch, tmp_path):
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
        if tunnel.current_url():
            break
        time.sleep(0.05)
    assert tunnel.current_url() == "https://quiet-river-a1b2.trycloudflare.com"
    assert tunnel.main(["url"]) == 0
    t.join(5)
    assert seen == [0]
    assert tunnel.current_url() == ""                       # gone with its runner
    assert tunnel.main(["url"]) == 1
    args = json.loads((tmp_path / "args").read_text())
    assert args[-2:] == ["--url", "http://127.0.0.1:18789"] and "--config" in args


def test_a_dead_runner_has_no_url():
    tunnel.state_path().parent.mkdir(parents=True, exist_ok=True)
    tunnel.state_path().write_text('{"url": "https://old.trycloudflare.com", "pid": 999999999}')
    assert tunnel.current_url() == ""


def test_platform_assets_are_pinned():
    for name, sha in tunnel.CLOUDFLARED_ASSETS.values():
        assert name.startswith("cloudflared-") and len(sha) == 64
    if sys.platform.startswith("linux") and os.uname().machine in ("x86_64", "aarch64"):
        assert tunnel.platform_asset() is not None


def test_fetch_keeps_one_already_there(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    (tmp_path / "cloudflared").write_text("x")
    path, what = tunnel.fetch_cloudflared(tmp_path)
    assert path == str(tmp_path / "cloudflared") and "already there" in what
