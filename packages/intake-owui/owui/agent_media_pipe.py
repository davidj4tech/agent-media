"""agent-media — an Open WebUI *Pipe* function.

Install: OWUI → Admin → Functions → “+” → paste this file → enable. It adds a
selectable model called “agent-media”. Pick it, then anything you SAY (via
OWUI's mic / dictation) or TYPE is injected into your agent's session — the
reply comes back as agent-media voice + canvas, not in this thread.

This is the input half of the OWUI↔agent-media bridge: it reuses the canvas
server's existing `/input` keystroke-injection endpoint (`POST /input`,
`X-Auth-Token: <amux token>`), so there is no second injection path to keep in
sync. The Pipe runs inside OWUI's server, so it reaches the canvas directly at
127.0.0.1:8781 — this hop does NOT go through Caddy.

The canvas answers `/input` with `{"ok": bool, "detail": str}` — `detail` is
the resolved destination (e.g. `amux:foo`, the session name) on success, or the
reason it couldn't send (e.g. "no speaker on record yet") on failure — and the
Pipe echoes it back into the thread so a fire-and-forget turn still gets an ack.

id: agent_media
title: agent-media (talk to the agent)
author: agent-media
version: 0.1.0
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from pydantic import BaseModel, Field


class Pipe:
    class Valves(BaseModel):
        canvas_url: str = Field(
            default="http://127.0.0.1:8781",
            description="Base URL of the agent-media canvas server (its /input endpoint).",
        )
        amux_token: str = Field(
            default="",
            description="amux auth token (from `media-intake-owui pair`). "
                        "Leave blank only if the canvas runs with MEDIA_VISUAL_TRUST_TAILNET=1.",
        )
        default_target: str = Field(
            default="speaker",
            description="Where to inject: 'speaker' (last-speaking agent) or 'amux:<session>'.",
        )
        acknowledge: bool = Field(
            default=True,
            description="Echo a one-line '↳ sent to …' confirmation into the OWUI thread.",
        )
        timeout_s: float = Field(default=8.0, description="HTTP timeout to the canvas server.")

    def __init__(self) -> None:
        self.valves = self.Valves()

    # OWUI lists this Pipe as a model of this id/name.
    def pipes(self) -> list[dict]:
        return [{"id": "agent-media", "name": "agent-media"}]

    @staticmethod
    def _last_user_text(body: dict) -> str:
        """Extract the newest user turn — handles both the plain-string and the
        multimodal (list-of-parts) content shapes OWUI may send."""
        for msg in reversed(body.get("messages") or []):
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if isinstance(content, str):
                return content.strip()
            if isinstance(content, list):  # multimodal: keep the text parts
                parts = [p.get("text", "") for p in content
                         if isinstance(p, dict) and p.get("type") == "text"]
                return "\n".join(parts).strip()
            return ""
        return ""

    @staticmethod
    def _detail(raw: bytes) -> str:
        """Best-effort pull of the canvas's `detail` field from a JSON body."""
        try:
            return str((json.loads(raw or b"{}") or {}).get("detail", "") or "")
        except (ValueError, json.JSONDecodeError):
            return ""

    def pipe(self, body: dict) -> str:
        v = self.valves
        text = self._last_user_text(body)
        if not text:
            return "⚠️ agent-media: no text to send."

        target = (body.get("target") or v.default_target or "speaker")
        payload = json.dumps({"text": text, "target": target}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if v.amux_token:
            headers["X-Auth-Token"] = v.amux_token

        req = urllib.request.Request(
            v.canvas_url.rstrip("/") + "/input",
            data=payload, headers=headers, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=v.timeout_s) as resp:
                detail = self._detail(resp.read())
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return ("🔒 agent-media: canvas rejected the token — run "
                        "`media-intake-owui pair` and update the amux_token valve.")
            # The canvas returns 400 with a `detail` for a send it couldn't
            # route ("no speaker on record yet", "unknown amux session …") —
            # surface that instead of a bare status code.
            detail = self._detail(e.read())
            if e.code == 400 and detail:
                return f"⚠️ agent-media: {detail}."
            return f"⚠️ agent-media: canvas returned HTTP {e.code}."
        except (urllib.error.URLError, OSError) as e:
            return f"⚠️ agent-media: can't reach the canvas at {v.canvas_url} ({e})."

        if not v.acknowledge:
            return ""
        return f"↳ sent to {detail or target}"
