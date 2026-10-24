"""Thread tags, kept in the thread's own org-roam node.

Every thread already has a node: agent-sessions exports each conversation to
`~/org/roam/sessions/**/<denote name>.org`, a property drawer with
`:AGENT_SID:` and a `#+filetags:` line. Once that file exists the exporter
keeps its filetags as they are (agent-sessions `export.write_session`), so a
tag added there — by the app through this server, or by hand in Emacs —
survives every re-export, and org-roam, the agenda and Denote see it too
(the exporter puts the filetags into the file name on its next pass). So
there is no store here: the org file is the record, and this module reads and
edits the one line.

The exporter writes tags of its own into the same line: the harness
(`claude`, `pi`, ...), `inbox` until the note is filed, `autonomous`, and the
project hub's key — the `<key>` in the node's `- Project ::
[[id:<harness>-proj-<key>][<name>]]` link. Older exports (and a thread moved
since) also left the hub's name and the directory, tagified, and path tags
(`home_ryer_scratch`). Those are the exporter's, not yours: rows carry only
the rest (`tags`), and an edit leaves them where they are. So a tag of yours
cannot start with `home_`.

`GET /targets` and `/conversations` rows carry `tags: [...]` (the thread's own,
in file order); `GET /tags` lists every tag in use with its count, for a
filter and for suggestions; `POST /session/tags {"session", "add"?, "remove"?,
"tags"?}` edits one thread's — `tags` replaces the set, `add` and `remove`
change it. A thread with no node yet (a conversation newer than the last
export, which runs every few minutes) is 409: there is nothing to edit until
the exporter has written it, and writing a node here would mean guessing the
exporter's own tags.

`MEDIA_THREAD_TAGS_DIR` moves the root (default `~/org/roam/sessions`).
"""

from __future__ import annotations

import os
import re
import tempfile
import threading
from pathlib import Path

#: Tags the exporter writes, whatever the thread.
EXPORTER_TAGS = frozenset({"inbox", "autonomous", "claude", "pi", "codex", "openclaw",
                           "gemini", "grok", "hermes"})
#: An org tag: letters, digits, `_`, `@`, `#`, `%`.
_TAG = re.compile(r"[\w@#%]+")
MAX_TAG_LEN = 40
MAX_TAGS = 20

_SID = re.compile(r"^:AGENT_SID:\s*(\S+)", re.M)
_HARNESS = re.compile(r"^:AGENT_HARNESS:\s*(\S+)", re.M)
_HUB = re.compile(r"^- Project :: \[\[id:[^\]]*?-proj-([^\]]+)\](?:\[([^\]]*)\])?", re.M)
_CWD = re.compile(r"^:AGENT_CWD:\s*(\S.*?)\s*$", re.M)
#: A tag the exporter made from a path.
_PATH_TAG = re.compile(r"home_\w*")


def _tagify(s: str) -> str:
    """agent-sessions `roam.tagify`, for recognising the exporter's tags."""
    t = re.sub(r"[^A-Za-z0-9_@#%]", "_", (s or "").strip())
    return re.sub(r"_+", "_", t).strip("_").lower()
_FILETAGS = re.compile(r"^#\+filetags:[ \t]*(.*)$", re.M | re.I)

_lock = threading.Lock()
# path -> ((mtime_ns, size), node); one stat per file once warm.
_cache: dict[str, tuple[tuple[int, int], dict | None]] = {}


