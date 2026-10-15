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

import os
import re
from pathlib import Path

from agent_media_core import docs

from . import auth, new_project

SUFFIXES = (".md", ".org", ".txt")
MAX_READ = 256 * 1024
_MD_TITLE = re.compile(r"^#\s+(.+?)\s*#*\s*$", re.M)
_ORG_TITLE = re.compile(r"^#\+title:\s*(.+)$", re.I | re.M)


def roots() -> list[Path]:
    out = []
    for r in [new_project.root(), *docs.doc_roots()]:
        try:
            out.append(r.expanduser().resolve())
        except OSError:
            continue
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


def load(raw: str) -> tuple[bool, dict]:
    """The document, ungated: for callers that have checked already."""
    p = safe_path(raw)
    if not p:
        return False, {"error": "no such document", "status": 404}
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


def _row(p: Path, root: Path) -> dict:
    d = docs.describe(p, root)
    named = re.match(r"(\d{4}-\d{2}-\d{2})-", p.name)     # a dated name without a Date: line
    return {"path": str(p), "title": d.title, "kind": d.kind,
            "status": (d.status or "").strip("* "),
            "date": d.date or (named.group(1) if named else "")}


def project_docs(project: str, path: str, bearer: str) -> tuple[bool, dict]:
    """A project's documents: its README first, then the dated ones newest
    first, then the rest by title."""
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
    return True, {"project": project or root.name, "root": str(root), "docs": rows}


def _rg(args: list[str], cwd: Path) -> list[str]:
    import subprocess
    try:
        out = subprocess.run(["rg", *args, "-g", "*.md", "-g", "*.org", "-g", "*.txt", "."],
                             cwd=cwd, capture_output=True, text=True, timeout=4)
    except (OSError, subprocess.SubprocessError):
        return []
    return [ln[2:] if ln.startswith("./") else ln for ln in out.stdout.splitlines() if ln]


def search(terms: list[str], limit: int = 8) -> list[dict]:
    """Documents under the projects folder holding every term, in their title
    or name first, then in their text; newest first within each."""
    terms = [t.lower() for t in terms if t.strip()]
    base = new_project.root()
    if not terms or not base.is_dir():
        return []
    args = ["-l", "-i", "-F"] + [x for t in terms for x in ("-e", t)]
    found = set(_rg(args, base))
    found |= {f for f in _rg(["--files"], base)
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
                                     "project": rel.split("/", 1)[0],
                                     "kind": p.parent.name, "snippet": line}))
    hits.sort(key=lambda h: (h[0], h[1]))
    # A clone or worktree beside the project (agent-media-audio) holds the
    # same files: one hit each, the most recently changed copy.
    out, seen = [], set()
    for _r, _m, h in hits:
        key = (h["title"], h["path"].split("/", len(base.parts) + 1)[-1])
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
        if len(out) >= limit:
            break
    return out
