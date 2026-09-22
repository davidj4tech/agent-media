"""Notes: browse, search and capture into the Org tree, without Emacs.

The tree is `~/org` (MEDIA_NOTES_DIR to move it), laid out the paragtd way:
the GTD files at the top (inbox, next-actions, tickler, ...) and org-roam
notes under `roam/`. Everything here reads and appends plain text; Emacs is
one editor of the same files, never a dependency. Commits are not ours
either: `org-autosync` commits and pushes the tree from every host, so a
capture only has to land in `inbox.org`.

  GET  /notes                  → {"views": [{name, label, kind, count}]}
  GET  /notes/view?name=       → {"view", "items": [...]}   (&done=1 keeps DONE)
  GET  /notes/read?path=[&at=] → {"path", "title", "text", "links"}
  GET  /notes/search?q=        → {"notes": [...], "memories": [...]}
                                 (&all=1 takes in session notes; &memory=0 skips memory)
  POST /notes/capture {"text", "kind": "todo"|"note", "memory": bool}
  POST /notes/say {"path", "at"?}  → read a note (or one heading) aloud

Setting all this up on a host is notes_setup.py (/notes/setup).

The file list and the capture template mirror paragtd's (paragtd-paths.el,
paragtd-capture.el) by hand for now; that copy is what a paragtd JSON export
would replace.

Search asks the memory store (agent-memory's Hippocampus) beside ripgrep, and
a capture is remembered there too, so a note surfaces in later recall. Both
are best-effort: the store being down costs the memory half, never the notes.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import auth

#: paragtd-core-files, with a label and what the app calls it.
GTD_FILES = (
    ("inbox", "Inbox", "inbox.org"),
    ("next", "Next actions", "next-actions.org"),
    ("waiting", "Waiting for", "waiting-for.org"),
    ("tickler", "Tickler", "tickler.org"),
    ("someday", "Someday", "someday.org"),
    ("projects", "Projects", "projects.org"),
    ("areas", "Areas", "areas.org"),
    ("routines", "Routines", "routines.org"),
)

#: org-roam folders, as shelves of notes. Sessions are the agents' notes —
#: hundreds of them — so they are a view of their own and left out of search
#: unless asked for.
ROAM_FOLDERS = (
    ("roam-projects", "Project notes", "roam/projects"),
    ("roam-people", "People", "roam/people"),
    ("roam-refs", "References", "roam/refs"),
    ("roam-notes", "Notes", "roam/notes"),
    ("roam-journal", "Journal", "roam/journal"),
    ("roam-sessions", "Agent sessions", "roam/sessions"),
)

#: Kept out of search by default: the agents' notes, the generated astro
#: alerts, and backups.
SEARCH_EXCLUDES = ("roam/sessions/**", "astro.org*", "*.bak*")

DONE_STATES = frozenset({"DONE", "CANCELLED", "CANCELED"})
STATES = ("TODO", "NEXT", "WAITING", "SOMEDAY", "DONE", "CANCELLED", "CANCELED")
NOTE_SUFFIXES = (".org", ".md", ".txt")

#: paragtd-astro-stale-days: a past astro alert older than this is not agenda.
ASTRO_STALE_DAYS = 2
AGENDA_AHEAD_DAYS = 7

MAX_READ = 256 * 1024
MAX_CAPTURE = 8 * 1024
MAX_ITEMS = 300

_HEADING = re.compile(
    r"^(\*+)\s+(?:(" + "|".join(STATES) + r")\s+)?(?:\[#([A-C])\]\s+)?(.*?)"
    r"(?:\s+(:[\w@#%:]+:))?\s*$")
_STAMP = re.compile(r"\b(SCHEDULED|DEADLINE):\s*<(\d{4}-\d{2}-\d{2})[^>]*>")
_TITLE = re.compile(r"^#\+title:\s*(.+)$", re.I | re.M)
_ID_PROP = re.compile(r"^\s*:ID:\s*(\S+)", re.M)
_ID_LINK = re.compile(r"\[\[id:([^\]]+)\](?:\[([^\]]*)\])?\]")


def root() -> Path:
    return Path(os.environ.get("MEDIA_NOTES_DIR") or "~/org").expanduser()


def _rel(p: Path) -> str:
    return p.relative_to(root()).as_posix()


def _safe_path(rel: str) -> Path | None:
    """`rel` as a note inside the tree, or None: no escaping it (`..`, a
    symlink out), no dot-dirs (.git), and only the note suffixes."""
    rel = (rel or "").strip().lstrip("/")
    if not rel or any(part.startswith(".") for part in Path(rel).parts):
        return None
    base = root().resolve()
    try:
        p = (base / rel).resolve()
        p.relative_to(base)
    except (OSError, ValueError):
        return None
    if p.suffix not in NOTE_SUFFIXES or not p.is_file():
        return None
    return p


# --- reading org --------------------------------------------------------------------

def _headings(path: Path, *, done: bool) -> list[dict]:
    """The headings of one Org file, each with its state and dates. The
    line (`at`, 1-based) is how /notes/read finds it again."""
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for i, line in enumerate(lines):
        m = _HEADING.match(line)
        if m:
            stars, state, prio, title, tags = m.groups()
            out.append({"path": _rel(path), "at": i + 1, "level": len(stars),
                        "state": state or "", "priority": prio or "",
                        "title": title.strip(),
                        "tags": [t for t in (tags or "").split(":") if t]})
        elif out and line.lstrip().startswith(("SCHEDULED", "DEADLINE")):
            for kind, date in _STAMP.findall(line):
                out[-1][kind.lower()] = date
    if not done:
        out = [h for h in out if h["state"] not in DONE_STATES]
    return out


def _title_of(path: Path) -> str:
    try:
        with path.open(errors="replace") as f:
            head = f.read(2048)
    except OSError:
        return path.stem
    m = _TITLE.search(head)
    return m.group(1).strip() if m else path.stem


def _folder_notes(folder: Path) -> list[dict]:
    rows = []
    for p in folder.rglob("*"):
        if p.suffix in NOTE_SUFFIXES and p.is_file() and not p.name.startswith("."):
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            rows.append((mtime, p))
    rows.sort(key=lambda r: r[0], reverse=True)
    return [{"path": _rel(p), "title": _title_of(p), "modified": round(m)}
            for m, p in rows[:MAX_ITEMS]]


def _agenda(today: dt.date | None = None) -> list[dict]:
    """What is scheduled or due in the next week, and what is overdue — the
    agenda Emacs would show, from the same files. Astro alerts are
    point-in-time and never marked DONE, so past ones age out as in
    paragtd-astro-skip-stale."""
    today = today or dt.date.today()
    horizon = today + dt.timedelta(days=AGENDA_AHEAD_DAYS)
    files = [name for _, _, name in GTD_FILES] + ["astro.org"]
    items = []
    for name in files:
        for h in _headings(root() / name, done=False):
            date = h.get("deadline") or h.get("scheduled")
            if not date:
                continue
            try:
                when = dt.date.fromisoformat(date)
            except ValueError:
                continue
            if when > horizon:
                continue
            if name == "astro.org" and (today - when).days > ASTRO_STALE_DAYS:
                continue
            items.append({**h, "date": date, "overdue": when < today})
    items.sort(key=lambda h: (h["date"], h["path"], h["at"]))
    return items[:MAX_ITEMS]


def _id_index() -> dict[str, str]:
    """org-roam `:ID:` → path, for following `[[id:...]]` links. The IDs sit
    in each note's first property drawer, so only the head is read."""
    now = time.monotonic()
    with _LOCK:
        if _IDS["at"] and now - _IDS["at"] < _ID_TTL_S:
            return _IDS["map"]
    index: dict[str, str] = {}
    for p in (root() / "roam").rglob("*.org"):
        try:
            with p.open(errors="replace") as f:
                m = _ID_PROP.search(f.read(1024))
        except OSError:
            continue
        if m:
            index[m.group(1)] = _rel(p)
    with _LOCK:
        _IDS.update(at=now, map=index)
    return index


