"""The app's language, site-wide: a code in the state dir, "en" unless set."""

from agent_media_core import language
from agent_media_core._paths import state_dir


def test_the_language_is_english_until_set_and_never_raises():
    assert language.current() == "en"
    language.set_language("FR")
    assert language.current() == "fr"
    (state_dir() / language.FILE_NAME).write_text("{not json")
    assert language.current() == "en"
    (state_dir() / language.FILE_NAME).write_text('["fr"]')
    assert language.current() == "en"
    assert language.NAMES["zh"] == "Chinese (Simplified)"


def test_a_language_is_chosen_with_its_country(tmp_path, monkeypatch):
    from agent_media_core import language

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert language.locale() == "en-AU", "English means Australia unless told"
    language.set_language("fr_ca")
    assert (language.current(), language.locale()) == ("fr", "fr-CA")
    language.set_language("de")
    assert (language.current(), language.locale()) == ("de", "de-DE")
    assert language.normalise("ZH-cn") == "zh-CN"
