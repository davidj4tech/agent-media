"""Documents you can listen to.

A design doc read aloud verbatim is unbearable: code fences, tables, URLs and
path names are most of what makes prose *technical*, and all of it degrades
into noise the moment it stops being something your eye can skip. So this is
not markdown-to-speech. It is a deliberate projection — announce what can't be
spoken, speak what can, and keep the structure that lets someone navigate.

Structure is the whole point. Nobody listens to a reference doc front to back;
they want the section on the thing they're stuck on. Headings therefore become
**chapter marks in the rendered audio**, so the popup's existing chapter
browser navigates the document, and the book channel's resume position brings
you back where you stopped. A document is a short audiobook, so it is played as
one rather than growing a channel of its own.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ._paths import cache_dir


# --- discovery -------------------------------------------------------------

def doc_roots() -> list[Path]:
    """Where documents live. `MEDIA_DOC_ROOTS` is a colon-separated override.

    Defaults to this repo's docs/ tree. Kept a list from the start because the
    interesting roots are elsewhere — an org/denote directory is the same kind
    of thing and should be listenable by the same key.
    """
    raw = os.environ.get("MEDIA_DOC_ROOTS", "")
    if raw:
        return [Path(p).expanduser() for p in raw.split(":") if p.strip()]
    here = Path(__file__).resolve()
    for parent in here.parents:
        cand = parent / "docs"
        if cand.is_dir() and (parent / ".git").exists():
            return [cand]
    return []


@dataclass
class Doc:
    path: Path
    slug: str
    title: str
    kind: str = ""
    status: str = ""
    date: str = ""

    def as_row(self) -> str:
        bits = [b for b in (self.date, self.kind, self.status) if b]
        return f"{self.title}" + (f"  ({', '.join(bits)})" if bits else "")


_TITLE_RE = re.compile(r"^#\s+(.+?)\s*$", re.M)
_STATUS_RE = re.compile(r"^Status:\s*\**(.+?)\**\s*$", re.M)
_DATE_RE = re.compile(r"^Date:\s*(\S+)\s*$", re.M)


def describe(path: Path, root: Optional[Path] = None) -> Doc:
    """Title/status/date from the file's first few lines — the three-line
    header the docs layout asks for. Falls back to the filename, so a document
    that predates the convention still lists."""
    try:
        head = path.read_text(errors="replace")[:2000]
    except OSError:
        head = ""
    m = _TITLE_RE.search(head)
    title = m.group(1) if m else path.stem.replace("-", " ")
    st = _STATUS_RE.search(head)
    dt = _DATE_RE.search(head)
    # Older docs write "Status: built** (2026-08-06). First pass implemented —
    # the contract, the..." — a whole paragraph on the Status line. A listing
    # wants the word, not the essay, so keep what comes before the first
    # qualifier and cap it.
    status = ""
    if st:
        status = re.split(r"[(—:.]| - ", st.group(1).strip(), maxsplit=1)[0]
        status = status.strip(" *_").lower()[:24]
    kind = path.parent.name if root and path.parent != root else ""
    slug = path.stem
    return Doc(path=path, slug=slug, title=title, kind=kind,
               status=status, date=dt.group(1) if dt else "")


def list_docs() -> list[Doc]:
    out: list[Doc] = []
    for root in doc_roots():
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*.md")):
            if p.name.lower() == "readme.md":
                continue
            out.append(describe(p, root))
    # Newest dated first, undated (reference) after — reference is browsed by
    # name, the dated kinds by recency.
    return sorted(out, key=lambda d: (d.date == "", d.date, d.title), reverse=True)


def find_doc(needle: str) -> Optional[Doc]:
    """Resolve a path, an exact slug, or a unique substring of either."""
    p = Path(needle).expanduser()
    if p.is_file():
        return describe(p)
    docs = list_docs()
    for d in docs:
        if d.slug == needle:
            return d
    n = needle.lower()
    hits = [d for d in docs if n in d.slug.lower() or n in d.title.lower()]
    return hits[0] if len(hits) >= 1 else None


# --- the speakable projection ----------------------------------------------

@dataclass
class Section:
    heading: str
    text: str
    level: int = 1
    lines: list = field(default_factory=list)


_FENCE = re.compile(r"^\s*(```|~~~)")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*$")
_TABLE_ROW = re.compile(r"^\s*\|")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_INLINE_CODE = re.compile(r"`([^`]+)`")
# Asterisk emphasis is unambiguous; underscore is not. `_` is a word character
# in every identifier this codebase talks about, so an unguarded rule turns
# MEDIA_DOC_ROOTS into MEDIADOCROOTS — mangling precisely the names a listener
# needs said correctly. Underscore only counts as emphasis at a word boundary.
_STAR_EMPH = re.compile(r"(\*\*|\*)(.+?)\1")
_US_EMPH = re.compile(r"(?<!\w)(__|_)(.+?)\1(?!\w)")
_BARE_URL = re.compile(r"https?://\S+")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_LIST_BULLET = re.compile(r"^\s*([-*+]|\d+\.)\s+")
_RULE = re.compile(r"^\s*([-*_])\s*(\1\s*){2,}$")


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one}" if n == 1 else f"{n} {many}"


def speak_inline(s: str) -> str:
    """Inline markup → words. Link text without the URL, code without ticks."""
    s = _IMAGE.sub(lambda m: (m.group(1) or "an image"), s)
    s = _LINK.sub(r"\1", s)
    s = _INLINE_CODE.sub(r"\1", s)
    s = _STAR_EMPH.sub(r"\2", s)
    s = _US_EMPH.sub(r"\2", s)
    s = _BARE_URL.sub("a link", s)
    return re.sub(r"\s+", " ", s).strip()


def speakable_sections(md: str) -> list[Section]:
    """Split into sections at headings, each already projected to speech.

    Code and tables are *announced, not read*. Saying "a code example follows,
    twelve lines" tells a listener what they'd have learned from the shape of
    the block on screen — that there is one, and roughly how big — which is all
    the eye takes from a block it skips anyway. Reading it aloud instead is how
    a document becomes unlistenable.
    """
    md = _HTML_COMMENT.sub("", md)
    sections: list[Section] = []
    cur = Section(heading="", text="", level=1)
    buf: list[str] = []

    def flush():
        body = "\n".join(buf).strip()
        if body or cur.heading:
            sections.append(Section(heading=cur.heading, text=body,
                                    level=cur.level))
        buf.clear()

    lines = md.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]

        if _FENCE.match(line):                      # code block: announce it
            marker = _FENCE.match(line).group(1)
            n = 0
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(marker):
                n += 1
                i += 1
            i += 1
            buf.append(f"A code example follows, {_plural(n, 'line', 'lines')}.")
            continue

        if _TABLE_ROW.match(line):                  # table: announce it
            rows = 0
            while i < len(lines) and _TABLE_ROW.match(lines[i]):
                if not re.match(r"^\s*\|[\s:|-]+\|?\s*$", lines[i]):
                    rows += 1
                i += 1
            buf.append(f"A table follows, {_plural(rows, 'row', 'rows')}.")
            continue

        m = _HEADING.match(line)
        if m:
            flush()
            cur = Section(heading=speak_inline(m.group(2)), text="",
                          level=len(m.group(1)))
            i += 1
            continue

        if _RULE.match(line):
            i += 1
            continue

        stripped = _LIST_BULLET.sub("", line)
        spoken = speak_inline(stripped)
        buf.append(spoken)
        i += 1

    flush()
    return [s for s in sections if s.text.strip() or s.heading]


def speakable_text(md: str) -> str:
    """The whole document as one block — for renderers without chapters."""
    parts = []
    for s in speakable_sections(md):
        if s.heading:
            parts.append(s.heading + ".")
        if s.text:
            parts.append(s.text)
    return "\n\n".join(p for p in parts if p.strip())


# --- rendering with chapters -----------------------------------------------

def _cache_key(path: Path, engine: str, voice: str) -> str:
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        mtime = 0
    raw = f"{path.resolve()}|{mtime}|{engine}|{voice}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def doc_cache_dir() -> Path:
    return cache_dir() / "docs"


def _ffprobe_duration(p: Path) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(p)],
            capture_output=True, text=True, timeout=30).stdout.strip()
        return float(out)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def _write_chapter_metadata(meta: Path, chapters: list[tuple[str, float, float]]) -> None:
    """FFMETADATA with one [CHAPTER] per heading, times in milliseconds."""
    lines = [";FFMETADATA1"]
    for title, start, end in chapters:
        safe = title.replace("=", r"\=").replace(";", r"\;").replace("#", r"\#")
        lines += ["[CHAPTER]", "TIMEBASE=1/1000",
                  f"START={int(start * 1000)}", f"END={int(end * 1000)}",
                  f"title={safe}"]
    meta.write_text("\n".join(lines) + "\n")


def render_doc(path: Path, engine: Optional[str] = None,
               voice: Optional[str] = None,
               force: bool = False) -> Optional[Path]:
    """Render `path` to one audio file whose chapters are its headings.

    Cached on (path, mtime, engine, voice): a document is re-read far more
    often than it is edited, and synthesis is by far the slowest thing here.
    Returns None if nothing could be rendered.
    """
    from .render import render_text

    engine = engine or os.environ.get("MEDIA_RENDER_ENGINE", "edge")
    voice = voice or os.environ.get(
        "MEDIA_RENDER_VOICE_" + engine.upper()) or ""

    outdir = doc_cache_dir()
    outdir.mkdir(parents=True, exist_ok=True)
    final = outdir / f"{path.stem}-{_cache_key(path, engine, voice)}.mp3"
    if final.exists() and final.stat().st_size > 0 and not force:
        return final

    try:
        md = path.read_text(errors="replace")
    except OSError:
        return None
    sections = speakable_sections(md)
    if not sections:
        return None

    work = outdir / f".{final.stem}.parts"
    work.mkdir(parents=True, exist_ok=True)
    parts: list[Path] = []
    chapters: list[tuple[str, float, float]] = []
    clock = 0.0
    for n, sec in enumerate(sections):
        say = ((sec.heading + ".\n\n") if sec.heading else "") + sec.text
        if not say.strip():
            continue
        part = work / f"{n:04d}.mp3"
        ok, _err = render_text(say, part, engine=engine, voice=voice or None)
        if not ok or not part.exists() or part.stat().st_size == 0:
            continue
        dur = _ffprobe_duration(part)
        parts.append(part)
        chapters.append((sec.heading or f"Section {n + 1}", clock, clock + dur))
        clock += dur

    if not parts:
        return None

    listing = work / "parts.txt"
    listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts))
    meta = work / "chapters.txt"
    _write_chapter_metadata(meta, chapters)
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-f", "concat", "-safe", "0", "-i", str(listing),
             "-i", str(meta), "-map_metadata", "1",
             "-c", "copy", str(final)],
            check=True, capture_output=True, timeout=600)
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        for p in list(parts) + [listing, meta]:
            try:
                p.unlink()
            except OSError:
                pass
        try:
            work.rmdir()
        except OSError:
            pass
    return final if final.exists() else None
