"""The pattern engine: generative SVG with no model, no network, no key.

Determinism is the whole contract here — it is what makes the artwork
reproducible, testable, and cheap enough to be the default.
"""

import xml.etree.ElementTree as ET

import pytest

from agent_media_visual import engines, pattern

REPLY = ("The relay connects each host over the tailnet, and messages route "
         "through the mail server before anyone reads them.")
OTHER = ("Speech plays on the phone: the reply is spoken aloud while the "
         "canvas illustrates it.")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("MEDIA_VISUAL_PATTERN_SEED", "MEDIA_VISUAL_PATTERN_SUBJECT",
                "MEDIA_VISUAL_BEAT", "MEDIA_VISUAL_FIGURE",
                "MEDIA_VISUAL_MONO", "MEDIA_VISUAL_DARK",
                "MEDIA_VISUAL_PATTERN_STYLE",
                "MEDIA_VISUAL_ENGINE", "MEDIA_VISUAL_FALLBACK_ENGINE"):
        monkeypatch.delenv(var, raising=False)


# --- the contract -------------------------------------------------------------

def test_generates_well_formed_svg():
    img, err = pattern.generate(REPLY)
    assert err == "" and img.startswith(b"<svg")
    root = ET.fromstring(img.decode())
    assert root.get("viewBox") == "0 0 1600 900"


def test_deterministic():
    assert pattern.generate(REPLY)[0] == pattern.generate(REPLY)[0]


def test_different_replies_differ():
    assert pattern.generate(REPLY)[0] != pattern.generate(OTHER)[0]


def test_empty_prompt_is_an_error_not_a_crash():
    img, err = pattern.generate("   ")
    assert img is None and "empty" in err


def test_never_reaches_the_network(monkeypatch):
    """The point of the engine. If it ever grows a request, this fails."""
    import urllib.request

    def boom(*a, **k):  # pragma: no cover - only runs on regression
        raise AssertionError("pattern engine made a network call")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert pattern.generate(REPLY)[1] == ""


def test_no_scripts_or_external_refs():
    svg = pattern.generate(REPLY)[0].decode().lower()
    body = svg.replace("http://www.w3.org/", "")
    for bad in ("<script", "<foreignobject", "http://", "https://", "javascript:"):
        assert bad not in body


# --- motif selection ----------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("The relay connects each host; messages route through the server.", "network"),
    ("Speech and audio: the voice signal is spoken aloud.", "waves"),
    ("The store keeps a history of every record in the archive.", "strata"),
    ("Search deeper and trace the question inward to find detail.", "spiral"),
    ("The pipeline streams each step through the queue.", "flow"),
])
def test_motif_follows_the_words(text, expected):
    import random
    assert pattern.choose_motif(text, random.Random(0)) == expected


def test_motiveless_text_still_draws_something():
    import random
    assert pattern.choose_motif("ok", random.Random(0)) in pattern.MOTIF_NAMES


def test_keywords_skip_stopwords():
    words = pattern.keywords("The canvas and the canvas and the relay", 2)
    assert "canvas" in words and "the" not in words


# --- continuity ---------------------------------------------------------------

def _bg(svg: bytes) -> str:
    """The backdrop gradient stops identify the palette."""
    root = ET.fromstring(svg.decode())
    stops = root.findall(".//{http://www.w3.org/2000/svg}stop")
    return "".join(s.get("stop-color", "") for s in stops[:2])


def test_one_session_keeps_one_palette(monkeypatch):
    monkeypatch.setenv("MEDIA_VISUAL_PATTERN_SEED", "session-abc")
    first = pattern.generate(REPLY)[0]
    second = pattern.generate(OTHER)[0]
    assert _bg(first) == _bg(second)
    assert first != second  # same palette, different picture


def test_beats_share_a_subject_and_advance(monkeypatch):
    monkeypatch.setenv("MEDIA_VISUAL_PATTERN_SEED", "session-abc")
    monkeypatch.setenv("MEDIA_VISUAL_PATTERN_SUBJECT", REPLY)
    frames = []
    for i in (1, 2, 3):
        monkeypatch.setenv("MEDIA_VISUAL_BEAT", f"{i}/3")
        frames.append(pattern.generate(f"part {i} of the reply")[0])
    assert len({_bg(f) for f in frames}) == 1       # one scene
    assert len(set(frames)) == 3                    # three moments


def test_malformed_beat_is_a_single_image(monkeypatch):
    monkeypatch.setenv("MEDIA_VISUAL_BEAT", "nonsense")
    assert pattern.generate(REPLY)[1] == ""
    assert pattern._beat() == (1, 1)


# --- modes --------------------------------------------------------------------

