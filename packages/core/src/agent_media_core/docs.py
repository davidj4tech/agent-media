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
    tags: list = field(default_factory=list)

    @property
    def fmt(self) -> str:
        return "org" if str(self.path).lower().endswith(".org") else "md"

    def as_row(self) -> str:
        bits = [b for b in (self.date, self.kind, self.status) if b]
        bits += self.tags[:3]
        # Capture notes take their title from the captured text, which can be
        # a whole paragraph or a JSON blob. A picker row is a label, not the
        # document.
        title = self.title if len(self.title) <= 88 else self.title[:87] + "…"
        return f"{title}" + (f"  ({', '.join(bits)})" if bits else "")


_TITLE_RE = re.compile(r"^#\s+(.+?)\s*$", re.M)
_STATUS_RE = re.compile(r"^Status:\s*\**(.+?)\**\s*$", re.M)
_DATE_RE = re.compile(r"^Date:\s*(\S+)\s*$", re.M)
# Org's own front matter. `#+filetags:` takes either `:a:b:` or bare words.
_ORG_TITLE_RE = re.compile(r"^#\+title:\s*(.+?)\s*$", re.M | re.I)
_ORG_TAGS_RE = re.compile(r"^#\+filetags:\s*(.+?)\s*$", re.M | re.I)
_ORG_FIRST_HEAD = re.compile(r"^\*+\s+(.+?)\s*$", re.M)
# Denote: 20260809T075902--title-slug__keyword1_keyword2.org — identity, title
# and keywords in the filename, so a picker can list and filter 900 notes
# without opening one of them.
_DENOTE = re.compile(r"^(\d{8}T\d{6})(?:--([^_.]+))?(?:__([^.]+))?$")


def _org_tags(raw: str) -> list:
    return [t for t in re.split(r"[:\s,]+", raw.strip()) if t]


def describe(path: Path, root: Optional[Path] = None) -> Doc:
    """Title/status/date/tags from the file's first lines.

    Markdown uses the repo layout's `# Title` + `Status:` + `Date:`; org uses
    its own `#+title:` / `#+filetags:`. A Denote filename carries all three
    without opening the file at all, so it wins when present — which is the
    point of that convention with 900 notes in the tree.
    """
    try:
        head = path.read_text(errors="replace")[:2000]
    except OSError:
        head = ""
    is_org = path.suffix.lower() == ".org"
    tags: list = []
    title = ""

    dn = _DENOTE.match(path.stem)
    if dn:
        if dn.group(2):
            title = dn.group(2).replace("-", " ").strip().capitalize()
        if dn.group(3):
            tags = [t for t in dn.group(3).split("_") if t]

    if is_org:
        m = _ORG_TITLE_RE.search(head)
        if m and not title:
            title = m.group(1)
        tm = _ORG_TAGS_RE.search(head)
        if tm:
            tags = tags or _org_tags(tm.group(1))
        if not title:
            h = _ORG_FIRST_HEAD.search(head)
            if h:
                title = h.group(1)
    else:
        m = _TITLE_RE.search(head)
        if m and not title:
            title = m.group(1)
    if not title:
        title = path.stem.replace("-", " ")
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
    date = dt.group(1) if dt else ""
    if not date and dn:
        d = dn.group(1)
        date = f"{d[0:4]}-{d[4:6]}-{d[6:8]}"      # Denote's ID is the date
    return Doc(path=path, slug=slug, title=title, kind=kind,
               status=status, date=date, tags=tags)


SUFFIXES = (".md", ".org")

# Emacs lock/backup droppings and our own dated backups. A picker that lists
# `inbox.org.bak-2026-08-09` next to `inbox.org` makes the user do the
# filtering that the tool exists to do.
_SKIP = re.compile(r"(^\.#|~$|\.bak(-|$)|^\.)", re.I)


INBOX_TAG = "inbox"


