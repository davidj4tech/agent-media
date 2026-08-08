"""The speakable projection.

Markdown read aloud verbatim is unbearable — code fences, tables, URLs and
punctuation are most of what makes technical prose *technical*, and all of it
turns to noise the moment it stops being something the eye can skip. The rule
is: announce what can't be spoken, speak what can, and keep the structure that
lets someone navigate.
"""

from agent_media_core.docs import (Doc, describe, speak_inline,
                                   speakable_sections, speakable_text)


def _texts(md):
    return [(s.heading, s.text) for s in speakable_sections(md)]


def test_code_blocks_are_announced_not_read():
    md = "Intro.\n\n```python\nx = 1\ny = 2\nprint(x + y)\n```\n\nAfter."
    body = speakable_text(md)
    assert "A code example follows, 3 lines." in body
    assert "print" not in body and "x = 1" not in body


def test_a_one_line_block_is_singular():
    assert "1 line." in speakable_text("```\njust_this()\n```")


def test_tables_are_announced_not_read():
    md = ("| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n")
    body = speakable_text(md)
    assert "A table follows, 3 rows." in body     # header + 2 data rows
    assert "|" not in body


def test_links_keep_their_text_and_lose_the_url():
    assert speak_inline("see [the handover](docs/handover/2026-08-09.md) now") \
        == "see the handover now"


def test_bare_urls_become_a_link():
    assert speak_inline("fetch https://example.com/x?y=1 first") \
        == "fetch a link first"


def test_inline_code_and_emphasis_lose_their_markers():
    assert speak_inline("set `MEDIA_DOC_ROOTS` and **restart**") \
        == "set MEDIA_DOC_ROOTS and restart"


def test_headings_become_sections():
    md = "# Title\n\nlead\n\n## One\n\nfirst\n\n## Two\n\nsecond"
    got = _texts(md)
    assert [h for h, _ in got] == ["Title", "One", "Two"]
    assert got[1][1] == "first"


def test_headings_are_spoken_as_well_as_split_on():
    """They're the chapter marks, but a listener still needs to hear them."""
    assert "One." in speakable_text("## One\n\nbody")


def test_list_bullets_are_dropped_but_items_kept():
    body = speakable_text("- first thing\n- second thing\n1. third thing")
    assert "first thing" in body and "third thing" in body
    assert "- " not in body


def test_horizontal_rules_and_comments_vanish():
    body = speakable_text("a\n\n---\n\n<!-- hidden -->\n\nb")
    assert "hidden" not in body and "---" not in body
    assert "a" in body and "b" in body


def test_an_unclosed_fence_does_not_hang_or_leak_code():
    body = speakable_text("intro\n\n```\nsecret_code()\n")
    assert "secret_code" not in body


def test_describe_trims_an_essay_on_the_status_line(tmp_path):
    p = tmp_path / "x.md"
    p.write_text("# A doc\n\nStatus: **built** (2026-08-06). First pass "
                 "implemented — the contract, the wiring, and\nDate: 2026-08-06\n")
    d = describe(p)
    assert d.title == "A doc"
    assert d.status == "built"          # the word, not the paragraph
    assert d.date == "2026-08-06"


def test_describe_falls_back_to_the_filename(tmp_path):
    p = tmp_path / "some-old-note.md"
    p.write_text("no heading here\n")
    assert describe(p).title == "some old note"


def test_row_is_readable():
    d = Doc(path=None, slug="s", title="T", kind="notes", status="active",
            date="2026-08-09")
    assert d.as_row() == "T  (2026-08-09, notes, active)"
