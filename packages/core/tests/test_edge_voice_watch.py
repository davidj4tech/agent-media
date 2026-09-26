"""The Natasha watch: the app's handshake values, edge-tts's, and the verdict."""

from agent_media_core.entrypoints import edge_voice_watch as w

APP_JAVA = '''
    private static final String TOKEN = "6A5AA1D4EAFF4E9FB37E23D68491D6F4";
    private static final String CHROMIUM = "143.0.3650.75";
'''
UPSTREAM_PY = '''
TRUSTED_CLIENT_TOKEN = "6A5AA1D4EAFF4E9FB37E23D68491D6F4"
CHROMIUM_FULL_VERSION = "150.0.1.2"
'''
APP = {"TOKEN": "6A5AA1D4EAFF4E9FB37E23D68491D6F4", "CHROMIUM": "143.0.3650.75"}


def test_the_values_are_read_from_both_sources():
    assert w.app_constants(APP_JAVA) == APP
    assert w.upstream_constants(UPSTREAM_PY) == dict(APP, CHROMIUM="150.0.1.2")


def test_the_token_is_the_apps():
    # EdgeVoiceTest pins the same instant to the same value.
    assert w.sec_ms_gec(APP["TOKEN"], 1790440000.0) == \
        "A2B08FD3F17FAFC219945C68A156129D414F0CF73B3383AD93ED31FC20449C8C"


def test_the_verdicts():
    assert w.verdict(APP, dict(APP), (9000, ""))[0] == "ok"
    assert w.verdict(APP, None, (9000, ""))[0] == "ok", "GitHub down is not the voice failing"
    level, _, detail = w.verdict(APP, dict(APP, CHROMIUM="150.0.1.2"), (9000, ""))
    assert level == "warn" and "150.0.1.2" in detail
    level, _, detail = w.verdict(APP, dict(APP, CHROMIUM="150.0.1.2"), (0, "refused (403)"))
    assert level == "needs" and "Google" in detail and "150.0.1.2" in detail
