"""Beat splitting, storyboard shaping, and the canvas's sequence endpoint."""

from agent_media_visual import cli, generate


# --- split_beats ----------------------------------------------------------------

PARA = ("First paragraph about the setup and what was attempted here.\n\n"
        "Second paragraph describing the obstacle that appeared midway.\n\n"
        "Third paragraph on how it was resolved and what happens next.")


def test_paragraph_split_with_fractions():
    beats = cli.split_beats(PARA, 4)
    assert len(beats) == 3
    fracs = [f for f, _ in beats]
    assert fracs[0] == 0.0
    assert fracs == sorted(fracs) and all(0 <= f < 1 for f in fracs)
    assert beats[1][1].startswith("Second paragraph")


def test_short_reply_gets_no_beats():
    assert cli.split_beats("One short thought.", 4) is None
    assert cli.split_beats("Two thoughts. Still short.", 4) is None


def test_single_paragraph_splits_by_sentences():
    text = ("Alpha is done. Beta came next and worked well. Gamma failed at "
            "first. Delta fixed it. Epsilon wrapped everything up nicely. "
            "Zeta is the plan for tomorrow.")
    beats = cli.split_beats(text, 3)
    assert beats is not None and 2 <= len(beats) <= 3
    assert "".join(p for _, p in beats).count("Alpha") == 1


def test_many_paragraphs_merge_to_max():
    text = "\n\n".join(f"Paragraph number {i} with some words in it." for i in range(9))
    beats = cli.split_beats(text, 4)
    assert len(beats) == 4


def test_code_fences_do_not_skew_pacing():
    text = ("Intro paragraph before any code appears in the reply.\n\n"
            "```\n" + "x = 1\n" * 200 + "```\n\n"
            "Closing paragraph after the enormous code block ends.")
    beats = cli.split_beats(text, 4)
    assert len(beats) == 2
    # Without the fence-strip the second beat would start near 1.0.
    assert beats[1][0] < 0.7


# --- shape_story: scene + storyboard in ONE call ---------------------------------

def test_shape_story_scene_plus_beats(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    seen = {}

    def fake_chat(system, user, timeout):
        seen["system"], seen["user"] = system, user
        return ("the whole scene\n"
                "1) beat one prompt\n2. beat two prompt\n- beat three prompt")

    monkeypatch.setattr(generate, "_gateway_chat", fake_chat)
    scene, beats, used = generate.shape_story(
        "the reply", ["part a", "part b", "part c"], session="s1")
    assert used and scene == "the whole scene"
    assert beats == ["beat one prompt", "beat two prompt", "beat three prompt"]
    assert "3) part c" in seen["user"]
    # The scene lands in the continuity memory for the next reply.
    from agent_media_visual import state
    assert state.load_scene("s1") == "the whole scene"


def test_shape_story_offers_previous_scene(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    from agent_media_visual import state
    state.save_scene("s1", "a lighthouse at dusk")
    seen = {}

    def fake_chat(system, user, timeout):
        seen["user"] = user
        return "evolved scene\nbeat one\nbeat two"

    monkeypatch.setattr(generate, "_gateway_chat", fake_chat)
    scene, beats, used = generate.shape_story("r", ["a", "b"], session="s1")
    assert "a lighthouse at dusk" in seen["user"]
    assert scene == "evolved scene" and len(beats) == 2


def test_shape_story_count_mismatch_keeps_scene_drops_beats(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(generate, "_gateway_chat",
                        lambda *a, **k: "just the scene line")
    scene, beats, used = generate.shape_story("r", ["a", "b", "c"])
    assert used and scene == "just the scene line" and beats is None


def test_shape_story_gateway_failure_falls_back_to_raw(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(generate, "_gateway_chat", lambda *a, **k: None)
    scene, beats, used = generate.shape_story("raw reply text", ["a", "b"])
    assert not used and beats is None and scene.startswith("raw reply text")


# --- canvas /show sequence validation --------------------------------------------

def test_show_sequence_event_shape():
    from agent_media_visual import canvas
    hub = canvas.Hub()
    hub.publish({"sequence": [{"image": "/img/a.webp", "at": 0.0},
                              {"image": "/img/b.webp", "at": 0.5}],
                 "estdur": 30.0})
    assert hub.last["sequence"][1]["at"] == 0.5


def test_show_image_passes_purpose_through():
    from agent_media_visual import canvas
    hub = canvas.Hub()
    canvas.HUB = hub  # not used by Hub tests directly; guard against globals
    hub.publish({"image": "/img/f.svg", "purpose": "figure"})
    assert hub.last["purpose"] == "figure"


def test_ctl_skip_actions_all_channels():
    from agent_media_visual import canvas
    assert canvas.ctl_argv("speech", "skip+", 1) == [
        "skip", "--unit", "sentence", "--dir", "1", "--seek-fallback", "5"]
    assert canvas.ctl_argv("speech", "para-", 1) == [
        "skip", "--unit", "paragraph", "--dir", "-1", "--seek-fallback", "-30"]
    assert canvas.ctl_argv("music", "skip-", 1) == ["music", "seek", "-5"]
    assert canvas.ctl_argv("book", "para+", 1) == ["book", "seek", "+30"]
    assert canvas.ctl_argv("speech", "web", 1) == ["speech-web"]
    assert canvas.ctl_argv("music", "web", 1) == ["music-web"]
    assert canvas.ctl_argv("book", "web", 1) == ["book-web"]
