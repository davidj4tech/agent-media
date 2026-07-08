"""Unit tests for the TTS text-cleanup helpers in intake/_text.py."""

from agent_media_core.intake._text import (
    IncrementalSentencer,
    _regex_suppress_tables,
    strip_markdown,
    suppress_code_blocks,
    suppress_tables,
    suppress_urls,
)


def _stream(text, chunk=7):
    """Feed `text` through IncrementalSentencer in small chunks."""
    s = IncrementalSentencer()
    out = []
    for i in range(0, len(text), chunk):
        out += s.feed(text[i:i + chunk])
    return out + s.close()


# --- code blocks --------------------------------------------------------
# A block is only replaced with a placeholder when it does NOT "read well":
# more than MEDIA_SPEECH_CODE_MAX_LINES lines (default 2), or symbol-dense.
# Small, low-symbol blocks are read literally (see the "reads well" tests).

def test_fenced_code_block_suppressed_with_lang_and_count():
    text = "before\n\n```python\na = 1\nb = 2\nc = 3\nd = 4\n```\n\nafter"
    out = suppress_code_blocks(text)
    assert "python code block, 4 lines, omitted." in out
    assert "a = 1" not in out


def test_no_code_block_is_untouched():
    text = "just some prose, nothing to omit."
    assert suppress_code_blocks(text) == text


def test_small_readable_code_block_read_literally():
    # A short, low-symbol block (e.g. a shell command) is spoken verbatim.
    out = suppress_code_blocks("Run:\n```sh\ngit push origin main\n```\ndone")
    assert "git push origin main" in out
    assert "code block" not in out


def test_symbol_dense_small_code_still_suppressed():
    # Small but symbol-dense (brace/bracket salad) does not read well.
    out = suppress_code_blocks("```js\nconst x={a:[1],b:{c:2}};\n```")
    assert "code block" in out


# --- tables -------------------------------------------------------------
# Tables over MEDIA_SPEECH_TABLE_MAX_ROWS (default 2), with long cells, or with
# a URL in a cell are replaced with a placeholder; smaller ones are read as
# prose (see test_small_table_rendered_as_prose).

TABLE = (
    "Options:\n\n"
    "| Name | Port | Notes |\n"
    "| ---- | ---- | ----- |\n"
    "| homer | 54321 | canonical |\n"
    "| red5 | 54323 | governor |\n"
    "| mel | 54322 | mesh |\n\n"
    "done."
)


def test_table_summarised_with_headers_and_row_count():
    out = suppress_tables(TABLE)
    assert "table, columns Name, Port, Notes, 3 rows, omitted." in out
    assert "54321" not in out
    assert "----" not in out  # separator row gone


def test_single_row_table_is_singular():
    # Force suppression with an over-long cell so the singular placeholder shows.
    text = "| A | B |\n|---|---|\n| 1 | " + "x" * 50 + " |"
    assert "1 row, omitted." in suppress_tables(text)


def test_small_table_rendered_as_prose():
    out = suppress_tables("| Host | Status |\n|---|---|\n| red5 | live |\n| mel | live |")
    assert "Host: red5, Status: live" in out
    assert "omitted" not in out
    assert "|" not in out


def test_table_with_url_in_cell_never_spoken():
    text = "| Repo | URL |\n|---|---|\n| am | https://github.com/foo/am/tree/main |"
    out = strip_markdown(text)
    assert "github.com" not in out
    assert "table," in out  # a URL cell doesn't read well -> placeholder, not prose


def test_fenced_block_with_pipes_is_code_not_table():
    text = "run:\n\n```\necho a | grep b\necho c | sort\necho d | uniq\n```\n\nend"
    out = strip_markdown(text)
    assert "code block" in out
    assert "table," not in out


def test_no_pipe_text_untouched_by_tables():
    text = "no pipes here, just prose."
    assert suppress_tables(text) == text


def test_regex_table_fallback_matches_markdown_it():
    # The regex fallback should also collapse the table to a placeholder.
    out = _regex_suppress_tables(TABLE)
    assert "table, columns Name, Port, Notes, 3 rows, omitted." in out
    assert "54321" not in out


