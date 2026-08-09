"""The org projection.

Same rule as markdown — announce what can't be spoken, speak what can — over a
different set of tokens. Org has more machinery than markdown does, and nearly
all of it is invisible to a reader (drawers, `#+keyword:` lines, heading tags),
so it must be invisible to a listener too.
"""

from agent_media_core.docs import (describe, list_docs, sections_for,
                                   speak_inline_org, speakable_text)


def _org(src):
    return speakable_text(src, "org")


def test_org_headings_become_sections():
    src = "* One\n\nfirst\n\n** Two\n\nsecond\n"
    got = [(s.heading, s.text) for s in sections_for(src, "org")]
    assert [h for h, _ in got] == ["One", "Two"]
    assert got[1][1] == "second"


def test_src_blocks_are_announced_not_read():
    src = "* H\n\n#+begin_src python\nx = 1\ny = 2\n#+end_src\n"
    body = _org(src)
    assert "A code example follows, 2 lines." in body
    assert "x = 1" not in body


def test_examples_are_announced_separately_from_code():
    src = "#+begin_example\nsome output\n#+end_example\n"
    assert "An example follows, 1 line." in _org(src)


def test_quotes_are_read_because_they_are_prose():
    src = "#+begin_quote\nthe thing itself\n#+end_quote\n"
    body = _org(src)
    assert "the thing itself" in body
    assert "follows" not in body


def test_drawers_are_dropped_entirely():
    src = ("* H\n:PROPERTIES:\n:ID: abc-123\n:END:\n\nreal text\n")
    body = _org(src)
    assert "abc-123" not in body and "PROPERTIES" not in body
    assert "real text" in body


def test_keyword_lines_are_dropped():
    src = "#+title: T\n#+filetags: a b\n\nbody text\n"
    body = _org(src)
    assert "filetags" not in body and "#+" not in body
    assert "body text" in body


def test_heading_tags_are_dropped_but_todo_state_is_spoken():
    src = "* TODO Fix the bridge :work:urgent:\n\nbody\n"
    heading = sections_for(src, "org")[0].heading
    assert ":work:" not in heading and "urgent" not in heading
    assert "Fix the bridge" in heading
    assert "todo" in heading.lower()


def test_priority_is_spoken_not_bracketed():
    src = "* TODO [#A] Ship it\n"
    h = sections_for(src, "org")[0].heading
    assert "[#A]" not in h and "priority A" in h


def test_org_links_keep_their_description():
    assert speak_inline_org("see [[file:x.org][the note]] now") \
        == "see the note now"


def test_bare_org_link_is_announced():
    assert speak_inline_org("see [[https://example.com]] now") \
        == "see a link now"


def test_timestamps_become_plain_dates():
    assert speak_inline_org("due <2026-08-09 Sun 10:30>") == "due 2026-08-09"


def test_org_emphasis_markers_are_removed():
    assert speak_inline_org("this is *bold* and /italic/ and =code=") \
        == "this is bold and italic and code"


def test_underscores_in_identifiers_survive_org_too():
    assert "MEDIA_DOC_ROOTS" in speak_inline_org("set MEDIA_DOC_ROOTS now")


def test_checkboxes_are_spoken_as_state():
    body = _org("- [ ] buy milk\n- [X] call bank\n")
    assert "buy milk — to do" in body
    assert "call bank — done" in body


def test_org_tables_are_announced():
    src = "| a | b |\n|---+---|\n| 1 | 2 |\n"
    body = _org(src)
    assert "A table follows" in body and "|" not in body


def test_paragraph_breaks_survive():
    """The only pacing cue the renderer gets."""
    body = _org("* H\n\none\n\ntwo\n")
    assert "one\n\ntwo" in body


def test_describe_reads_org_front_matter(tmp_path):
    p = tmp_path / "n.org"
    p.write_text("#+title: My Note\n#+filetags: :gtd:para:\n\n* body\n")
    d = describe(p)
    assert d.title == "My Note"
    assert d.tags == ["gtd", "para"]
    assert d.fmt == "org"


def test_describe_reads_a_denote_filename_without_opening_it(tmp_path):
    p = tmp_path / "20260809T075902--speech-convergence__decision_media.org"
    p.write_text("")                      # deliberately empty: filename wins
    d = describe(p)
    assert d.tags == ["decision", "media"]
    assert d.date == "2026-08-09"
    assert "speech convergence" in d.title.lower()


def test_inbox_captures_are_excluded_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DOC_ROOTS", str(tmp_path))
    (tmp_path / "a.org").write_text("#+title: Real Note\n#+filetags: :ref:\n")
    (tmp_path / "b.org").write_text("#+title: Raw Capture\n#+filetags: :inbox:\n")

    assert [d.title for d in list_docs()] == ["Real Note"]
    assert {d.title for d in list_docs(include_inbox=True)} == \
        {"Real Note", "Raw Capture"}
    # Asking for the queue by name is a deliberate look at it.
    assert [d.title for d in list_docs(tag="inbox")] == ["Raw Capture"]


def test_backups_and_lockfiles_are_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DOC_ROOTS", str(tmp_path))
    (tmp_path / "real.org").write_text("#+title: Real\n")
    (tmp_path / "real.org.bak-2026-08-09").write_text("#+title: Old\n")
    (tmp_path / ".#real.org").write_text("#+title: Lock\n")
    assert [d.title for d in list_docs()] == ["Real"]


def test_long_capture_titles_are_truncated_in_a_row(tmp_path):
    p = tmp_path / "x.org"
    p.write_text("#+title: " + ("word " * 60) + "\n")
    assert len(describe(p).as_row()) < 120
