"""Reading a project's document in the app: the reader outside the Org tree.

A doc path an agent mentions in a thread (`docs/proposals/….md`) opens in
the same reader as a note, with the same Listen (org_listen.py, `{"doc"}`).

  GET /doc/read?path=<absolute path> → {"path", "title", "text", "fmt", "mtime", "chats"}
  GET /doc/read?path=…&since=<mtime> → {"path", "mtime", "unchanged": true} when it has not changed

Only `.md`, `.org` and `.txt` files under the projects folder
(MEDIA_PROJECTS_DIR, else ~/projects) or a doc root (`docs.doc_roots()`,
MEDIA_DOC_ROOTS), with no dot-dir on the way (.git, .claude) and no symlink
out. 404 for anything else. server-contract.md §6.10.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from agent_media_core import docs, layout

from . import auth, new_project

SUFFIXES = (".md", ".org", ".txt")
MAX_READ = 256 * 1024
_MD_TITLE = re.compile(r"^#\s+(.+?)\s*#*\s*$", re.M)
_ORG_TITLE = re.compile(r"^#\+title:\s*(.+)$", re.I | re.M)


def roots() -> list[Path]:
    out = []
    for r in [new_project.root(), *(Path(p) for p in layout.projects_roots()),
              *docs.doc_roots()]:
        try:
            r = r.expanduser().resolve()
        except OSError:
            continue
        if r not in out:
            out.append(r)
    return out


def safe_path(raw: str) -> Path | None:
    """`raw` as a readable document, or None."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        p = Path(os.path.expanduser(raw)).resolve()
    except OSError:
        return None
    if p.suffix.lower() not in SUFFIXES or not p.is_file():
        return None
    for base in roots():
        try:
            rel = p.relative_to(base)
        except ValueError:
            continue
        if not any(part.startswith(".") for part in rel.parts):
            return p
    return None


def title_of(p: Path, text: str) -> str:
    m = (_ORG_TITLE if p.suffix.lower() == ".org" else _MD_TITLE).search(text[:4096])
    return m.group(1).strip() if m else p.stem


def fmt_of(p: Path) -> str:
    return "org" if p.suffix.lower() == ".org" else "md"


def refusal(raw: str) -> str:
    """Why `raw` will not open: a document that is there but outside the
    roots (or in a dot-dir) is said so, not called missing."""
    try:
        p = Path(os.path.expanduser((raw or "").strip())).resolve()
        there = (raw or "").strip() and p.suffix.lower() in SUFFIXES and p.is_file()
    except OSError:
        there = False
    return "outside the folders the reader opens" if there else "no such document"


def load(raw: str) -> tuple[bool, dict]:
    """The document, ungated: for callers that have checked already."""
    p = safe_path(raw)
    if not p:
        return False, {"error": refusal(raw), "status": 404}
    try:
        text = p.read_text(errors="replace")[:MAX_READ]
        mtime = p.stat().st_mtime
    except OSError as e:
        return False, {"error": f"could not read it ({e})", "status": 500}
    return True, {"path": str(p), "title": title_of(p, text), "text": text, "fmt": fmt_of(p),
                  "mtime": mtime}


def read(raw: str, bearer: str, since: float | None = None) -> tuple[bool, dict]:
    """The document; with `since` (the `mtime` last read), only whether it
    has changed: the reader looks again every few seconds while open."""
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    if since is not None:
        p = safe_path(raw)
        try:
            mtime = p.stat().st_mtime if p else None
        except OSError:
            mtime = None
        if mtime is not None and mtime == since:
            return True, {"path": str(p), "mtime": mtime, "unchanged": True}
    return load(raw)


# --- a project's documents, and finding one ---------------------------------
#
#   GET /docs?project=<label>|&path=<folder>
#       → {"project", "root", "docs": [{path, title, kind, status, date}]}
#   GET /search also answers "docs": [{path, title, project, kind, date, snippet}]

#: How many documents a project lists.
LIST_MAX = 300
_LABEL_PREFIXES = ("p-", "proj-")


def _project_of_rel(rel: str) -> str:
    """The project folder a path under a reader root is in: its first part,
    or the first two for one in a group (`sasonica/sasonica-app/docs/x.md`)."""
    parts = rel.split("/")
    if len(parts) > 2 and parts[0] in layout.project_groups():
        return f"{parts[0]}/{parts[1]}"
    return parts[0]


