"""/speech/ctl: the app's speech bar may use the listener's verbs, and each
of them must be one the canvas can actually run (`ctl_argv`) — and that
`media` will actually accept."""

import pytest

from agent_media_visual import canvas


def test_the_bar_gets_only_listener_verbs():
    for action in canvas._APP_SPEECH_ACTIONS:
        assert canvas.ctl_argv("speech", action, 1) is not None
    assert "mute-keep" not in canvas._APP_SPEECH_ACTIONS
    assert "goto" not in canvas._APP_SPEECH_ACTIONS


def test_the_player_gets_the_popups_listening_keys():
    for action in ("para-", "para+", "prev", "replay", "speed+", "speed0", "vol-", "mute"):
        assert action in canvas._APP_SPEECH_ACTIONS
    assert canvas.ctl_argv("speech", "prev", 3) == ["replay-prev", "--idx", "3"]
    assert canvas.ctl_argv("speech", "replay", 2) == ["replay", "2"]
    # A transcript line's own turn, by history id — not an index, not clamped.
    assert canvas.ctl_argv("speech", "replay-id", 48213) == ["replay", "--id", "48213"]


def test_every_verb_the_canvas_sends_is_one_the_cli_accepts():
    """The argv has to parse, not merely exist.

    `goto-sentence` carried `--pane` from the day "read from here" was built,
    and `media skip` never declared it: argparse exited 2 before the player
    was touched, the usage error went to a stderr the canvas does not read,
    and the tap was logged as a success. Three days of "the follow-along
    moves but the audio doesn't" (David, 23 Sep 2026) was this line.

    Building the argv is the canvas's half of the contract; parsing it is the
    CLI's, and nothing checked they met.
    """
    from agent_media_core.cli import _build_parser

    # A remembered reply, as there is after anything is spoken: this is what
    # adds `--pane` to a jump.
    canvas._LAST_CLIP["key"] = "%42"
    parser = _build_parser()
    for action in sorted(canvas._APP_SPEECH_ACTIONS):
        argv = canvas.ctl_argv("speech", action, 1, "3")
        assert argv is not None, action
        try:
            parser.parse_args(argv)
        except SystemExit as e:  # argparse's way of refusing
            pytest.fail(f"media cannot run what the canvas sends for "
                        f"{action}: {' '.join(argv)} ({e})")
