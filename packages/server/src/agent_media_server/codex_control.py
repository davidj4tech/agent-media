"""Conversation settings on Codex's shared local app-server (0.163+).

The TUI no longer owns the rollout fd. Its existing daemon exposes the
canonical model and collaboration settings over a Unix WebSocket. Never
start a daemon, resume an unloaded thread, or send a prompt here.
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager

from agent_media_core import harnesses, native_models

TIMEOUT = 3.0


class RPCError(Exception):
    pass


class Client:
    def __init__(self, websocket):
        self.websocket = websocket
        self.seq = 0

    def call(self, method: str, params: dict) -> dict:
        self.seq += 1
        rid = self.seq
        self.websocket.send(json.dumps({"id": rid, "method": method, "params": params}))
        deadline = time.monotonic() + TIMEOUT
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(method)
            message = json.loads(self.websocket.recv(timeout=remaining))
            if message.get("id") != rid:
                continue
            if "error" in message:
                raise RPCError(str(message["error"].get("message") or "Codex refused the request"))
            return message.get("result") or {}

    def read(self, session: str) -> dict | None:
        thread = self.call("thread/read", {"threadId": session, "includeTurns": False}).get("thread") or {}
        status = thread.get("status") or {}
        if status.get("type") == "notLoaded":
            return None
        # With a loaded thread this rejoins it, without configuration
        # overrides. It returns the effective collaborationMode as well as
        # model, and does not execute a turn or change the thread's settings.
        result = self.call("thread/resume", {"threadId": session, "excludeTurns": True})
        return {"model": result.get("model") or thread.get("model") or "",
                "effort": result.get("reasoningEffort"),
                "plan": (result.get("collaborationMode") or {}).get("mode") == "plan",
                "mode_known": result.get("collaborationMode") is not None,
                "status": status.get("type", "notLoaded")}


@contextmanager
def connect(session: str):
    from websockets.sync.client import unix_connect

    profile = harnesses.profile_of(session)
    path = native_models.home("codex", profile) / "app-server-control" / "app-server-control.sock"
    with unix_connect(str(path), open_timeout=TIMEOUT, close_timeout=0.2,
                      max_size=1024 * 1024, compression=None) as websocket:
        client = Client(websocket)
        client.call("initialize", {"clientInfo": {"name": "sasonica-settings", "version": "0.1"},
                                   "capabilities": {"experimentalApi": True}})
        websocket.send(json.dumps({"method": "initialized"}))
        yield client


def state(session: str) -> dict | None:
    path = native_models.home("codex", harnesses.profile_of(session)) / "app-server-control" / "app-server-control.sock"
    if not path.exists():
        return None
    try:
        with connect(session) as client:
            return client.read(session)
    except (OSError, ValueError, RPCError, TimeoutError, ImportError):
        return None
    except Exception:  # WebSocket handshake/connection errors: fall back to the pane.
        return None


def configure(session: str, *, model: str | None, mode: str | None) -> tuple[bool, dict]:
    try:
        with connect(session) as client:
            before = client.read(session)
            if before is None:
                return False, {"error": "this Codex thread is not loaded", "status": 409}
            if before["status"] != "idle":
                return False, {"error": "wait for the turn or question to finish, then change it", "status": 409}
            params = {"threadId": session}
            if model is not None:
                params["model"] = model
            if mode is not None:
                if not before["mode_known"]:
                    return False, {"error": "this Codex daemon does not expose plan settings", "status": 400}
                params["collaborationMode"] = {
                    "mode": "plan" if mode == "plan" else "default",
                    "settings": {"model": model or before["model"],
                                 "reasoning_effort": before["effort"],
                                 "developer_instructions": None}}
            client.call("thread/settings/update", params)
            after = client.read(session)
            if not after or (model is not None and after["model"] != model) or (
                    mode is not None and after["plan"] != (mode == "plan")):
                return False, {"error": "Codex did not confirm the settings change", "status": 502}
            return True, {"told": True}
    except Exception as e:
        return False, {"error": f"Codex settings unavailable: {e}", "status": 502}