def project_dir(project: str = "", path: str = "") -> Path | None:
    """The folder a project's documents live in: `path` (a folder a thread
    runs in; a worktree's own folder counts), else ~/projects/<name> for a
    label like `p-agent-media` or `agent-media`. Only under a reader root."""
    cand: Path | None = None
    if path:
        try:
            cand = Path(os.path.expanduser(path)).resolve()
        except OSError:
            return None
        top = docs.project_root(cand)
        cand = top.resolve() if top else cand
    elif project:
        name = project.strip()
        known = layout.project_dir(name)    # any root, a group too: p-sasonica-app
        if known:
            cand = Path(known).resolve()
        else:
            for pre in _LABEL_PREFIXES:
                if name.startswith(pre) and (new_project.root() / name[len(pre):]).is_dir():
                    name = name[len(pre):]
                    break
            if not name or "/" in name or name.startswith("."):
                return None
            cand = (new_project.root() / name).resolve()
    if not cand or not cand.is_dir():
        return None
    for base in roots():
        if cand == base:
            return None                     # the whole projects folder is not a project
        try:
            rel = cand.relative_to(base)
        except ValueError:
            continue
        if not any(part.startswith(".") for part in rel.parts):
            return cand
    return None


#: A `Status:` anywhere in a document's opening lines ("…seal. Status: **proposal**").
_STATUS_ANY = re.compile(r"\bStatus:\s*\**([^\n]+)", re.I)
_SETTLED = re.compile(r"^(built|implemented|decided|accepted|landed|done|superseded|"
                      r"executed|retired|shipped|live)\b|"
                      r"\b(not doing|superseded|withdrawn|rejected|dropped)\b")
_PARTLY = re.compile(r"\b(steps?|first version|phase)\b.*\bbuilt\b|^steps?\b")
_OPEN = re.compile(r"^(propos|draft|plan|experiment|idea|open)|nothing built")


def _state(kind: str, status: str) -> str:
    """Where a proposal or decision stands, as an index groups them (PEP 0
    style): `open` (nothing built), `partly` (some steps built) or `settled`
    (built, decided, or closed: not doing, superseded). A proposal with no
    status counts as open. Other kinds have none."""
    s = status.lower().strip("* ")
    if kind == "decisions":
        return "settled"
    if kind != "proposals":
        return ""
    if _PARTLY.search(s):
        return "partly"
    if _SETTLED.search(s):
        return "settled"
    return "open"


def _row(p: Path, root: Path) -> dict:
    d = docs.describe(p, root)
    named = re.match(r"(\d{4}-\d{2}-\d{2})-", p.name)     # a dated name without a Date: line
    status = (d.status or "").strip("* ")
    if not status:
        try:
            with p.open(errors="replace") as f:
                m = _STATUS_ANY.search(f.read(3000))
            status = m.group(1).replace("**", "").strip(" .*") if m else ""
        except OSError:
            pass
    rel = p.relative_to(root)
    area = ("readme" if len(rel.parts) == 1 and p.name.lower() == "readme.md"
            else "docs" if rel.parts[0] == "docs" else "elsewhere")
    return {"path": str(p), "title": d.title, "kind": d.kind, "status": status,
            "date": d.date or (named.group(1) if named else ""),
            "area": area, "state": _state(d.kind, status)}


#: The tools that write a file, by the name the transcript gives them.
_WRITERS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})


def _written(transcript: Path) -> list[str]:
    """The files a Claude Code transcript wrote or edited, last touched
    first. Only lines naming a writing tool are parsed."""
    order: dict[str, int] = {}
    try:
        with transcript.open("rb") as f:
            for n, raw in enumerate(f):
                if b'"tool_use"' not in raw or b'"file_path"' not in raw:
                    continue
                try:
                    content = json.loads(raw).get("message", {}).get("content")
                except ValueError:
                    continue
                for part in content if isinstance(content, list) else []:
                    if (isinstance(part, dict) and part.get("type") == "tool_use"
                            and part.get("name") in _WRITERS):
                        fp = (part.get("input") or {}).get("file_path")
                        if isinstance(fp, str) and fp:
                            order[fp] = n
    except OSError:
        return []
    return sorted(order, key=lambda k: -order[k])


def thread_docs(session: str, listed: list[str]) -> list[str]:
    """Which of `listed` (a project's document paths) the thread `session`
    made or edited, its subagents included, or was started about from the
    reader (doc_chat). Last touched first; reads do not count, since nearly
    every thread reads the contract."""
    from . import org_chat, recaps

    by_real: dict[str, str] = {}
    for p in listed:
        try:
            by_real[str(Path(p).resolve())] = p
        except OSError:
            continue
    out: list[str] = []

    def take(fp: str) -> None:
        try:
            hit = by_real.get(str(Path(os.path.expanduser(fp)).resolve()))
        except OSError:
            return
        if hit and hit not in out:
            out.append(hit)

    tp = recaps.transcript_path(session)
    if tp:
        t = Path(tp)
        files = _written(t)
        for sub in sorted((t.parent / t.stem / "subagents").glob("*.jsonl")):
            files += _written(sub)
        for fp in files:
            take(fp)
    for key, rows in org_chat._load().items():
        if any(r.get("session") == session for r in rows if isinstance(r, dict)):
            take(key.split("\t", 1)[0])
    return out


