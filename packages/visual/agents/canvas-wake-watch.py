#!/usr/bin/env python3
"""canvas-wake-watch — turn a Linux desktop's display on when the canvas asks.

Watches the agent-media canvas SSE stream; when a show event arrives stamped
wake=<this screen> AND the canvas is the foreground window here, simulates
user activity so the display comes back on. Stdlib only.

Foreground checks (first one whose tool exists wins):
  X11      xdotool getactivewindow getwindowname
  sway     swaymsg -t get_tree (focused node's name)
  GNOME    best-effort via org.gnome.Shell.Introspect; if the desktop exposes
           none of these, set CANVAS_WAKE_NO_FOCUS_CHECK=1 to always wake.

Wake actions (all tried, errors ignored — first effective one wins):
  org.gnome.ScreenSaver.SimulateUserActivity (GNOME/cinnamon/mate)
  xset dpms force on                          (X11)

Config (env):
  CANVAS_URL                  default http://100.103.43.93:8781 (red5)
  CANVAS_SCREEN               default $HOSTNAME
  CANVAS_MATCH_TITLE          default "agent-media canvas"
  CANVAS_WAKE_FIGURES_ONLY=1  skip ambient art, wake only for figures
  CANVAS_WAKE_NO_FOCUS_CHECK=1  wake without the foreground test

Install as a systemd user service:
  [Unit]
  Description=canvas wake watch
  [Service]
  ExecStart=/usr/bin/python3 %h/.local/bin/canvas-wake-watch.py
  Restart=always
  RestartSec=5
  [Install]
  WantedBy=default.target

One-time page setup on this screen: open <canvas>/?screen=<name> and pair it
(QR at /pair) — the page's activity beacons make this screen the wake target.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import urllib.request

URL = (os.environ.get("CANVAS_URL") or "http://100.103.43.93:8781").rstrip("/")
SCREEN = os.environ.get("CANVAS_SCREEN") or socket.gethostname().split(".")[0]
MATCH = os.environ.get("CANVAS_MATCH_TITLE") or "agent-media canvas"
FIGURES_ONLY = os.environ.get("CANVAS_WAKE_FIGURES_ONLY") == "1"
NO_FOCUS = os.environ.get("CANVAS_WAKE_NO_FOCUS_CHECK") == "1"


def _run(argv: list[str], timeout: float = 3.0) -> str:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def canvas_is_foreground() -> bool:
    if NO_FOCUS:
        return True
    if shutil.which("xdotool"):
        title = _run(["xdotool", "getactivewindow", "getwindowname"])
        if title:
            return MATCH in title
    if shutil.which("swaymsg"):
        out = _run(["swaymsg", "-t", "get_tree"])
        if out:
            def focused(node):
                if node.get("focused"):
                    return node.get("name") or ""
                return next((t for n in (node.get("nodes") or [])
                             + (node.get("floating_nodes") or [])
                             if (t := focused(n))), "")
            try:
                return MATCH in focused(json.loads(out))
            except ValueError:
                pass
    if shutil.which("gdbus"):
        out = _run(["gdbus", "call", "--session",
                    "--dest", "org.gnome.Shell.Introspect",
                    "--object-path", "/org/gnome/Shell/Introspect",
                    "--method", "org.gnome.Shell.Introspect.GetWindows"])
        if out:
            # crude but sufficient: is the canvas title on the focused window?
            return MATCH in out and "'has-focus': <true>" in \
                out[max(0, out.find(MATCH) - 400):out.find(MATCH) + 400]
    return False  # no tool worked — err on not waking


def wake_display() -> None:
    _run(["gdbus", "call", "--session", "--dest", "org.gnome.ScreenSaver",
          "--object-path", "/org/gnome/ScreenSaver",
          "--method", "org.gnome.ScreenSaver.SimulateUserActivity"])
    _run(["xset", "dpms", "force", "on"])


def watch() -> None:
    req = urllib.request.Request(URL + "/events")
    with urllib.request.urlopen(req, timeout=90) as resp:
        for raw in resp:  # server pings every ~25s keep the read alive
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            try:
                d = json.loads(line[5:].strip())
            except ValueError:
                continue
            if d.get("wake") != SCREEN:
                continue
            if not (d.get("image") or d.get("sequence")):
                continue
            if FIGURES_ONLY and d.get("purpose") != "figure":
                continue
            if canvas_is_foreground():
                wake_display()


if __name__ == "__main__":
    while True:
        try:
            watch()
        except Exception:  # noqa: BLE001 — stream dropped / red5 down: retry
            pass
        time.sleep(5)