def list_docs(tag: str = "", include_inbox: bool = False) -> list[Doc]:
    """Documents under the roots. `tag` filters on filetags/keywords.

    Filtering is by tag rather than by directory on purpose. Where these files
    live, PARA membership is a filetag and a note routinely belongs to several
    places at once — which is the one thing a directory cannot express.

    Unclarified captures are left out by default. In this tree they are 650 of
    940 documents, and GTD is explicit about what they are: an inbox is a queue
    of things not yet decided about, not a library you browse. Listing them
    with everything else means the reader does the sorting the tool exists to
    do. Asking for the tag by name overrides this — that is a deliberate look
    at the queue.
    """
    out: list[Doc] = []
    seen: set = set()
    for root in doc_roots():
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*")):
            if p.suffix.lower() not in SUFFIXES or not p.is_file():
                continue
            if _SKIP.search(p.name) or p.name.lower() == "readme.md":
                continue
            rp = p.resolve()
            if rp in seen:          # overlapping roots (~/org and ~/org/roam)
                continue
            seen.add(rp)
            out.append(describe(p, root))
    if tag:
        t = tag.lower()
        out = [d for d in out if any(t == x or t in x for x in d.tags)]
    elif not include_inbox:
        out = [d for d in out if INBOX_TAG not in [x.lower() for x in d.tags]]
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


# --- org ------------------------------------------------------------------

_ORG_HEADING = re.compile(r"^(\*+)\s+(.*?)\s*$")
_ORG_BLOCK = re.compile(r"^\s*#\+begin_(\w+)", re.I)
_ORG_KEYWORD = re.compile(r"^\s*#\+\w+:")
_ORG_COMMENT = re.compile(r"^\s*#(\s|$)")
_ORG_DRAWER = re.compile(r"^\s*:(\w+):\s*$")
_ORG_DRAWER_END = re.compile(r"^\s*:END:\s*$", re.I)
_ORG_LINK = re.compile(r"\[\[([^\]]+?)\](?:\[([^\]]*?)\])?\]")
_ORG_EMPH = re.compile(r"(?<![\w*/=~+])([*/=~+])(\S(?:.*?\S)?)\1(?![\w*/=~+])")
_ORG_TODO = re.compile(r"^(TODO|NEXT|DONE|WAITING|SOMEDAY|CANCELLED|HOLD)\s+")
_ORG_PRIORITY = re.compile(r"^\[#([A-C])\]\s*")
_ORG_HEAD_TAGS = re.compile(r"\s+(:[\w@#%:]+:)\s*$")
_ORG_TIMESTAMP = re.compile(r"[<\[](\d{4}-\d{2}-\d{2})(?:\s+\w{3})?"
                            r"(?:\s+\d{2}:\d{2})?[^>\]]*[>\]]")
_ORG_CHECKBOX = re.compile(r"^\s*[-+*]\s+\[([ xX-])\]\s*")


def speak_inline_org(s: str) -> str:
    """Org inline markup → words.

    `[[url][text]]` keeps the text and drops the target, exactly as a markdown
    link does; a bare `[[url]]` has no text worth saying, so it is announced.
    """
    s = _ORG_LINK.sub(lambda m: (m.group(2) or "a link"), s)
    s = _ORG_TIMESTAMP.sub(lambda m: m.group(1), s)
    s = _ORG_EMPH.sub(r"\2", s)
    s = _BARE_URL.sub("a link", s)
    return re.sub(r"\s+", " ", s).strip()


def _org_heading_text(raw: str) -> str:
    """Strip the machinery, keep the words: TODO state and priority spoken,
    the trailing `:tag:tag:` dropped — it is metadata, not a sentence."""
    raw = _ORG_HEAD_TAGS.sub("", raw)
    state = ""
    m = _ORG_TODO.match(raw)
    if m:
        state = m.group(1).lower().replace("cancelled", "cancelled")
        raw = raw[m.end():]
    p = _ORG_PRIORITY.match(raw)
    if p:
        raw = raw[p.end():]
        state = (state + f", priority {p.group(1)}").lstrip(", ")
    text = speak_inline_org(raw)
    return f"{text} — {state}" if state else text


