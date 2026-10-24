"""Signed links to rendered speech clips, for a player off the tailnet.

The phone fetches a server-rendered clip without a credential of its own
(ClipCache sends no bearer), so a clip link handed to it over the public
route carries one: `?sig=` is an HMAC of the clip's file name under a key
kept on this host. A link opens that one clip and nothing else; the key
never leaves `<state_dir>/clip-sign.key`. Turned on per target with
`MEDIA_SPEECH_CLIP_SIGNED_<TARGET>=1` (sinks/speech.py `_clip_uri_for`);
the server's `GET /speech/clips/<name>` checks it (David, 24 Oct 2026:
Natasha refused on the phone, rendered on red5 instead, and the phone off
the tailnet).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from pathlib import Path
from typing import Optional

from ._paths import speech_audio_dir, state_dir

# A clip's file name as the renderer writes it: no slashes, no dot-dot.
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,200}")
SUFFIXES = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg"}


def _key_path() -> Path:
    return state_dir() / "clip-sign.key"


def _key() -> bytes:
    path = _key_path()
    try:
        return bytes.fromhex(path.read_text().strip())
    except (OSError, ValueError):
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_hex(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, key.encode())
    except BaseException:
        os.close(fd)
        raise
    os.close(fd)
    return bytes.fromhex(key)


def sign(name: str) -> str:
    return hmac.new(_key(), name.encode(), hashlib.sha256).hexdigest()[:32]


def signed(base: str, name: str) -> str:
    return f"{base.rstrip('/')}/{name}?sig={sign(name)}"


def resolve(name: str, sig: str) -> Optional[tuple[Path, str]]:
    """The clip's file and content type when the link is good, else None."""
    if not NAME.fullmatch(name or "") or ".." in name:
        return None
    ctype = SUFFIXES.get(Path(name).suffix.lower())
    if not ctype or not sig or not hmac.compare_digest(sign(name), sig):
        return None
    f = speech_audio_dir() / name
    return (f, ctype) if f.is_file() else None
