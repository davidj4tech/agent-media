"""One SMS from the owner's own number, through the Google Messages bridge.

`media sms send --to +61… [--txn ID]`, the text on stdin: any producer (a
booking mirror, a reminder) texts through mautrix-gmessages, the bridge the
Messages tab uses, without knowing Matrix. The number is used for that one
send and never logged, stored or printed. `--txn` makes a retry after an
unclear failure the same message, not a second one.

Config, `[sms]`: `bridge` ("gmessages", the bridge's appservice id),
`login_id` (the bridge login to send from; default its first), `matrix_env`
(the file with MATRIX_HOMESERVER / MATRIX_OWNER_TOKEN / MATRIX_OWNER_ID when
the process environment lacks them; default ~/.config/agent-media/matrix.env).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid

DEFAULTS = {"bridge": "gmessages", "login_id": "", "matrix_env": "~/.config/agent-media/matrix.env"}
E164 = re.compile(r"\+[1-9]\d{7,14}")


class SmsError(RuntimeError):
    """A text not sent. Its message never carries a number or a name."""


def settings() -> dict:
    from agent_media_core import config
    got = config.load().get("sms") or {}
    return {**DEFAULTS, **(got if isinstance(got, dict) else {})}


def _env_file(path: str) -> dict:
    out = {}
    try:
        lines = Path(path).expanduser().read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if line.startswith("export "):
            line = line[7:]
        k, sep, v = line.partition("=")
        if sep and re.fullmatch(r"[A-Z_][A-Z0-9_]*", k):
            out[k] = v.strip().strip("'\"")
    return out


class GmessagesSender:
    """One SMS from the owner's number through mautrix-gmessages (bridgev2):

    1. `POST /_matrix/provision/v3/create_dm/<+61…>?user_id=<owner>` on the
       bridge's provisioning API (bearer = its `provisioning.shared_secret`,
       found as bridges.py finds it) starts (or finds) the chat with that
       number and answers its portal room, `dm_room_mxid`.
    2. The owner joins it (a no-op when already in) and sends the text there
       with the owner's token (`MATRIX_OWNER_TOKEN`), as the Messages tab does
       (matrix.Room.send); the bridge sends it as an SMS from the owner's phone.
       The transaction id is the text's own, so a retry after an unclear
       failure is the same message, not a second one.

    Unverified against the live bridge (22 Oct 2026): the identifier format
    gmessages' ResolveIdentifier takes (E.164 is sent) and whether a new
    portal invites the owner (the join covers it when it does)."""

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def _env(self) -> dict:
        env = _env_file(self.cfg.get("matrix_env") or DEFAULTS["matrix_env"])
        env.update({k: v for k, v in os.environ.items() if k.startswith("MATRIX_") and v})
        return env

    @staticmethod
    def _http(method: str, url: str, token: str, body: dict | None, what: str, timeout=60) -> dict:
        data = json.dumps(body).encode() if body is not None else b""
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Authorization": f"Bearer {token}",
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                got = json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            raise SmsError(f"{what} refused it (HTTP {e.code})") from None
        except (urllib.error.URLError, OSError, ValueError):
            raise SmsError(f"{what} could not be reached") from None
        return got if isinstance(got, dict) else {}

    def send(self, number: str, body: str, txn: str) -> str:
        from agent_media_core import matrix
        from . import bridges
        env = self._env()
        token, owner = env.get("MATRIX_OWNER_TOKEN") or "", matrix.owner_of(env)
        homeserver = (env.get("MATRIX_HOMESERVER") or "").rstrip("/")
        if not token or not owner or not homeserver:
            raise SmsError("No Matrix owner login for texts (MATRIX_OWNER_TOKEN, MATRIX_OWNER_ID, "
                           "MATRIX_HOMESERVER; run matrix-owner-login)")
        bridge = bridges.find(self.cfg.get("bridge") or "gmessages")
        if bridge is None:
            raise SmsError("The Google Messages bridge was not found (its provisioning secret)")
        q = {"user_id": owner}
        if self.cfg.get("login_id"):
            q["login_id"] = self.cfg["login_id"]
        url = (f"{bridge.url}/_matrix/provision/v3/create_dm/"
               f"{urllib.parse.quote(number, safe='')}?{urllib.parse.urlencode(q)}")
        room = self._http("POST", url, bridge.secret, None, "The Google Messages bridge").get("dm_room_mxid")
        if not room:
            raise SmsError("The Google Messages bridge gave no chat for that number")
        r = urllib.parse.quote(room, safe="")
        api = f"{homeserver}/_matrix/client/v3"
        self._http("POST", f"{api}/rooms/{r}/join", token, {}, "The homeserver")
        got = self._http("PUT", f"{api}/rooms/{r}/send/m.room.message/{urllib.parse.quote(txn, safe='')}",
                         token, {"msgtype": "m.text", "body": body}, "The homeserver")
        return str(got.get("event_id") or "")


def _sender(cfg: dict):
    return GmessagesSender(cfg)


def send(to: str, body: str, txn: str = "") -> dict:
    """Text `body` to `to` (E.164, +…). SmsError says why not, without the number."""
    if not E164.fullmatch(str(to or "")):
        raise SmsError("--to: a number in international form, +61…")
    if not str(body or "").strip():
        raise SmsError("Nothing to send (the text comes on stdin)")
    txn = txn or f"sms-{uuid.uuid4().hex}"
    return {"ok": True, "event_id": _sender(settings()).send(to, body, txn), "txn": txn}
