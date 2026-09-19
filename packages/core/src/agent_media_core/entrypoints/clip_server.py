"""agent-media-clips: the clip server on :8780, with byte ranges.

``python3 -m http.server`` served this directory for its first two months, and
it answers every request with the whole file: a ``Range`` header is ignored and
the reply is ``200``. For speech clips that never mattered (a clip is small and
played from the start). For music it does. Sasonica's ExoPlayer reads a
Matroska file's head, seeks to the index at the end and comes back, and each of
those seeks fetched all 3.4 MB again. Over the tailnet that was 35 seconds of
"buffering" before the first note, and every seek after it would cost the
same (2026-09-19).

Same command line as the module it replaces (``PORT --bind ADDR --directory
DIR``), stdlib only, so the unit can keep running it with the system python
straight from the checkout. One range per request. Multiple ranges get the
whole file, as they do in ``feed_server``, since no player asks for them.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import io
import os
import shutil


class _Window(io.RawIOBase):
    """A file, readable only from `start` for `length` bytes."""

    def __init__(self, fh, start: int, length: int) -> None:
        self._fh = fh
        self._left = length
        fh.seek(start)

    def readable(self) -> bool:
        return True

    def readinto(self, buf) -> int:
        if self._left <= 0:
            return 0
        n = self._fh.readinto(memoryview(buf)[: min(len(buf), self._left)])
        self._left -= n or 0
        return n or 0

    def close(self) -> None:
        self._fh.close()
        super().close()


def parse_range(header: str, size: int):
    """``(start, end)`` inclusive; ``(None, None)`` = no range, send the whole
    file; ``(None, -1)`` = unsatisfiable (416)."""
    raw = (header or "").strip()
    if not raw.startswith("bytes=") or "," in raw:
        return None, None
    spec = raw[len("bytes="):].strip()
    try:
        if spec.startswith("-"):
            n = int(spec[1:])
            return (max(0, size - n), size - 1) if n > 0 else (None, -1)
        first, _, last = spec.partition("-")
        start = int(first)
        end = int(last) if last else size - 1
    except ValueError:
        return None, None
    if start < 0 or start >= size or end < start:
        return None, -1
    return start, min(end, size - 1)


class Handler(http.server.SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"   # keep-alive: a player makes several requests

    def end_headers(self) -> None:
        self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    def send_head(self):
        path = self.translate_path(self.path)
        if "Range" not in self.headers or not os.path.isfile(path):
            return super().send_head()
        try:
            fh = open(path, "rb")
        except OSError:
            self.send_error(404, "File not found")
            return None
        try:
            fs = os.fstat(fh.fileno())
            start, end = parse_range(self.headers["Range"], fs.st_size)
            if start is None and end == -1:
                fh.close()
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{fs.st_size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            if start is None:
                start, end, code = 0, fs.st_size - 1, 200
            else:
                code = 206
            self.send_response(code)
            self.send_header("Content-Type", self.guess_type(path))
            self.send_header("Content-Length", str(end - start + 1))
            if code == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{fs.st_size}")
            self.send_header("Last-Modified", self.date_time_string(fs.st_mtime))
            self.end_headers()
            return _Window(fh, start, end - start + 1)
        except Exception:
            fh.close()
            raise

    def copyfile(self, source, outputfile) -> None:
        try:
            shutil.copyfileobj(source, outputfile)
        except (BrokenPipeError, ConnectionResetError):
            pass   # a player that seeks hangs up mid-body; that is normal


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("port", type=int, nargs="?", default=8780)
    ap.add_argument("--bind", "-b", default="127.0.0.1")
    ap.add_argument("--directory", "-d", default=os.getcwd())
    a = ap.parse_args(argv)
    handler = functools.partial(Handler, directory=a.directory)
    with http.server.ThreadingHTTPServer((a.bind, a.port), handler) as srv:
        srv.serve_forever()


if __name__ == "__main__":
    main()