_LOCK = threading.Lock()
_IDS: dict = {"at": 0.0, "map": {}}
_ID_TTL_S = 60.0


# --- the routes' bodies ---------------------------------------------------------------

def views(bearer: str) -> tuple[bool, dict]:
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    out = [{"name": "agenda", "label": "Agenda", "kind": "agenda"}]
    for name, label, fname in GTD_FILES:
        p = root() / fname
        if p.is_file():
            n = sum(1 for h in _headings(p, done=False) if h["state"])
            out.append({"name": name, "label": label, "kind": "file",
                        "path": fname, "count": n})
    for name, label, folder in ROAM_FOLDERS:
        d = root() / folder
        if d.is_dir():
            n = sum(1 for p in d.rglob("*") if p.suffix in NOTE_SUFFIXES)
            out.append({"name": name, "label": label, "kind": "folder", "count": n})
    return True, {"root": str(root()), "views": out}


def view(name: str, bearer: str, *, done: bool = False) -> tuple[bool, dict]:
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    if name == "agenda":
        return True, {"view": name, "items": _agenda()}
    for vname, _, fname in GTD_FILES:
        if vname == name:
            return True, {"view": name, "items": _headings(root() / fname, done=done)}
    for vname, _, folder in ROAM_FOLDERS:
        if vname == name:
            return True, {"view": name, "items": _folder_notes(root() / folder)}
    return False, {"error": f"no such view {name!r}", "status": 404}


