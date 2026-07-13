"""Open WebUI intake helper.

The actual bridge is an OWUI *Pipe* function (owui/agent_media_pipe.py) that
forwards OWUI messages to the canvas `/input` endpoint. This module is just the
setup helper you run on the host: it prints the exact Valve values to paste into
that Pipe, and checks the canvas is reachable and its /input surface is authed.

    media-intake-owui pair     # print canvas_url + amux_token for the OWUI Valves
    media-intake-owui check    # confirm the canvas /input surface is up + authed

Environment (matches the canvas server / CLI so a paired host "just works"):
    MEDIA_CANVAS_URL             explicit canvas base URL (wins over everything)
    MEDIA_VISUAL_URL             canvas push target(s) the `media` CLI uses;
                                 the first entry is borrowed when set
    MEDIA_VISUAL_PORT            listen port (default 8781) for the loopback default
    AMUX_AUTH_TOKEN              amux token (env wins over ~/.amux/auth_token;
                                 "none" means the token was deliberately dropped)
    MEDIA_VISUAL_TRUST_TAILNET   "1" → canvas trusts the tailnet, so /input needs
                                 no token and the amux_token valve stays blank
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


def _amux_token() -> str:
    """Same resolution the canvas server uses (env wins, then ~/.amux)."""
    tok = os.environ.get("AMUX_AUTH_TOKEN", "")
    if tok:
        return "" if tok.lower() == "none" else tok
    try:
        return (Path.home() / ".amux" / "auth_token").read_text().strip()
    except OSError:
        return ""


def _trust_tailnet() -> bool:
    return (os.environ.get("MEDIA_VISUAL_TRUST_TAILNET") or "").strip() == "1"


def _canvas_url() -> str:
    if url := os.environ.get("MEDIA_CANVAS_URL"):
        return url.rstrip("/")
    # Borrow the `media` CLI's push target if set (may be comma-separated).
    if visual := os.environ.get("MEDIA_VISUAL_URL"):
        return visual.split(",")[0].strip().rstrip("/")
    port = os.environ.get("MEDIA_VISUAL_PORT", "8781")
    return f"http://127.0.0.1:{port}"


def _pair() -> int:
    url, token, trust = _canvas_url(), _amux_token(), _trust_tailnet()
    print("Paste these into the agent-media Pipe's Valves (OWUI → Functions):\n")
    print(f"  canvas_url      {url}")
    if token:
        print(f"  amux_token      {token}")
    elif trust:
        print("  amux_token      (leave blank — canvas trusts the tailnet)")
    else:
        print("  amux_token      (NONE FOUND — no ~/.amux/auth_token and "
              "MEDIA_VISUAL_TRUST_TAILNET != 1; /input will stay closed)")
    print("  default_target  speaker        # or amux:<session-name>")
    return 0 if (token or trust) else 1


def _check() -> int:
    url = _canvas_url()
    try:
        with urllib.request.urlopen(url + "/healthz", timeout=5) as resp:
            ok = resp.status == 200
    except (urllib.error.URLError, OSError) as e:
        print(f"✗ canvas unreachable at {url}: {e}", file=sys.stderr)
        return 2
    if not ok:
        print(f"✗ canvas /healthz not 200 at {url}", file=sys.stderr)
        return 2
    print(f"✓ canvas up at {url}")
    if _amux_token() or _trust_tailnet():
        print("✓ /input is authorized (token present or tailnet-trusted)")
        return 0
    print("✗ /input has no credential — run on the host with ~/.amux/auth_token "
          "or set MEDIA_VISUAL_TRUST_TAILNET=1", file=sys.stderr)
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(prog="media-intake-owui")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("pair", help="print the OWUI Pipe Valve values")
    sub.add_parser("check", help="confirm the canvas /input surface is reachable + authed")
    args = ap.parse_args()
    if args.cmd == "check":
        return _check()
    return _pair()  # default


if __name__ == "__main__":
    sys.exit(main())
