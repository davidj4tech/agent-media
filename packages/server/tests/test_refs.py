"""A chip naming another conversation gets a line saying which one (refs.py)."""
from agent_media_server import refs

A = "6c73a0e2-1111-4222-8333-444455556666"
B = "0f00ba12-aaaa-4bbb-8ccc-ddddeeeeffff"


def _no_files(monkeypatch, index=()):
    monkeypatch.setattr(refs.transcript, "transcript_of",
                        lambda s: ("claude", f"/t/{s}.jsonl") if s == A else ("", ""))
    monkeypatch.setattr(refs.sessions, "sessions_index", lambda: list(index))


def test_no_chip_is_untouched(monkeypatch):
    _no_files(monkeypatch)
    assert refs.expand("plain words", {"x": A}) == "plain words"


def test_chip_from_refs_gets_a_line(monkeypatch):
    _no_files(monkeypatch)
    out = refs.expand("what did we say in @[Gimbal notes]? ", {"Gimbal notes": A})
    assert out == ("what did we say in @[Gimbal notes]?\n\n"
                   f"@[Gimbal notes] is conversation {A} (claude, transcript /t/{A}.jsonl)")


def test_each_chip_once_in_order(monkeypatch):
    _no_files(monkeypatch)
    out = refs.expand("@[Two] and @[One] and @[Two]", {"One": A, "Two": B})
    lines = out.split("\n\n", 1)[1].splitlines()
    assert [l.split("]")[0] for l in lines] == ["@[Two", "@[One"]
    assert "no transcript file here" in lines[0]


def test_unknown_chip_falls_back_to_a_unique_title(monkeypatch):
    _no_files(monkeypatch, [{"session": A, "title": "Gimbal  notes"},
                            {"session": B, "title": "Twice"}, {"session": A, "title": "Twice"},
                            {"session": B, "title": "twice"}])
    out = refs.expand("see @[gimbal notes] and @[Twice]")
    assert f"@[gimbal notes] is conversation {A}" in out
    assert "@[Twice] is" not in out          # two threads carry it: no guess


def test_bad_session_and_nothing_found_leave_text_alone(monkeypatch):
    _no_files(monkeypatch)
    assert refs.expand("see @[X]", {"X": "not-a-session"}) == "see @[X]"
    assert refs.expand("see @[X]", ["junk"]) == "see @[X]"
