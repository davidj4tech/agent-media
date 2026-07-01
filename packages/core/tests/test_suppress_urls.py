"""Tests for suppress_urls() — TTS shouldn't read long URLs / query strings
aloud, but should keep markdown link text and reduce bare URLs to a spoken
"<host> link" placeholder. See _text.suppress_urls."""

from agent_media_core.intake._text import strip_markdown, suppress_urls


def test_markdown_link_keeps_text_drops_url():
    assert suppress_urls("see [the docs](https://github.com/a/b?x=1)") == \
        "see the docs"


def test_image_keeps_alt_text():
    assert suppress_urls("![a diagram](https://x.io/y.png)") == "a diagram"


def test_empty_link_text_falls_back_to_host():
    assert suppress_urls("[](https://github.com/a/b)") == "github.com link"


def test_autolink_becomes_host_placeholder():
    assert suppress_urls("read <https://www.example.com/path>") == \
        "read example.com link"


def test_bare_url_dropped_to_host():
    assert suppress_urls("go to https://github.com/foo/bar?q=1#frag now") == \
        "go to github.com link now"


def test_www_stripped():
    assert suppress_urls("https://www.google.com/search?q=x") == "google.com link"


def test_trailing_punctuation_kept_outside_url():
    # The sentencer splits on this '.', so it must survive outside the URL.
    assert suppress_urls("see https://github.com/a.") == "see github.com link."


def test_non_scheme_text_untouched():
    # No scheme -> not a URL we rewrite ("e.g." / bare "example.com" survive).
    assert suppress_urls("e.g. example.com and foo.bar") == \
        "e.g. example.com and foo.bar"


def test_empty_input():
    assert suppress_urls("") == ""


def test_integrated_via_strip_markdown():
    # strip_markdown runs suppress_urls after code-block suppression.
    assert strip_markdown("Check [here](https://x.io/y?z=1) out") == \
        "Check here out"
