#!/data/data/com.termux/files/usr/bin/env python3
"""The phone's music cache, read-only over HTTP on loopback, for Sasonica.

Sasonica's media player (sasonica-app Media3Music) cannot read Termux's files,
and play-local has already downloaded the track into Termux's cache on this
phone. Sending it to red5 so red5 could serve it back was measured on 28 Sep
2026 at about 8 KB/s over the phone's link: a 52 MB mix would have taken over
an hour to start. From here it is a loopback read.

GET/HEAD with Range (ExoPlayer seeks with it), for files directly in the cache
dir only: no subdirectories, no dotfiles (a download in progress is
``.<id>.mka.part``), no sidecars. Bound to 127.0.0.1, so only apps on this
phone can reach it; what they can read is music already downloaded here.

Env: MEDIA_MUSIC_FILES_PORT (6616), MEDIA_MUSIC_LOCAL_CACHE
(.cache/music-offline, $HOME-relative, as play-local and agent-media use).
"""

from __future__ import annotations

import http.server
import mimetypes
import os
import re
import urllib.parse

HOME = os.path.expanduser("~")
ROOT = os.path.join(HOME, os.environ.get("MEDIA_MUSIC_LOCAL_CACHE", ".cache/music-offline"))
PORT = int(os.environ.get("MEDIA_MUSIC_FILES_PORT", "6616"))
SKIP = (".title", ".json", ".part")
TYPES = {".mka": "audio/x-matroska", ".webm": "audio/webm", ".opus": "audio/ogg",
         ".m4a": "audio/mp4", ".mp3": "audio/mpeg", ".ogg": "audio/ogg", ".flac": "audio/flac"}
RANGE = re.compile(r"bytes=(\d*)-(\d*)$")


def _file(path: str) -> str | None:
    name = urllib.parse.unquote(urllib.parse.urlsplit(path).path).lstrip("/")
    if not name or "/" in name or name.startswith(".") or name.endswith(SKIP):
        return None
    full = os.path.join(ROOT, name)
    return full if os.path.isfile(full) else None


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_HEAD(self) -> None:
        self._serve(body=False)

    def do_GET(self) -> None:
        self._serve(body=True)

    def _serve(self, body: bool) -> None:
        full = _file(self.path)
        if not full:
            self.send_error(404)
            return
        size = os.path.getsize(full)
        start, end = 0, size - 1
        m = RANGE.match(self.headers.get("Range", "").strip())
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        ext = os.path.splitext(full)[1].lower()
        self.send_header("Content-Type", TYPES.get(ext) or mimetypes.guess_type(full)[0]
                         or "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        if not body:
            return
        with open(full, "rb") as f:
            f.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    chunk = f.read(min(256 * 1024, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass    # the player seeked away; it opens another request

    def log_message(self, fmt: str, *args) -> None:
        pass


def main() -> None:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.daemon_threads = True
    print(f"music-files: {ROOT} on 127.0.0.1:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
