"""Every verb we send at the speech player, the companion app must implement.

The phone lane no longer ends at mpv. It ends at an app answering the same
socket with its own subset of mpv's verbs, and a verb missing from that subset
is not an error anyone sees — it is a key that does nothing. That happened
twice: `cycle` (so the popup's Space stopped pausing) and `seek` (so `<`, `>`
and the jump keys stopped moving the playhead), each found weeks later by
someone noticing the key was dead.

`MpvServerTest` exists and did not catch either, because it tests "the
sequences SinkSpeech sends" — and both of those verbs are sent from `cli.py`,
not the sink, so they were never in its field of view. This looks at the
senders instead: whatever the Python says out loud, the Java has to answer.

Static, deliberately. It needs no phone, and the drift it catches is someone
adding a verb here without adding it there — which is a thing you do at a
keyboard, not at runtime.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


CORE = Path(__file__).resolve().parents[1] / "src" / "agent_media_core"
APP = (Path(__file__).resolve().parents[3] / "android" / "companion" / "src"
       / "net" / "agentmedia" / "companion" / "MpvServer.java")

#: Where a raw verb can be aimed at the speech player.
SENDERS = ["cli.py", "sinks/speech.py", "intake/submit.py",
           "route/coordinator.py"]

#: `ipc.command(sock, "verb", ...)` / `ipc.send_nowait(sock, "verb", ...)`.
_SENT = re.compile(r'ipc\.(?:command|send_nowait)\(\s*[^,]+,\s*"([a-z][a-z-]*)"')

#: `if ("verb".equals(verb))`, and the `||` alternatives beside it.
_IMPLEMENTED = re.compile(r'"([a-z][a-z_-]*)"\.equals\(verb\)')

#: Sent through helpers the app implements by name, not as raw verbs.
_VIA_HELPERS = {"get_property", "set_property", "observe_property"}


def _sent_verbs() -> set[str]:
    found = set()
    for rel in SENDERS:
        path = CORE / rel
        if not path.exists():                     # a file moved: say so
            pytest.fail(f"{rel} is gone — this test is looking in the wrong "
                        "place and would pass by finding nothing")
        found |= set(_SENT.findall(path.read_text()))
    return found - _VIA_HELPERS


def _implemented_verbs() -> set[str]:
    if not APP.exists():
        pytest.skip(f"no companion sources at {APP}")
    return set(_IMPLEMENTED.findall(APP.read_text()))


def test_the_scrapers_still_find_things():
    """A regex that matches nothing turns this file into a test that always
    passes, which is worse than not having it."""
    assert len(_sent_verbs()) >= 3, _sent_verbs()
    assert len(_implemented_verbs()) >= 6, _implemented_verbs()


def test_the_app_answers_every_verb_we_send():
    sent, implemented = _sent_verbs(), _implemented_verbs()
    missing = sorted(sent - implemented)
    assert not missing, (
        f"the companion app does not implement {missing} — it will answer "
        '{"error":"invalid parameter"} and the control will silently do '
        "nothing on the phone lane. Add it to MpvServer.dispatch.")


def test_the_verbs_we_know_about_are_still_there():
    """The two that were missing, named, so a revert is loud."""
    implemented = _implemented_verbs()
    for verb in ("cycle", "seek"):
        assert verb in implemented, (
            f"{verb!r} went away again; it was a dead key for weeks the last "
            "time")