def project_docs(project: str, path: str, bearer: str,
                 session: str = "") -> tuple[bool, dict]:
    """A project's documents: its README first, then the dated ones newest
    first, then the rest by title. With `session`, `thread` names the ones
    that thread made or edited."""
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    root = project_dir(project, path)
    if not root:
        return False, {"error": "no such project", "status": 404}
    rows = []
    for p in sorted(docs.walk_docs(root)):
        if docs._SKIP.search(p.name):
            continue
        try:
            if p.stat().st_size == 0:
                continue
        except OSError:
            continue
        rows.append(_row(p, root))
        if len(rows) >= LIST_MAX:
            break
    readme = lambda r: Path(r["path"]).parent == root and Path(r["path"]).name.lower() == "readme.md"  # noqa: E731
    rows.sort(key=lambda r: (not readme(r), r["date"] == "",
                             "".join(chr(0x10FFFF - ord(c)) for c in r["date"]), r["title"].lower()))
    out = {"project": project or root.name, "root": str(root), "docs": rows}
    if session:
        out["thread"] = thread_docs(session, [r["path"] for r in rows])
    return True, out


def _rg(args: list[str], cwd: Path) -> list[str] | None:
    """rg's file names under `cwd`, relative and with forward slashes (rg on
    Windows prints `.\\docs\\x.md`). None when there is no rg to ask."""
    import shutil
    import subprocess
    rg = shutil.which("rg")
    if not rg:
        return None
    try:
        out = subprocess.run([rg, *args, "-g", "*.md", "-g", "*.org", "-g", "*.txt", "."],
                             cwd=cwd, capture_output=True, text=True, timeout=4)
    except (OSError, subprocess.SubprocessError):
        return []
    lines = (ln.replace(os.sep, "/") for ln in out.stdout.splitlines() if ln)
    return [ln[2:] if ln.startswith("./") else ln for ln in lines]


#: How many documents the walk without rg looks through before it stops.
WALK_MAX = 20000


def _walk(base: Path, terms: list[str]) -> list[str]:
    """What `rg -l -i -F -e …` and the name match find, for a host with no rg
    (GitHub's runners, a fresh Mac or Windows): every document under `base`
    whose name or text holds any of `terms`, relative with forward slashes."""
    out = []
    for n, p in enumerate(docs.walk_docs(base)):
        if n >= WALK_MAX:
            break
        name = p.name.lower()
        if not any(t in name for t in terms):
            try:
                with p.open(errors="replace") as f:
                    low = f.read(MAX_READ).lower()
            except OSError:
                continue
            if not any(t in low for t in terms):
                continue
        out.append(p.relative_to(base).as_posix())
    return out


def search(terms: list[str], limit: int = 8) -> list[dict]:
    """Documents under the projects folder holding every term, in their title
    or name first, then in their text; newest first within each."""
    terms = [t.lower() for t in terms if t.strip()]
    base = new_project.root()
    if not terms or not base.is_dir():
        return []
    args = ["-l", "-i", "-F"] + [x for t in terms for x in ("-e", t)]
    listed = _rg(args, base)
    if listed is None:
        found = set(_walk(base, terms))
    else:
        found = set(listed)
        found |= {f for f in _rg(["--files"], base) or []
                  if all(t in Path(f).name.lower() for t in terms)}
    hits = []
    for rel in found:
        p = (base / rel)
        if not safe_path(str(p)):
            continue
        try:
            text = p.read_text(errors="replace")[:MAX_READ]
            mtime = p.stat().st_mtime
        except OSError:
            continue
        low = text.lower()
        if not all(t in low or t in p.name.lower() for t in terms):
            continue
        title = title_of(p, text)
        rank = 0 if all(t in title.lower() for t in terms) else 1 if all(t in p.name.lower() for t in terms) else 2
        line = next((ln.strip() for ln in text.splitlines() if terms[0] in ln.lower()), "")
        if len(line) > 160:
            at = max(0, line.lower().find(terms[0]) - 60)
            line = ("…" if at else "") + line[at:at + 160] + "…"
        hits.append((rank, -mtime, {"path": str(p), "title": title,
                                     "project": _project_of_rel(rel),
                                     "kind": p.parent.name, "snippet": line},
                     rel.split("/", 1)[-1]))
    hits.sort(key=lambda h: (h[0], h[1], h[2]["path"]))
    # A clone or worktree beside the project (agent-media-audio) holds the
    # same files: one hit each, the most recently changed copy.
    out, seen = [], set()
    for _r, _m, h, inside in hits:
        key = (h["title"], inside)
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
        if len(out) >= limit:
            break
    return out
