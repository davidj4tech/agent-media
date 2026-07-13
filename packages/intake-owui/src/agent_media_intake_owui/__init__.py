"""Open WebUI intake helper.

The actual bridge is an OWUI *Pipe* function (owui/agent_media_pipe.py) that
forwards OWUI messages to the canvas `/input` endpoint. This module is just the
setup helper you run on the host: it prints the exact Valve values to paste into
that Pipe, and checks the canvas is reachable.

    media-intake-owui pair     # print canvas_url + amux_token for the OWUI Valves
    media-intake-owui check    # confirm the canvas /input surface is up + authed
"""

from __future__ import annotations

import argparse
import json
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


def _canvas_url() -> str:
    port = os.environ.get("MEDIA_VISUAL_PORT", "8781")
    return os.environ.get("MEDIA_CANVAS_URL", f"http://127.0.0.1:{port}").rstrip("/")


def _pair() -> int:
    url, token = _canvas_url(), _amux_token()
    trust = (os.environ.get("MEDIA_VISUAL_TRUST_TAILNET") or "").strip() == "1"
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
    if _amux_token() or (os.environ.get("MEDIA_VISUAL_TRUST_TAILNET") or "").strip() == "1":
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
