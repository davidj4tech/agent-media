"""/speech/ctl: the app's speech bar may use the listener's verbs, and each
of them must be one the canvas can actually run (`ctl_argv`)."""

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
