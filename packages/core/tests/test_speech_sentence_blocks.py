"""A heading, and the words said for a code block or table, are sentences of
their own (intake/_text.strip_markdown + submit._split_sentences).

History row 15815 (session 2ccb8b31, 18 Oct 2026): strip_markdown ran every
paragraph break down to one newline, so the splitter saw no paragraphs, and a
heading with no full stop ("The layout", "A few lines in ~/.gitconfig do
it:") was glued onto the next sentence — the description of the block under
it. The app walks each spoken sentence against the reply as shown, could not
find "The layout\\n \\n<description>" there, and fell back to space-joined
sentences for the whole reply.
"""

import json
import pathlib

from agent_media_core.intake import _summary
from agent_media_core.intake._text import strip_markdown
from agent_media_core.intake._visual import extract_visual_markers
from agent_media_core.intake.submit import _split_sentences, _split_sentences_with_paragraphs

FX = json.loads((pathlib.Path(__file__).parent / "fixtures" /
                 "described_code_reply.json").read_text())


def _spoken(reply, **kw):
    return _split_sentences(strip_markdown(extract_visual_markers(reply)[0], **kw))


def test_the_replys_headings_and_blocks_are_sentences_of_their_own():
    got = _spoken(FX["reply"])
    for alone in ("The layout", "code block, 4 lines, omitted.",
                  "Why folders beat a naming prefix",
                  "A few lines in ~/.gitconfig do it:", "ini code block, 4 lines, omitted.",
                  "What I noticed in the folder now", "Sasonica is the open question.",
                  "The catch: hard-coded paths"):
        assert alone in got, (alone, got)
    assert "Each folder can carry its own git identity." in got


def test_a_described_block_is_never_glued_to_the_heading_above_it(monkeypatch):
    said = iter(["A directory tree. It has three folders, one per business.",
                 "A git config that picks an identity by folder."])
    monkeypatch.setattr(_summary, "describe_code", lambda body: next(said))
    got = _spoken(FX["reply"], describe=True)
    i = got.index("The layout")
    assert got[i + 1:i + 3] == ["A directory tree.", "It has three folders, one per business."]
    j = got.index("A few lines in ~/.gitconfig do it:")
    assert got[j + 1] == "A git config that picks an identity by folder."
    # Nothing the voice says carries a heading's words and a block's.
    assert not any("\n" in s and ("layout" in s or "do it:" in s) for s in got), got


def test_a_heading_with_no_blank_line_after_it():
    assert _spoken("## Setup\nRun the installer first.") == ["Setup", "Run the installer first."]
    assert _spoken("**The catch**\nMany things point straight at it.") == [
        "The catch", "Many things point straight at it."]


def test_a_block_with_no_blank_lines_around_it():
    text = "Run this to see it:\n```\na=1\nb=2\nc=3\n```\nThen it works as before."
    assert _spoken(text) == ["Run this to see it:", "code block, 3 lines, omitted.",
                             "Then it works as before."]


def test_a_table_stand_in_is_its_own_sentence():
    text = ("The ports in use\n| Name | Port |\n|---|---|\n| a | 1 |\n| b | 2 |\n| c | 3 |\n"
            "That is all of them.")
    got = _spoken(text)
    assert got[-1] == "That is all of them."
    assert got[-2].startswith("table, columns Name, Port, 3 rows")
    assert got[0] == "The ports in use"


def test_ordinary_prose_splits_as_before():
    # One paragraph: exactly the old split (hard wraps, lists and the
    # short-fragment merge included).
    para = ("This is the first sentence. And here is a second one!\n"
            "A hard-wrapped line\ncarries on here. Is it fine?\n"
            "- One item ends here.\n- Another item, also.")
    assert _split_sentences(strip_markdown(para)) == _split_sentences(
        strip_markdown(para, paragraphs=False))
    # Paragraphs that end in a full stop end their sentence where they did.
    text = ("The first paragraph ends right here. It has two sentences.\n\n"
            "- One list item that ends here.\n- Another list item, also ended.\n\n"
            "And the last paragraph is this one.")
    sents, paras = _split_sentences_with_paragraphs(strip_markdown(text))
    assert sents == _split_sentences(strip_markdown(text, paragraphs=False)) == [
        "The first paragraph ends right here.", "It has two sentences.",
        "One list item that ends here.", "Another list item, also ended.",
        "And the last paragraph is this one."]
    assert paras == [0, 0, 1, 1, 2]


def test_the_old_form_is_kept_for_old_keys():
    text = "**The layout**\n\n```\na\nb\nc\n```\n\n- item one.\n\nDone here."
    assert strip_markdown(text, paragraphs=False) == (
        "The layout\ncode block, 3 lines, omitted.\nitem one.\nDone here.")
    assert strip_markdown(text) == (
        "The layout\n\ncode block, 3 lines, omitted.\n\nitem one.\n\nDone here.")