def read(rel: str, at: int, bearer: str) -> tuple[bool, dict]:
    """A whole note, or with `at` the subtree under the heading on that line.
    `links` resolves the note's org-roam id links to paths the app can open."""
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    p = _safe_path(rel)
    if not p:
        return False, {"error": "no such note", "status": 404}
    try:
        text = p.read_text(errors="replace")[:MAX_READ]
    except OSError as e:
        return False, {"error": f"could not read it ({e})", "status": 500}
    title = _title_of(p)
    if at > 0:
        lines = text.splitlines()
        m = _HEADING.match(lines[at - 1]) if at <= len(lines) else None
        if not m:
            return False, {"error": "no heading on that line (the file changed?)",
                           "status": 409}
        level = len(m.group(1))
        end = next((j for j in range(at, len(lines))
                    if (n := _HEADING.match(lines[j])) and len(n.group(1)) <= level),
                   len(lines))
        text = "\n".join(lines[at - 1:end]) + "\n"
        title = m.group(4).strip()
    ids = _id_index() if "[[id:" in text else {}
    links = [{"label": label or ids.get(i, i), "path": ids[i]}
             for i, label in _ID_LINK.findall(text) if i in ids]
    return True, {"path": _rel(p), "at": at, "title": title, "text": text,
                  "links": links}


def _scan(q: str, *, everything: bool, limit: int) -> list[dict]:
    """Search without ripgrep: the same rules, in Python. Slower on a big
    tree, and only used where `rg` is not installed."""
    import fnmatch

    needle = q.lower()
    hits: list[dict] = []
    for p in sorted(root().rglob("*")):
        rel = _rel(p)
        if (p.suffix not in NOTE_SUFFIXES or not p.is_file()
                or any(part.startswith(".") for part in Path(rel).parts)):
            continue
        if not everything and any(fnmatch.fnmatch(rel, x.replace("/**", "/*"))
                                  for x in SEARCH_EXCLUDES):
            continue
        try:
            if p.stat().st_size > 2 * 1024 * 1024:
                continue
            lines = p.read_text(errors="replace").splitlines()
        except OSError:
            continue
        n = 0
        for i, line in enumerate(lines):
            if needle in line.lower():
                hits.append({"path": rel, "line": i + 1, "text": line.strip()[:300]})
                n += 1
                if len(hits) >= limit:
                    return hits
                if n >= 3:
                    break
    return hits