def root() -> Path:
    raw = os.environ.get("MEDIA_THREAD_TAGS_DIR", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / "org" / "roam" / "sessions"


def _head(text: str) -> str:
    """The file's header: everything before its first heading."""
    m = re.search(r"^\* ", text, re.M)
    return text[:m.start()] if m else text


def _split(line: str) -> list[str]:
    return [t for t in line.strip().strip(":").split(":") if t.strip()]


def _parse(text: str) -> dict | None:
    head = _head(text)
    m = _SID.search(head)
    if not m:
        return None
    ft = _FILETAGS.search(head)
    every = _split(ft.group(1)) if ft else []
    auto = set(EXPORTER_TAGS)
    h = _HARNESS.search(head)
    if h:
        auto.add(h.group(1))
    hub = _HUB.search(head)
    if hub:
        auto.update({hub.group(1), _tagify(hub.group(2) or "")})
    cwd = _CWD.search(head)
    if cwd:
        auto.update({_tagify(cwd.group(1)), _tagify(os.path.basename(cwd.group(1).rstrip("/")))})
    return {"session": m.group(1), "every": every,
            "tags": [t for t in every if t not in auto and not _PATH_TAG.fullmatch(t)]}


def _nodes() -> dict[str, list[tuple[Path, dict]]]:
    """session -> its node(s), read through the per-file cache."""
    base = root()
    out: dict[str, list[tuple[Path, dict]]] = {}
    seen: set[str] = set()
    if not base.is_dir():
        return out
    with _lock:
        for dirpath, dirnames, files in os.walk(base):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in files:
                if not name.endswith(".org"):
                    continue
                p = os.path.join(dirpath, name)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                key = (st.st_mtime_ns, st.st_size)
                hit = _cache.get(p)
                if hit is None or hit[0] != key:
                    try:
                        with open(p, encoding="utf-8", errors="replace") as f:
                            node = _parse(f.read(4096))
                    except OSError:
                        continue
                    _cache[p] = hit = (key, node)
                seen.add(p)
                if hit[1]:
                    out.setdefault(hit[1]["session"], []).append((Path(p), hit[1]))
        for gone in set(_cache) - seen:
            _cache.pop(gone, None)
    return out


def tags_by_session() -> dict[str, list[str]]:
    """Every thread's own tags, by session id (threads with none left out)."""
    out: dict[str, list[str]] = {}
    for sid, found in _nodes().items():
        merged: list[str] = []
        for _, node in found:
            merged += [t for t in node["tags"] if t not in merged]
        if merged:
            out[sid] = merged
    return out


def add_tags(rows: list[dict]) -> list[dict]:
    """Give each row `tags`, in place. A sealed row gets none: it says
    nothing of its thread but where it was."""
    try:
        by = tags_by_session()
    except Exception:  # noqa: BLE001 — a thread list never fails on tags
        by = {}
    for r in rows:
        r["tags"] = [] if r.get("sealed") else list(by.get(str(r.get("session") or ""), []))
    return rows


def counts() -> list[dict]:
    """`[{"tag", "count"}]`, most used first, then by name."""
    n: dict[str, int] = {}
    for tags in tags_by_session().values():
        for t in tags:
            n[t] = n.get(t, 0) + 1
    return [{"tag": t, "count": c} for t, c in sorted(n.items(), key=lambda kv: (-kv[1], kv[0]))]


def clean(raw) -> str | None:
    """A tag as the app typed it, made an org tag (spaces and dashes become
    `_`, a leading `#` or `:` dropped), or None when nothing is left."""
    if not isinstance(raw, str):
        return None
    t = re.sub(r"[\s\-]+", "_", raw.strip().lstrip("#:").rstrip(":"))
    if not t or len(t) > MAX_TAG_LEN or not _TAG.fullmatch(t) or _PATH_TAG.fullmatch(t):
        return None
    return t


def _rewrite(path: Path, want: list[str]) -> None:
    """Set the node's own tags to `want`, keeping the exporter's tags first and
    in place, and every other byte of the file as it was."""
    text = path.read_text(encoding="utf-8", errors="replace")
    node = _parse(text)
    if node is None:
        raise OSError(f"{path.name} is not a thread node")
    own = set(node["tags"])
    keep = [t for t in node["every"] if t not in own]
    every = keep + [t for t in want if t not in keep]
    line = "#+filetags: " + (":" + ":".join(every) + ":" if every else "")
    head = _head(text)
    m = _FILETAGS.search(head)
    if m:
        text = text[:m.start()] + line + text[m.end():]
    else:
        # After `#+title:` when there is one, else at the end of the header.
        t = re.search(r"^#\+title:.*$", head, re.M | re.I)
        at = t.end() if t else len(head.rstrip("\n"))
        text = text[:at] + "\n" + line + text[at:]
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tags-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, path.stat().st_mode & 0o777)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _tag_list(raw, field: str) -> tuple[list[str] | None, str | None]:
    if raw is None:
        return [], None
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return None, f"{field} must be a list of tags"
    out: list[str] = []
    for r in raw:
        t = clean(r)
        if t is None:
            return None, f"not a tag: {str(r)[:40]!r}"
        if t not in out:
            out.append(t)
    return out, None


def session_tags(session: str, body: dict, bearer: str) -> tuple[bool, dict]:
    """`POST /session/tags`. Gated like `/session/pin`. 400 for a bad id or a
    bad tag, 409 when the thread has no org node yet."""
    from . import auth, sessions

    session = (session or "").strip()
    if not sessions._SESSION.fullmatch(session):
        return False, {"error": "not a session id", "status": 400}
    replace = body.get("tags")
    whole, err = _tag_list(replace, "tags")
    add, err2 = _tag_list(body.get("add"), "add")
    drop, err3 = _tag_list(body.get("remove"), "remove")
    if err or err2 or err3:
        return False, {"error": err or err2 or err3, "status": 400}
    if replace is None and not add and not drop:
        return False, {"error": "give tags, add or remove", "status": 400}
    user, gerr = auth.gate(bearer)
    if not user:
        return False, gerr
    from agent_media_core.sealed import is_sealed

    if is_sealed(session):
        return False, {"error": "a sealed thread has no tags", "status": 423}
    found = _nodes().get(session)
    if not found:
        return False, {"error": "this thread is not in your org files yet; "
                                "try again in a few minutes", "status": 409}
    current: list[str] = []
    for _, node in found:
        current += [t for t in node["tags"] if t not in current]
    want = list(whole) if replace is not None else list(current)
    want += [t for t in add if t not in want]
    want = [t for t in want if t not in drop]
    if len(want) > MAX_TAGS:
        return False, {"error": f"at most {MAX_TAGS} tags", "status": 400}
    if want != current:
        try:
            for path, _ in found:
                _rewrite(path, want)
        except OSError as e:
            return False, {"error": f"could not save the tags ({e})", "status": 500}
    return True, {"session": session, "tags": want}


def tags_list(bearer: str) -> tuple[bool, dict]:
    """`GET /tags`. Gated like `/conversations`."""
    from . import auth

    user, err = auth.gate(bearer)
    if not user:
        return False, err
    return True, {"tags": counts()}
