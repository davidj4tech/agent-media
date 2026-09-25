"""`media replay --id N --from-sentence K`: "read from here" on an old reply.

One command, so the app never has to replay and then seek (a race: the seek
can land before the replay has loaded, or on the reply that was playing).
The index counts `replay_sentence_map(row)` — what `/speech/sentences` hands
the app — and the replay starts there without playing what comes before.
"""

import time

import pytest

from agent_media_core import cli


class _Sink:
    def __init__(self):
        self.calls = []

    def prefetch(self, paths, target=None):
        return True

    def play(self, uri, target=None, **kw):
        self.calls.append(("play", uri))

    def play_playlist(self, uris, target=None, gapless=True, start=0):
        self.calls.append(("play_playlist", list(uris), start))


class _Proc:
    pid = 4242


@pytest.fixture
def rig(monkeypatch):
    """Replay with the player, the follower and the row write recorded."""
    sink = _Sink()
    rec = {"sink": sink, "rows": [], "seeks": []}
    monkeypatch.setattr(cli, "SinkSpeech", lambda: sink)
    monkeypatch.setattr(cli, "_replay_visual", lambda ex: None)
    monkeypatch.setattr(cli.ipc, "set_property", lambda *a, **k: None)

    def command(sock, *args, **kw):
        if args and args[0] == "seek":
            rec["seeks"].append(args[1])

    monkeypatch.setattr(cli.ipc, "command", command)
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: _Proc())
    monkeypatch.setattr(cli.StateStore, "set_now_playing",
                        lambda self, sink, **kw: rec["rows"].append(kw))
    monkeypatch.setattr(cli._speech_sink, "set_media_title", lambda *a, **k: None)
    monkeypatch.setattr(cli._speech_sink, "set_reply_text", lambda *a, **k: None)
    return rec


SENTENCES = ["One.", "Two.", "Three.", "Four."]


def _playlist_row():
    return {"uri": "/c/0.mp3", "text": " ".join(SENTENCES),
            "extras": {"clip_uris": [f"remote-{i}.mp3" for i in range(4)],
                       "clips_remote": True, "clip_sentences": SENTENCES,
                       "clip_durations_s": [1.0, 2.0, 3.0, 4.0]}}


def _one_clip_row():
    return {"uri": "remote-all.mp3", "text": " ".join(SENTENCES),
            "extras": {"clip_uris": ["remote-all.mp3"], "clips_remote": True,
                       "clip_sentences": SENTENCES,
                       "clip_offsets_s": [0.0, 1.5, 3.0, 6.5],
                       "total_duration_s": 9.0}}


def test_the_map_is_the_sentences_a_replay_can_start_at():
    assert cli.replay_sentence_map(_playlist_row()) == SENTENCES
    assert cli.replay_sentence_map(_one_clip_row()) == SENTENCES
    swept = {"uri": "x", "extras": {"clip_sentences": SENTENCES}}
    assert cli.replay_sentence_map(swept) == []           # one clip, no timeline
    assert cli.replay_sentence_map({"uri": "x", "extras": None}) == []


def test_a_playlist_starts_on_the_sentence_in_one_batch(rig):
    before = time.time()
    assert cli._replay_row(_playlist_row(), from_sentence=2) == 0
    assert rig["sink"].calls == [("play_playlist",
                                  [f"remote-{i}.mp3" for i in range(4)], 2)]
    ex = rig["rows"][-1]["extras"]
    assert ex["current_sentence_idx"] == 2
    assert ex["current_sentence"] == "Three."
    # The clock counts as if sentences 0 and 1 (1 s + 2 s) had played.
    assert before - 3.0 - 0.5 <= ex["play_started_at"] <= time.time() - 3.0 + 0.01


def test_one_clip_seeks_to_where_the_sentence_begins(rig):
    assert cli._replay_row(_one_clip_row(), from_sentence=3) == 0
    assert rig["sink"].calls == [("play", "remote-all.mp3")]
    assert rig["seeks"] == [6.5]
    ex = rig["rows"][-1]["extras"]
    assert ex["current_sentence_idx"] == 3
    assert abs((time.time() - ex["play_started_at"]) - 6.5) < 0.5


