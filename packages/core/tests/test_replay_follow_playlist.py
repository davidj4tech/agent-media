"""Following along on a replay the app started, a clip per sentence, on the phone.

David, 28 Sep 2026: ▶ on a held reply in the app played it to the end, with
no mini player and no bold. Two things, both in the replay's push:

- the follower got the sentences only with a tmux pane (the popup's), and the
  app has none — so the row's sentence never moved;
- a clip per sentence was followed by asking the player, and the phone's
  player is across the tailnet: reads cost 0.6-2.4 s, a few slow ones trip the
  endpoint's breaker for 45 s, and five refusals read as the end. The row was
  cleared while the phone read on.

So a far player's playlist is followed on the clock, from the clips' starts,
as the live lane does, and the sentences go to the follower with or without a
pane.
"""

import json
import time

import pytest

from agent_media_core import cli


SENTENCES = ["One.", "Two.", "Three."]


class _Sink:
    def __init__(self):
        self.calls = []

    def prefetch(self, paths, target=None):
        return True

    def play_playlist(self, uris, target=None, gapless=True, start=0):
        self.calls.append(("play_playlist", list(uris), start))


class _Proc:
    pid = 4242


@pytest.fixture
def rig(monkeypatch):
    rec = {"rows": [], "argv": []}
    monkeypatch.setattr(cli, "SinkSpeech", _Sink)
    monkeypatch.setattr(cli, "_replay_visual", lambda ex: None)
    monkeypatch.setattr(cli, "_caller_pane", lambda: "")      # the app: no pane

    def popen(argv, **kw):
        rec["argv"] = list(argv)
        return _Proc()

    monkeypatch.setattr(cli.subprocess, "Popen", popen)
    monkeypatch.setattr(cli.StateStore, "set_now_playing",
                        lambda self, sink, **kw: rec["rows"].append(kw))
    monkeypatch.setattr(cli._speech_sink, "set_media_title", lambda *a, **k: None)
    monkeypatch.setattr(cli._speech_sink, "set_reply_text", lambda *a, **k: None)
    return rec


def _row(**extra):
    return {"uri": "remote-0.mp3", "text": " ".join(SENTENCES),
            "extras": {"clip_uris": [f"remote-{i}.mp3" for i in range(3)],
                       "clips_remote": True, "clip_sentences": SENTENCES,
                       "clip_durations_s": [1.0, 2.0, 3.0], **extra}}


def _arg(argv, flag):
    return argv[argv.index(flag) + 1]


def test_the_phone_is_followed_on_the_clock(rig, monkeypatch):
    monkeypatch.setattr(cli, "_socket_for", lambda t: "tcp://127.0.0.1:16614")
    assert cli._replay_row(_row()) == 0
    ex = rig["rows"][-1]["extras"]
    assert ex["clip_offsets_s"] == [0.0, 1.0, 3.0]
    assert abs(time.time() - ex["play_started_at"]) < 0.5
    argv = rig["argv"]
    assert json.loads(_arg(argv, "--sentences")) == SENTENCES
    assert json.loads(_arg(argv, "--offsets")) == [0.0, 1.0, 3.0]
    assert _arg(argv, "--pane") == ""


def test_measured_starts_win_over_summed_durations(rig, monkeypatch):
    """A reply that played before carries where each clip really began —
    the gaps between them included."""
    monkeypatch.setattr(cli, "_socket_for", lambda t: "tcp://127.0.0.1:16614")
    cli._replay_row(_row(clip_starts_s=[0.0, 1.4, 3.9]), from_sentence=2)
    ex = rig["rows"][-1]["extras"]
    assert ex["clip_offsets_s"] == [0.0, 1.4, 3.9]
    assert abs((time.time() - ex["play_started_at"]) - 3.9) < 0.5


def test_a_local_player_is_still_asked(rig, monkeypatch, tmp_path):
    """A player on this host answers at once: its position (pauses included)
    beats the clock, so no offsets — but the sentences still go along."""
    monkeypatch.setattr(cli, "_socket_for", lambda t: tmp_path / "mpv.sock")
    cli._replay_row(_row())
    ex = rig["rows"][-1]["extras"]
    assert "clip_offsets_s" not in ex
    argv = rig["argv"]
    assert json.loads(_arg(argv, "--sentences")) == SENTENCES
    assert not json.loads(_arg(argv, "--offsets") or "[]")
