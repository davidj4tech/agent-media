"""Small text-cleanup helpers shared across intake adapters."""

from __future__ import annotations

import re


_FENCE_RE = re.compile(r"```[a-zA-Z0-9_-]*")
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_ITAL_RE = re.compile(r"(?<!\*)\*([^*]+)\*")
_CODE_RE = re.compile(r"`([^`]+)`")
_HEAD_RE = re.compile(r"^#{1,6}\s+", flags=re.MULTILINE)
_BULLET_RE = re.compile(r"^\s*[-*]\s+", flags=re.MULTILINE)
_BLANK_RE = re.compile(r"\n[ \t]*\n+")


_REGEX_FENCE_BLOCK = re.compile(r"(`{3,}|~{3,})([^\n]*)\n(.*?)\n[ \t]*\1", re.DOTALL)


# Markdown link / image: [text](url) or ![alt](url) -> keep only the human text.
_MD_LINK_RE = re.compile(r"!?\[([^\]]*)\]\(\s*<?([^)\s>]+)>?(?:\s+[^)]*)?\)")
# Autolink <https://...> -> spoken host.
_AUTOLINK_RE = re.compile(r"<(https?://[^>\s]+)>")
# Bare URL with an explicit scheme (safe: won't catch "e.g." / "example.com").
_BARE_URL_RE = re.compile(r"\bhttps?://[^\s)>\]`]+", re.IGNORECASE)


def _url_host(url: str) -> str:
    """Reduce a URL to a spoken 'host link' placeholder, dropping the path/query
    so TTS says "github.com link" instead of reading the whole thing."""
    host = re.sub(r"^https?://", "", url, flags=re.IGNORECASE)
    host = re.sub(r"^www\.", "", host, flags=re.IGNORECASE)
    host = host.split("/")[0].split("?")[0].split("#")[0].strip()
    host = host.rstrip(".,;:!?")
    return f"{host} link" if host else "link"


def suppress_urls(text: str) -> str:
    """Keep markdown link *text* but drop the URL, and reduce bare URLs to a
    spoken "<host> link" placeholder, so TTS doesn't read long URLs / query
    strings aloud."""
    if not text:
        return text
    out = _MD_LINK_RE.sub(lambda m: m.group(1).strip() or _url_host(m.group(2)), text)
    out = _AUTOLINK_RE.sub(lambda m: _url_host(m.group(1)), out)

    def _bare(m: "re.Match[str]") -> str:
        url = m.group(0)
        trail = ""
        while url and url[-1] in ".,;:!?":  # keep sentence punctuation outside the URL
            trail = url[-1] + trail
            url = url[:-1]
        return _url_host(url) + trail

    out = _BARE_URL_RE.sub(_bare, out)
    return out


def _code_placeholder(n_lines: int, lang: str = "") -> str:
    n = max(1, n_lines)
    lang_word = f"{lang} " if lang else ""
    return f"{lang_word}code block, {n} line{'s' if n != 1 else ''}, omitted."


def _regex_suppress_fences(text: str) -> str:
    def repl(m: "re.Match[str]") -> str:
        lang = (m.group(2) or "").strip().split(" ")[0]
        n = m.group(3).count("\n") + 1
        return _code_placeholder(n, lang)
    return _REGEX_FENCE_BLOCK.sub(repl, text)


