"""Shared bits of talking to Audiobookshelf.

Both daemons loaded the same env file and wrote the same REST helper, in two
copies, in two files nothing tracked. One copy now, and the parts worth testing
are functions rather than a loop: which library, which local file, and whether
a session is actually playing.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path
from typing import Optional

#: Config lives with the rest of agent-media's, not beside the code — it holds
#: an API token, and the daemons are installed from a package.
CONFIG = Path("~/.config/agent-media/abs-bridge.env").expanduser()


def load_env(path: Path = CONFIG) -> None:
    """Layer the config file under the real environment.

    `setdefault`, so a value passed in by the unit or by hand wins over the
    file — the same precedence every other config surface here uses.
    """
    try:
        text = path.read_text()
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def log(*a) -> None:
    print(time.strftime("%H:%M:%S"), *a, flush=True)


class Abs:
    """The REST calls these daemons make, and nothing more."""

    def __init__(self, url: str = "", token: str = "", timeout: float = 10.0):
        self.url = (url or os.environ.get("ABS_URL", "http://127.0.0.1:13378")).rstrip("/")
        self.token = token or os.environ.get("ABS_TOKEN", "")
        self.timeout = timeout

    def req(self, method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method)
        req.add_header("Authorization", "Bearer " + self.token)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            raw = r.read()
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            # Some endpoints (the progress PATCH among them) answer with a bare
            # status and no body.
            return {}


def pick_library(libs: list, want: str = "") -> Optional[str]:
    """The library id to work in: the named one, else the first book one.

    Pure, because "which library" is the setting most likely to be wrong on a
    server with several, and a wrong answer is silent — positions get pushed
    against items nobody is playing.
    """
    if not libs:
        return None
    if want:
        for lib in libs:
            if want in (lib.get("id"), lib.get("name")):
                return lib["id"]
    for lib in libs:
        if lib.get("mediaType") == "book":
            return lib["id"]
    return libs[0].get("id")


def basename_map(items: list, *, expand=None) -> dict:
    """basename(audio file) -> {id, duration, offset, part} for a page of items.

    Matching is by basename because the two sides disagree about the prefix and
    always will: mpv sees `/home/ryer/audiobooks/X.m4b`, ABS (in a container)
    records `/audiobooks/X.m4b`. The filename is the only part both agree on.

    `duration` is the whole *item*; `offset` is where this file starts inside
    it and `part` how long the file itself is. For the single-file book that is
    0.0 and None, and every caller then behaves as it always did — but a book
    in six files is six basenames with six offsets, which is what makes a
    position in one of them mean anything to Audiobookshelf.

    A library listing carries no `audioFiles` at all (measured on 2.35.1: the
    media object is metadata, `numAudioFiles` and counts), so a book that is a
    *folder* has no filename in it to match — its `relPath` is the folder's.
    Those were silently invisible to the bridge: nothing pushed, nothing
    pulled. `expand(item_id) -> [track]` is how the caller offers to go and
    ask; it is called only for a folder item, which is one request per
    multi-part book per map refresh.
    """
    out: dict = {}
    for it in items:
        media = it.get("media") or {}
        total = media.get("duration")

        def _entry(offset=0.0, part=None):
            return {"id": it.get("id"), "duration": total,
                    "offset": float(offset or 0.0), "part": part}

        files = media.get("audioFiles") or []
        if not files and expand is not None and not it.get("isFile", True):
            try:
                files = expand(it.get("id")) or []
            except Exception:  # noqa: BLE001 — a book we cannot expand is a book we skip
                files = []
        for af in files:
            p = (af.get("metadata") or {}).get("path") or af.get("path") or ""
            if p:
                out[os.path.basename(p)] = _entry(af.get("startOffset"),
                                                  af.get("duration"))
        rel = (it.get("relPath") or "").strip("/")
        if rel:
            out.setdefault(os.path.basename(rel), _entry())
    return out


def local_path(container_path: str, *, lib: Optional[Path] = None) -> Optional[Path]:
    """An ABS path, as this host can open it.

    ABS runs in a container and reports its own view (`/audiobooks/X.m4b`); the
    book channel needs the host's (`~/audiobooks/X.m4b`). Only the basename is
    trusted — a path from the server is not a path to hand a player unchecked,
    and everything the rooms can play is in the one library directory anyway.
    """
    if not container_path:
        return None
    root = lib or Path(os.environ.get("AUDIOBOOK_LIB")
                       or os.environ.get("MEDIA_AUDIOBOOK_ABS_DIR")
                       or "~/audiobooks").expanduser()
    p = root / os.path.basename(container_path)
    return p if p.is_file() else None