def _rg(q: str, *, everything: bool, limit: int) -> list[dict]:
    if not shutil.which("rg"):
        return _scan(q, everything=everything, limit=limit)
    cmd = ["rg", "--json", "-i", "-F", "--max-count", "3", "--max-filesize", "2M"]
    for s in NOTE_SUFFIXES:
        cmd += ["-g", f"*{s}"]
    if not everything:
        for x in SEARCH_EXCLUDES:
            cmd += ["-g", f"!{x}"]
    cmd += ["--", q, "."]
    try:
        out = subprocess.run(cmd, cwd=root(), capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"notes: search failed ({e})", file=sys.stderr)
        return []
    hits: list[dict] = []
    for line in out.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") != "match":
            continue
        d = ev["data"]
        rel = d["path"]["text"].removeprefix("./")
        hits.append({"path": rel, "line": d["line_number"],
                     "text": d["lines"]["text"].strip()[:300]})
        if len(hits) >= limit:
            break
    return hits


def _memory_env() -> dict[str, str]:
    """The store's address and key: the process env first, then the file the
    `agent-memory-search` script reads."""
    env = {}
    for f in ("~/.config/hippocampus.env", "~/.config/sacred-brain.env"):
        p = Path(f).expanduser()
        if p.is_file():
            for line in p.read_text(errors="replace").splitlines():
                k, sep, v = line.partition("=")
                if sep and not k.lstrip().startswith("#"):
                    env[k.strip()] = v.strip().strip("'\"")
            break
    env.update({k: v for k, v in os.environ.items()
                if k.startswith(("HIPPOCAMPUS_", "AGENT_MEMORY_"))})
    return env