def suppress_code_blocks(text: str) -> str:
    """Replace fenced / indented code blocks with a short *spoken* placeholder
    ("python code block, 12 lines, omitted.") so TTS describes code instead of
    reading it line by line. Uses markdown-it-py for robust block detection when
    available; falls back to a fenced-code regex. Must run on the FULL text
    (a block spans multiple sentences), so it's applied at the top of
    ``strip_markdown`` before sentence-level cleanup.
    """
    if not text or ("```" not in text and "~~~" not in text and "\n    " not in text):
        return text
    try:
        from markdown_it import MarkdownIt
        tokens = MarkdownIt("commonmark").parse(text)
    except Exception:  # noqa: BLE001 — any import/parse issue → regex fallback
        return _regex_suppress_fences(text)

    spans: list[tuple[int, int, str]] = []
    for tok in tokens:
        rng = getattr(tok, "map", None)
        if not rng or tok.type not in ("fence", "code_block"):
            continue
        start, end = rng
        if tok.type == "fence":
            lang = (tok.info or "").strip().split(" ")[0]
            spans.append((start, end, _code_placeholder(end - start - 2, lang)))
        else:  # indented code_block
            spans.append((start, end, _code_placeholder(end - start)))
    if not spans:
        return text

    spans.sort()
    lines = text.split("\n")
    out_lines: list[str] = []
    i = 0
    si = 0
    while i < len(lines):
        if si < len(spans) and i == spans[si][0]:
            out_lines.append(spans[si][2])
            i = spans[si][1]
            si += 1
        else:
            out_lines.append(lines[i])
            i += 1
    return "\n".join(out_lines)


def strip_markdown(text: str) -> str:
    """Strip enough markdown that TTS doesn't read backticks / asterisks /
    fence markers aloud, and replace fenced code blocks with a spoken
    placeholder. Loose by design: callers can submit anything.
    """
    if not text:
        return ""
    out = suppress_code_blocks(text)
    out = suppress_urls(out)
    out = _FENCE_RE.sub("", out)
    out = _HEAD_RE.sub("", out)
    out = _BOLD_RE.sub(r"\1", out)
    out = _ITAL_RE.sub(r"\1", out)
    out = _CODE_RE.sub(r"\1", out)
    out = _BULLET_RE.sub("", out)
    out = _BLANK_RE.sub("\n", out).strip()
    return out


# Sentence-ending punctuation, optional closing quote/bracket, then whitespace.
# The trailing whitespace is what tells us the sentence is *complete* — we only
# split once the following character has arrived in the stream.
_SENT_BOUNDARY = re.compile(r'[.!?]+["\'”’)\]]*\s')

# Abbreviations / initials whose trailing period is NOT a sentence end. Matched
# against the text up to and including the boundary's first punctuation char.
_ABBREV_TAIL = re.compile(
    r'(?:\b(?:Mr|Mrs|Ms|Dr|Prof|Sr|Jr|St|vs|etc|e\.g|i\.e|'
    r'Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)|\b[A-Za-z])\.$'
)

_WS_RE = re.compile(r'\s+')


class IncrementalSentencer:
    """Segment a *streamed* text into complete sentences as they arrive.

    Used by the streaming pi intake: token deltas are `feed()` in as they
    arrive, and each call returns any sentences that have just completed (i.e.
    whose terminating punctuation is now followed by whitespace). Trailing
    partial text is buffered until more arrives. `close()` flushes whatever
    remains as a final sentence.

    Each emitted sentence is run through `strip_markdown` and whitespace-
    collapsed so it's ready to render. Abbreviations and single-letter
    initials ("Dr.", "e.g.", "J.") don't trigger a split.

    Known limitation: fenced code blocks aren't suppressed (strip_markdown
    drops the ``` markers but keeps the lines) — fine for prose / roleplay,
    which is the streaming path's purpose.
    """

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, chunk: str) -> list[str]:
        if not chunk:
            return []
        self._buf += chunk
        return self._drain()

    def close(self) -> list[str]:
        out = self._drain()
        tail = self._clean(self._buf)
        self._buf = ""
        if tail:
            out.append(tail)
        return out

    def _drain(self) -> list[str]:
        out: list[str] = []
        while True:
            split_end = -1
            for m in _SENT_BOUNDARY.finditer(self._buf):
                # Text up to and including the first punctuation char of the
                # boundary; skip if it ends in an abbreviation/initial.
                head = self._buf[: m.start() + 1]
                if _ABBREV_TAIL.search(head):
                    continue
                split_end = m.end()  # includes the trailing whitespace
                break
            if split_end < 0:
                break
            raw, self._buf = self._buf[:split_end], self._buf[split_end:]
            cleaned = self._clean(raw)
            if cleaned:
                out.append(cleaned)
        return out

    @staticmethod
    def _clean(raw: str) -> str:
        return _WS_RE.sub(" ", strip_markdown(raw)).strip()
