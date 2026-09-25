"""A file shared to the app, kept on the host (`POST /upload`).

The phone's share sheet hands the app a photo, a PDF, a recording; the app
sends each one here as the raw request body, named by `?name=`, and puts the
path it gets back into the words it is about to send, so the assistant can
open the file. Files land in a folder per day under ~/shared (MEDIA_UPLOAD_DIR
moves it), never over another: a second `photo.jpg` that day is `photo-2.jpg`.

The body is streamed to disk, never held in memory, and only after the
bearer has been checked: the 64 KiB cap on every other route (#139) is lifted
for this one path, to MEDIA_UPLOAD_MAX_MB (512 by default).
"""

from __future__ import annotations

import datetime as dt
import os
import re
from pathlib import Path
from typing import BinaryIO

from . import auth

CHUNK = 1 << 20


def max_bytes() -> int:
    try:
        mb = int(os.environ.get("MEDIA_UPLOAD_MAX_MB") or 512)
    except ValueError:
        mb = 512
    return max(1, mb) << 20


def root() -> Path:
    return Path(os.path.expanduser(os.environ.get("MEDIA_UPLOAD_DIR") or "~/shared"))


_UNSAFE = re.compile(r"[\x00-\x1f\x7f/\\:*?\"<>|]+")


def safe_name(name: str) -> str:
    """The last part of `name`, with nothing a shell or a path would trip on,
    no leading dot (a shared `.bashrc` is not a hidden file here), and short."""
    name = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = _UNSAFE.sub("-", name).strip(" .-")
    if not name:
        return "shared"
    if len(name) > 120:
        stem, dot, ext = name.rpartition(".")
        name = (stem[:110] + "." + ext[:9]) if dot and 0 < len(ext) <= 9 else name[:120]
    return name


def _free(folder: Path, name: str) -> Path:
    p = folder / name
    if not p.exists():
        return p
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, ext = name, ""
    n = 2
    while True:
        p = folder / (f"{stem}-{n}.{ext}" if ext else f"{stem}-{n}")
        if not p.exists():
            return p
        n += 1


def save(body: BinaryIO, length: int, name: str, bearer: str,
         today: dt.date | None = None) -> tuple[bool, dict]:
    """Write `length` bytes of `body` as `name` in today's folder."""
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    if length <= 0:
        return False, {"error": "nothing sent (Content-Length is required)", "status": 411}
    if length > max_bytes():
        return False, {"error": f"too large (the limit is {max_bytes() >> 20} MB)", "status": 413}
    folder = root() / (today or dt.date.today()).isoformat()
    folder.mkdir(parents=True, exist_ok=True)
    final = _free(folder, safe_name(name))
    part = folder / f".{final.name}.part"
    left = length
    try:
        with open(part, "wb") as out:
            while left > 0:
                chunk = body.read(min(CHUNK, left))
                if not chunk:
                    break
                out.write(chunk)
                left -= len(chunk)
        if left:
            raise OSError(f"the upload stopped {left} bytes short")
        final = _free(folder, final.name)
        os.replace(part, final)
    except OSError as e:
        part.unlink(missing_ok=True)
        return False, {"error": str(e), "status": 400}
    return True, {"path": str(final), "name": final.name, "size": length}