def test_past_the_end_is_the_last_sentence(rig):
    cli._replay_row(_playlist_row(), from_sentence=99)
    assert rig["sink"].calls[0][2] == 3


def test_without_a_sentence_it_plays_from_the_top(rig):
    cli._replay_row(_playlist_row())
    assert rig["sink"].calls[0][2] == 0
    assert rig["rows"][-1]["extras"]["current_sentence_idx"] == 0
    assert "play_started_at" not in rig["rows"][-1]["extras"]


def test_a_row_with_no_map_ignores_the_sentence(rig):
    row = {"uri": "remote-all.mp3", "text": "x",
           "extras": {"clip_uris": ["remote-all.mp3"], "clips_remote": True}}
    assert cli._replay_row(row, from_sentence=2) == 0
    assert rig["seeks"] == []


def test_the_cli_takes_the_flag(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "_speech_history",
                        lambda n, **kw: [{"id": 7, **_playlist_row()}])
    monkeypatch.setattr(cli, "_replay_row",
                        lambda row, from_sentence=None: seen.update(
                            id=row["id"], at=from_sentence) or 0)
    args = cli._build_parser().parse_args(["replay", "--id", "7", "--from-sentence", "2"])
    assert args.func(args) == 0
    assert seen == {"id": 7, "at": 2}


def test_a_jump_in_a_playlist_replay_moves_its_clock(monkeypatch):
    """`media skip --to` on a replay played as a playlist: nothing else moves
    `play_started_at`, so the reader's bold went back to the old place."""
    rows = []
    monkeypatch.setattr(cli.StateStore, "set_now_playing",
                        lambda self, sink, **kw: rows.append(kw))
    ex = {"history_id": 9, "clip_durations_s": [1.0, 2.0, 3.0, 4.0],
          "clip_sentences": SENTENCES, "play_started_at": 0.0}
    cli._restamp_replay_clock({"uri": "u", "target": "app"}, ex, 3, 4)
    got = rows[-1]["extras"]
    assert got["current_sentence_idx"] == 3 and got["current_sentence"] == "Four."
    assert abs((time.time() - got["play_started_at"]) - 6.0) < 0.5
    # Paused: counted from the pause, so the reading stays frozen there.
    ex2 = dict(ex, paused_at=1000.0)
    cli._restamp_replay_clock({"uri": "u"}, ex2, 1, 4)
    assert rows[-1]["extras"]["play_started_at"] == 999.0
    # A live reply's row is its own clock's: left alone.
    n = len(rows)
    cli._restamp_replay_clock({"uri": "u"}, {"clip_durations_s": [1.0] * 4}, 2, 4)
    assert len(rows) == n


def test_replay_inside_a_thread_stays_in_it(monkeypatch):
    """`media replay --session` (the app's Replay inside a thread) replays
    that thread's newest reply, whatever spoke last elsewhere."""
    from agent_media_core import cli

    seen = []
    monkeypatch.setattr(cli, "_do_replay", lambda i, session=None: seen.append((i, session)) or 0)
    monkeypatch.setattr(cli, "_anchor_session", lambda: "somewhere-else")
    import argparse

    assert cli.cmd_replay(argparse.Namespace(index=1, id=None, session="sess-x")) == 0
    assert seen == [(1, "sess-x")]


def test_replay_resumes_a_kept_stop_however_old(monkeypatch):
    """`extras.stopped_at` on the row wins over the five-minute stamp."""
    from agent_media_core import cli
    from agent_media_core.sinks import speech as SP

    monkeypatch.setattr(SP, "last_stop", lambda: {})
    monkeypatch.setattr(cli, "replay_sentence_map", lambda row: [0, 1, 2, 3])
    row = {"id": 7, "extras": {"stopped_at": {"sentence": 2, "at": 0}}}
    assert cli._resume_point(row, None) == 2
    assert cli._resume_point({"id": 8, "extras": {}}, None) is None
