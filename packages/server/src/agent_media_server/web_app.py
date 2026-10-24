"""The Sasonica web app, served by this server at `/app/` (roadmap item 21).

An https page cannot call a plain-http LAN or tailnet address, so a browser on
the Wi-Fi reaches a server's app only if the server hands out the app itself:
the sasonica-app bundle built for `/app/` (`SASONICA_BASE=/app/ pnpm build`,
its `build-app/client`), served from the same origin it then talks to — so no
CORS, and the app's default server is the page's own origin.

Where the bundle is: MEDIA_WEB_APP_DIR, else the copy the binary build puts
beside this module (`web_app/`, deploy/binary/build.sh). A directory without
an index.html is no bundle, and then `/app/` is the caller's 404.

What it answers mirrors sasonica-app's serve.mjs: a file under the bundle as
itself; anything else under `/app/` is a route the SPA owns, so index.html.
Hashed `assets/` are kept a year (immutable), everything else is no-cache, so
a new bundle is picked up on the next load. A missing `assets/` file is a 404
rather than index.html, which a browser would only fail to run as a script.
Text is gzipped when the caller takes it (the largest chunk is ~490 KB).

`/app/dl/` is served from its own folder, not the bundle: a one-off app
download (an APK link handed to the user) put inside the bundle went with the
next build, which replaces the bundle's whole folder (David, 25 Oct 2026: "that
download link gave me a 404"). MEDIA_WEB_DL_DIR, else `app-dl` in the state
dir. A file there is itself; anything else under `/app/dl/` is a 404, never
index.html, so a gone link says so instead of opening the app.

Open on every listener, the public one too (contract §19): it is the same
static files app.sasonica.com will host, carries no credential, and a server
reached through its tunnel then has a web app at `<tunnel>/app/` as well.
"""

from __future__ import annotations

import gzip
import os
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import unquote

PREFIX = "/app"

_TYPES = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript", ".mjs": "text/javascript",
    ".css": "text/css", ".json": "application/json", ".svg": "image/svg+xml",
    ".png": "image/png", ".ico": "image/x-icon", ".woff2": "font/woff2",
    ".woff": "font/woff", ".map": "application/json", ".txt": "text/plain; charset=utf-8",
    ".webmanifest": "application/manifest+json", ".webp": "image/webp",
    ".apk": "application/vnd.android.package-archive",
}
_GZIP = (".html", ".js", ".mjs", ".css", ".json", ".svg", ".map", ".webmanifest", ".txt")
_IMMUTABLE = "public, max-age=31536000, immutable"

# Gzipped bodies by (path, mtime, size): the bundle is a few dozen files, and
# compressing the big chunk on every load is the one cost worth keeping.
_ZCACHE: dict[tuple[str, float, int], bytes] = {}
_ZLOCK = threading.Lock()


def bundle_dir() -> Path | None:
    """The bundle being served, or None when there is none."""
    env = os.environ.get("MEDIA_WEB_APP_DIR", "").strip()
    d = Path(env).expanduser() if env else Path(__file__).with_name("web_app")
    return d if (d / "index.html").is_file() else None


def dl_dir() -> Path:
    """Where `/app/dl/` files live: outside the bundle, so a build keeps them."""
    env = os.environ.get("MEDIA_WEB_DL_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    from agent_media_core._paths import state_dir
    return state_dir() / "app-dl"


def _dl(h: BaseHTTPRequestHandler, rel: str) -> None:
    root = dl_dir().resolve()
    try:
        p = (root / rel).resolve()
    except (OSError, ValueError):
        p = root
    if p != root and root in p.parents and p.is_file():
        _file(h, p, "no-store")
    else:
        _send(h, 404, b"not found\n", {"Content-Type": "text/plain", "Cache-Control": "no-store"})


def serving() -> bool:
    return bundle_dir() is not None


def _send(h: BaseHTTPRequestHandler, code: int, body: bytes, headers: dict[str, str]) -> None:
    h.send_response(code)
    for k, v in headers.items():
        h.send_header(k, v)
    h.send_header("Content-Length", str(len(body)))
    h.end_headers()
    if h.command != "HEAD":
        h.wfile.write(body)


def _file(h: BaseHTTPRequestHandler, p: Path, cache: str) -> None:
    ext = p.suffix.lower()
    st = p.stat()
    headers = {"Content-Type": _TYPES.get(ext, "application/octet-stream"),
               "Cache-Control": cache, "X-Content-Type-Options": "nosniff"}
    accepts = "gzip" in (h.headers.get("Accept-Encoding") or "").lower()
    if ext in _GZIP and accepts and st.st_size > 1024:
        key = (str(p), st.st_mtime, st.st_size)
        with _ZLOCK:
            body = _ZCACHE.get(key)
        if body is None:
            body = gzip.compress(p.read_bytes(), 6)
            with _ZLOCK:
                if len(_ZCACHE) > 256:
                    _ZCACHE.clear()
                _ZCACHE[key] = body
        headers["Content-Encoding"] = "gzip"
        headers["Vary"] = "Accept-Encoding"
    else:
        body = p.read_bytes()
    _send(h, 200, body, headers)


def serve(h: BaseHTTPRequestHandler, path: str) -> bool:
    """Answer a GET under `/app` if a bundle is served. False otherwise, for
    the caller's 404. `path` is the request path without its query."""
    if path != PREFIX and not path.startswith(PREFIX + "/"):
        return False
    root = bundle_dir()
    if root is None:
        return False
    if path == PREFIX:
        # Relative asset and route URLs need the trailing slash.
        query = h.path.partition("?")[2]
        _send(h, 301, b"", {"Location": PREFIX + "/" + (f"?{query}" if query else ""),
                            "Cache-Control": "no-cache"})
        return True
    rel = unquote(path[len(PREFIX) + 1:])
    if rel.startswith("dl/"):
        _dl(h, rel[3:])
        return True
    root = root.resolve()
    try:
        p = (root / rel).resolve()
    except (OSError, ValueError):
        p = root
    inside = p != root and root in p.parents
    if inside and p.is_file():
        _file(h, p, _IMMUTABLE if rel.startswith("assets/") else "no-cache")
    elif rel.startswith("assets/") or (not inside and p != root):
        # A hashed chunk the bundle does not have (an old page after an
        # update), or a path that climbed out of the bundle.
        _send(h, 404, b"not found\n", {"Content-Type": "text/plain", "Cache-Control": "no-store"})
    else:
        _file(h, root / "index.html", "no-cache")
    return True