def speakable_sections_org(src: str) -> list[Section]:
    """Org's equivalent of the markdown projection.

    Same rule — announce what can't be spoken — applied to a different set of
    tokens: source blocks and examples are announced, quotes are read (they
    are prose), drawers and `#+keyword:` lines vanish entirely because they
    are machinery the reader never sees either.
    """
    sections: list[Section] = []
    cur = Section(heading="", text="", level=1)
    buf: list[str] = []

    def flush():
        # Blank lines are kept, then runs collapsed: a paragraph break is the
        # only pacing cue the renderer gets, and dropping them turns a section
        # into one breathless run-on.
        body = re.sub(r"\n{3,}", "\n\n", "\n".join(buf)).strip()
        if body or cur.heading:
            sections.append(Section(heading=cur.heading, text=body,
                                    level=cur.level))
        buf.clear()

    lines = src.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]

        b = _ORG_BLOCK.match(line)
        if b:
            kind = b.group(1).lower()
            end = re.compile(rf"^\s*#\+end_{kind}", re.I)
            n = 0
            i += 1
            body: list[str] = []
            while i < len(lines) and not end.match(lines[i]):
                body.append(lines[i])
                n += 1
                i += 1
            i += 1
            if kind in ("quote", "verse"):
                buf.extend(speak_inline_org(x) for x in body)
            elif kind == "src":
                buf.append(f"A code example follows, {_plural(n, 'line', 'lines')}.")
            elif kind == "example":
                buf.append(f"An example follows, {_plural(n, 'line', 'lines')}.")
            continue

        if _ORG_DRAWER.match(line) and not _ORG_DRAWER_END.match(line):
            i += 1
            while i < len(lines) and not _ORG_DRAWER_END.match(lines[i]):
                i += 1
            i += 1
            continue

        if _ORG_KEYWORD.match(line) or _ORG_COMMENT.match(line):
            i += 1
            continue

        if _TABLE_ROW.match(line):
            rows = 0
            while i < len(lines) and _TABLE_ROW.match(lines[i]):
                if not re.match(r"^\s*\|[-+\s|]+\|?\s*$", lines[i]):
                    rows += 1
                i += 1
            buf.append(f"A table follows, {_plural(rows, 'row', 'rows')}.")
            continue

        h = _ORG_HEADING.match(line)
        if h:
            flush()
            cur = Section(heading=_org_heading_text(h.group(2)), text="",
                          level=len(h.group(1)))
            i += 1
            continue

        cb = _ORG_CHECKBOX.match(line)
        if cb:
            mark = cb.group(1)
            state = "done" if mark in "xX" else ("partly done" if mark == "-"
                                                 else "to do")
            buf.append(f"{speak_inline_org(line[cb.end():])} — {state}")
            i += 1
            continue

        buf.append(speak_inline_org(_LIST_BULLET.sub("", line)))
        i += 1

    flush()
    return [s for s in sections if s.text.strip() or s.heading]


def sections_for(text: str, fmt: str = "md") -> list[Section]:
    return (speakable_sections_org(text) if fmt == "org"
            else speakable_sections(text))


def speakable_text(md: str, fmt: str = "md") -> str:
    """The whole document as one block — for renderers without chapters."""
    parts = []
    for s in sections_for(md, fmt):
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
    sections = sections_for(md, 'org' if path.suffix.lower() == '.org' else 'md')
    if not sections:
        return None
    return _render_sections_to(sections, final, engine, voice)


def render_sections(sections: list, stem: str, engine: Optional[str] = None,
                    voice: Optional[str] = None,
                    force: bool = True) -> Optional[Path]:
    """Render already-built sections (an agenda, a summary) to chaptered audio.

    Same pipeline as a document, minus the file: the caller has composed the
    text itself, so there is no mtime to cache on and nothing to re-read.
    """
    engine = engine or os.environ.get("MEDIA_RENDER_ENGINE", "edge")
    voice = voice or os.environ.get(
        "MEDIA_RENDER_VOICE_" + engine.upper()) or ""
    outdir = doc_cache_dir()
    outdir.mkdir(parents=True, exist_ok=True)
    final = outdir / f"{stem}.mp3"
    if final.exists() and not force:
        return final
    if not sections:
        return None
    return _render_sections_to(sections, final, engine, voice)


def _render_sections_to(sections: list, final: Path, engine: str,
                        voice: str) -> Optional[Path]:
    from .render import render_text

    outdir = final.parent
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
