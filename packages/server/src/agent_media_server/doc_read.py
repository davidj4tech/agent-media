"""Reading a project's document in the app: the reader outside the Org tree.

A doc path an agent mentions in a thread (`docs/proposals/….md`) opens in
the same reader as a note, with the same Listen (org_listen.py, `{"doc"}`).

  GET /doc/read?path=<absolute path> → {"path", "title", "text", "fmt"}

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
    except OSError as e:
        return False, {"error": f"could not read it ({e})", "status": 500}
    return True, {"path": str(p), "title": title_of(p, text), "text": text, "fmt": fmt_of(p)}


def read(raw: str, bearer: str) -> tuple[bool, dict]:
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    return load(raw)