def test_mono_mode_is_flat_for_eink(monkeypatch):
    """DU4 shows four greys and dithers everything else, so mono emits no
    gradients and no partial opacity."""
    monkeypatch.setenv("MEDIA_VISUAL_MONO", "1")
    svg = pattern.generate(REPLY)[0].decode()
    assert "linearGradient" not in svg and "radialGradient" not in svg
    for chunk in svg.split('opacity="')[1:]:
        assert chunk.split('"')[0] == "1"


def test_light_mode_uses_a_light_backdrop(monkeypatch):
    monkeypatch.setenv("MEDIA_VISUAL_DARK", "0")
    assert _bg(pattern.generate(REPLY)[0]) in [
        p.bg[0] + p.bg[1] for p in pattern.LIGHT_PALETTES]


def test_figure_mode_labels_the_subject(monkeypatch):
    monkeypatch.setenv("MEDIA_VISUAL_PATTERN_STYLE", "art")
    monkeypatch.setenv("MEDIA_VISUAL_FIGURE", "1")
    svg = pattern.generate(REPLY)[0].decode()
    assert "<text" in svg
    assert all(w in svg for w in pattern.keywords(REPLY, 3))


def test_art_style_is_wordless(monkeypatch):
    monkeypatch.setenv("MEDIA_VISUAL_PATTERN_STYLE", "art")
    assert "<text" not in pattern.generate(REPLY)[0].decode()


# --- the card -----------------------------------------------------------------

CARD_REPLY = ("The canvas draws itself now. Venice was billing 364 images a "
              "day for beats alone. Three built-ins live in engines.py, and "
              "the work touched pattern.py and cli.py.")


def _texts(svg: bytes):
    root = ET.fromstring(svg.decode())
    return [(el.text or "") for el in
            root.findall(".//{http://www.w3.org/2000/svg}text")]


def test_card_is_the_default(monkeypatch):
    assert pattern.card_style()
    assert "<text" in pattern.generate(CARD_REPLY)[0].decode()


def test_card_sets_the_words_that_were_said():
    said = " ".join(_texts(pattern.generate(CARD_REPLY)[0]))
    assert "canvas draws itself" in said


def test_card_carries_the_figures_and_the_filenames(monkeypatch):
    monkeypatch.setenv("MEDIA_VISUAL_PATTERN_SUBJECT", CARD_REPLY)
    said = " ".join(_texts(pattern.generate("Venice was billing a lot.")[0]))
    assert "364" in said and "images" in said
    assert "engines.py" in said and "pattern.py" in said


def test_headline_is_the_opening_sentence():
    assert pattern.headline(CARD_REPLY) == "The canvas draws itself now."


def test_figures_pair_a_number_with_what_it_counts():
    assert ("364", "images a day") == pattern.figures(CARD_REPLY)[0]


def test_files_are_deduped_basenames():
    assert pattern.files("see packages/visual/cli.py and cli.py") == ["cli.py"]


def test_long_lines_stay_inside_the_frame():
    """A line that overruns the viewBox is cut off mid-word on the wall."""
    long_reply = "supercalifragilistic " * 40
    for line in pattern._wrap(long_reply, 88, pattern.CARD_W, 4):
        assert len(line) * 88 * pattern._CHAR_W <= pattern.CARD_W


def test_the_card_shows_how_far_through_the_reply_it_is(monkeypatch):
    monkeypatch.setenv("MEDIA_VISUAL_PATTERN_SUBJECT", CARD_REPLY)
    widths = []
    for i in (1, 3):
        monkeypatch.setenv("MEDIA_VISUAL_BEAT", f"{i}/3")
        root = ET.fromstring(pattern.generate("a part")[0].decode())
        rects = root.findall(".//{http://www.w3.org/2000/svg}rect")
        widths.append(float(rects[-1].get("width")))
    assert widths[0] < widths[1]


def test_mono_card_is_type_alone(monkeypatch):
    """E-ink has no faint: a tint behind the words is a tone competing
    with them."""
    monkeypatch.setenv("MEDIA_VISUAL_MONO", "1")
    svg = pattern.generate(CARD_REPLY)[0].decode()
    assert "<text" in svg and "filter" not in svg


# --- registration -------------------------------------------------------------

def test_pattern_is_the_default_engine():
    assert engines.default_engine() == "pattern"
    assert "pattern" in engines.all_engine_names()
    assert not engines.needs_shaping(None)


def test_pattern_is_the_last_resort(monkeypatch):
    """A failing primary must land somewhere that cannot itself fail for
    want of a key — which is the whole reason the default changed."""
    monkeypatch.setenv("MEDIA_VISUAL_ENGINE", "nope-not-installed")
    img, err = engines.generate_image(REPLY)
    assert err == "" and img.startswith(b"<svg")
