"""Unit tests for the TTS text-cleanup helpers in intake/_text.py."""

from agent_media_core.intake._text import (
    _regex_suppress_tables,
    strip_markdown,
    suppress_code_blocks,
    suppress_tables,
    suppress_urls,
)


# --- code blocks --------------------------------------------------------

def test_fenced_code_block_suppressed_with_lang_and_count():
    text = "before\n\n```python\na = 1\nb = 2\n```\n\nafter"
    out = suppress_code_blocks(text)
    assert "python code block, 2 lines, omitted." in out
    assert "a = 1" not in out


def test_no_code_block_is_untouched():
    text = "just some prose, nothing to omit."
    assert suppress_code_blocks(text) == text


# --- tables -------------------------------------------------------------

TABLE = (
    "Options:\n\n"
    "| Name | Port | Notes |\n"
    "| ---- | ---- | ----- |\n"
    "| homer | 54321 | canonical |\n"
    "| red5 | 54323 | governor |\n\n"
    "done."
)


def test_table_summarised_with_headers_and_row_count():
    out = suppress_tables(TABLE)
    assert "table, columns Name, Port, Notes, 2 rows, omitted." in out
    assert "54321" not in out
    assert "----" not in out  # separator row gone


def test_single_row_table_is_singular():
    text = "| A | B |\n|---|---|\n| 1 | 2 |"
    assert "1 row, omitted." in suppress_tables(text)


def test_table_with_url_in_cell_never_spoken():
    text = "| Repo | URL |\n|---|---|\n| am | https://github.com/foo/am/tree/main |"
    out = strip_markdown(text)
    assert "github.com" not in out
    assert "table," in out


def test_fenced_block_with_pipes_is_code_not_table():
    text = "run:\n\n```\necho a | grep b\necho c | sort\n```\n\nend"
    out = strip_markdown(text)
    assert "code block" in out
    assert "table," not in out


def test_no_pipe_text_untouched_by_tables():
    text = "no pipes here, just prose."
    assert suppress_tables(text) == text


def test_regex_table_fallback_matches_markdown_it():
    # The regex fallback should also collapse the table to a placeholder.
    out = _regex_suppress_tables(TABLE)
    assert "table, columns Name, Port, Notes, 2 rows, omitted." in out
    assert "54321" not in out


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

def test_strip_markdown_handles_all_three():
    text = (
        "See [docs](https://x.io/a/b):\n\n"
        "| K | V |\n|---|---|\n| a | 1 |\n\n"
        "```python\nx = 1\n```"
    )
    out = strip_markdown(text)
    assert "docs" in out and "x.io" not in out
    assert "table," in out
    assert "python code block" in out