def _memory_call(method: str, path: str, body: dict | None = None,
                 timeout: float = 6.0) -> dict | None:
    env = _memory_env()
    base = (env.get("AGENT_MEMORY_HIPPOCAMPUS_URL") or env.get("HIPPOCAMPUS_URL")
            or "http://127.0.0.1:54321").rstrip("/")
    key = env.get("AGENT_MEMORY_API_KEY") or env.get("HIPPOCAMPUS_API_KEY") or ""
    req = urllib.request.Request(
        base + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", **({"X-API-Key": key} if key else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except Exception as e:  # noqa: BLE001 — the store is optional
        print(f"notes: memory {method} {path.split('?')[0]} failed ({e})", file=sys.stderr)
        return None


#: The namespaces `agent-memory-search` searches by default.
MEMORY_USERS = ("ryer", "sam")


def _memories(q: str, limit: int) -> list[dict]:
    hits = []
    for uid in MEMORY_USERS:
        got = _memory_call("GET", f"/memories/{uid}?"
                           + urllib.parse.urlencode({"query": q, "limit": limit}))
        for m in (got or {}).get("memories", []):
            hits.append({"id": m.get("id"), "user": uid,
                         "score": m.get("score"),
                         "text": str(m.get("text") or "")[:500]})
    hits.sort(key=lambda m: -(m["score"] or 0))
    return hits[:limit]


def search(q: str, bearer: str, *, everything: bool = False,
           memory: bool = True, limit: int = 30) -> tuple[bool, dict]:
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    q = (q or "").strip()
    if not q:
        return False, {"error": "nothing to search for", "status": 400}
    with ThreadPoolExecutor(max_workers=2) as pool:
        mem = pool.submit(_memories, q, 8) if memory else None
        notes = _rg(q, everything=everything, limit=limit)
        memories = mem.result() if mem else []
    return True, {"q": q, "notes": notes, "memories": memories}


def _org_now(now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now()
    return now.strftime("[%Y-%m-%d %a %H:%M]")


def entry(text: str, kind: str, now: dt.datetime | None = None) -> str:
    """paragtd's "t" template (and a plain heading for a note): the first
    line is the heading, the rest its body. A body line that would read as
    a heading of its own is indented one space, so a capture is one entry."""
    first, _, rest = text.strip().partition("\n")
    head = f"* TODO {first.strip()}" if kind == "todo" else f"* {first.strip()}"
    body = "".join(f" {ln}\n" if ln.startswith("*") else f"{ln}\n"
                   for ln in rest.strip("\n").splitlines())
    return f"{head}\n:PROPERTIES:\n:CREATED: {_org_now(now)}\n:END:\n{body}"


def capture(text: str, kind: str, bearer: str, *, remember: bool = True) -> tuple[bool, dict]:
    """Append to inbox.org, and (unless told not to) remember it."""
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    text = (text or "").replace("\r\n", "\n").strip()
    if not text:
        return False, {"error": "nothing to capture", "status": 400}
    if len(text) > MAX_CAPTURE:
        return False, {"error": "too long for a capture", "status": 413}
    kind = kind if kind in ("todo", "note") else "todo"
    inbox = root() / "inbox.org"
    block = entry(text, kind)
    try:
        with inbox.open("a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            before = f.read()
            if before and not before.endswith("\n"):
                block = "\n" + block
            # The line the heading lands on, for /notes/read?at=.
            at = before.count("\n") + 1 + (block[0] == "\n")
            f.write(block)
            f.flush()
    except OSError as e:
        return False, {"error": f"could not write the inbox ({e})", "status": 500}
    if remember:
        threading.Thread(target=_remember, args=(text, kind), daemon=True).start()
    print(f"notes: captured a {kind} ({len(text)} chars) for "
          f"{user.get('username')}", file=sys.stderr)
    return True, {"path": "inbox.org", "at": at, "kind": kind, "remembered": remember}


def _remember(text: str, kind: str) -> None:
    _memory_call("POST", "/memories", {
        "user_id": MEMORY_USERS[0],
        "text": f"David noted ({kind}): {text}",
        "metadata": {"source": "agent-media notes", "path": "inbox.org",
                     "kind": kind}})


# --- reading aloud -----------------------------------------------------------------

MAX_SPOKEN = 6000

_LINK = re.compile(r"\[\[(?:[^\]]+)\]\[([^\]]*)\]\]|\[\[([^\]]+)\]\]")
_DRAWER = re.compile(r"^\s*:[A-Z_]+:\s*$(?:.*?)^\s*:END:\s*$\n?", re.M | re.S)


def spoken(text: str) -> str:
    """Org as something to listen to: no drawers, keywords or dates; links
    by their label; a heading as a sentence of its own."""
    text = _DRAWER.sub("", text)
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith(("#+", "#", "SCHEDULED", "DEADLINE", "CLOSED")):
            continue
        m = _HEADING.match(line)
        if m:
            s = m.group(4).strip()
            if m.group(2) in ("TODO", "NEXT", "WAITING"):
                s = f"{m.group(2).capitalize()}: {s}"
            s = s.rstrip(".") + "."
        s = _LINK.sub(lambda k: k.group(1) or k.group(2).removeprefix("id:"), s)
        s = re.sub(r"^[-+]\s+(\[[ X-]\]\s+)?", "", s)
        out.append(s)
    return "\n".join(out)[:MAX_SPOKEN].strip()


def say(rel: str, at: int, bearer: str) -> tuple[bool, dict]:
    """Hand a note to the speech pipeline (`media say`), like any reply.
    Gated like the speech bar, since it makes the phone talk."""
    ok, detail = auth.may_control_speech(bearer)
    if not ok:
        return False, detail
    ok, got = read(rel, at, bearer)
    if not ok:
        return False, got
    text = spoken(got["text"])
    if not text:
        return False, {"error": "nothing in that note to read", "status": 422}
    try:
        p = subprocess.Popen([sys.executable, "-m", "agent_media_core.cli", "say"],
                             stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        p.stdin.write(text.encode())
        p.stdin.close()
    except OSError as e:
        return False, {"error": f"could not start speech ({e})", "status": 503}
    return True, {"path": got["path"], "at": at, "title": got["title"], "chars": len(text)}