# --- optional LLM description (MEDIA_SPEECH_DESCRIBE=1) ------------------

def test_describe_on_replaces_unreadable_block(monkeypatch):
    import agent_media_core.intake._summary as summary
    monkeypatch.setenv("MEDIA_SPEECH_DESCRIBE", "1")
    monkeypatch.setattr(summary, "describe_code", lambda body: "a script that prints numbers")
    big = "```python\n" + "\n".join(f"print({i})" for i in range(6)) + "\n```"
    assert suppress_code_blocks(big) == "a script that prints numbers"


def test_describe_failure_falls_back_to_placeholder(monkeypatch):
    import agent_media_core.intake._summary as summary
    monkeypatch.setenv("MEDIA_SPEECH_DESCRIBE", "1")
    monkeypatch.setattr(summary, "describe_code", lambda body: None)
    big = "```python\n" + "\n".join(f"print({i})" for i in range(6)) + "\n```"
    assert "code block" in suppress_code_blocks(big)


def test_describe_off_keeps_placeholder_no_call(monkeypatch):
    # Default: no describe, no model import needed — just the placeholder.
    monkeypatch.delenv("MEDIA_SPEECH_DESCRIBE", raising=False)
    big = "```python\n" + "\n".join(f"print({i})" for i in range(6)) + "\n```"
    assert "python code block, 6 lines, omitted." in suppress_code_blocks(big)


# --- urls ---------------------------------------------------------------

def test_bare_url_reduced_to_host_link():
    out = suppress_urls("see https://github.com/foo/bar?x=1#L2 now.")
    assert out == "see github.com link now."


def test_markdown_link_keeps_text_drops_url():
    out = suppress_urls("read the [release notes](https://example.com/v/1.2.3) here.")
    assert out == "read the release notes here."


def test_url_trailing_sentence_punctuation_preserved():
    out = suppress_urls("at https://foo.com. Next.")
    assert out == "at foo.com link. Next."


def test_bare_domain_without_scheme_untouched():
    text = "visit www.google.com/search?q=hi later."
    assert suppress_urls(text) == text


# --- combined -----------------------------------------------------------

def test_table_absorbed_prose_line_not_swallowed():
    # GFM pulls a following non-blank line into the table; we must keep it.
    text = "| A | B |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n| 5 | 6 |\nDone here."
    out = suppress_tables(text)
    assert "3 rows, omitted." in out
    assert "Done here." in out


# --- streaming sentencer ------------------------------------------------

def test_stream_plain_prose_splits_normally():
    out = _stream("One here. Two follows! Three ends.", chunk=5)
    assert out == ["One here.", "Two follows!", "Three ends."]


def test_stream_abbreviations_do_not_split():
    out = _stream("See Dr. Smith, e.g. today. Next.", chunk=4)
    assert out == ["See Dr. Smith, e.g. today.", "Next."]


def test_stream_code_fence_with_punctuation_held_together():
    # print("Hello. World.") must NOT split the fence mid-block; block is large
    # enough to be suppressed so its content never leaks into speech.
    text = 'Intro.\n```python\nprint("Hello. World.")\nx = 1\ny = 2\nz = 3\n```\nAfter.'
    out = _stream(text, chunk=6)
    assert out[0] == "Intro."
    joined = " ".join(out)
    assert "python code block" in joined
    assert "Hello. World." not in joined
    assert joined.endswith("After.")


def test_stream_table_held_together():
    text = "Below. | A | B |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n| 5 | 6 |\nDone."
    out = _stream(text, chunk=6)
    joined = " ".join(out)
    assert "table, columns A, B" in joined
    assert "| 1 | 2 |" not in joined
    assert "Done." in joined


def test_strip_markdown_handles_all_three():
    text = (
        "See [docs](https://x.io/a/b):\n\n"
        "| K | V |\n|---|---|\n| a | 1 |\n| b | 2 |\n| c | 3 |\n\n"
        "```python\nx = 1\ny = 2\nz = 3\n```"
    )
    out = strip_markdown(text)
    assert "docs" in out and "x.io" not in out
    assert "table," in out
    assert "python code block" in out
