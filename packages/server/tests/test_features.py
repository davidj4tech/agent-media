"""`GET /features` (server-contract.md §6.11a): which optional parts this
server has, so the app shows only those. A bare server answers all false; each
part turns true from its own local sign, and nothing is asked over the network.
"""

from __future__ import annotations

from agent_media_server import app, features

from test_contract import AUTH, call, server, signed_in, typed  # noqa: F401

PARTS = {"org", "music", "contacts", "bridges", "personas"}


def _bare(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_ORG_DIR", str(tmp_path / "org"))
    monkeypatch.setenv("MATRIX_BRIDGES", str(tmp_path / "mautrix-*" / "config.yaml"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))


def test_a_bare_server_has_none_of_them(server, signed_in, monkeypatch, tmp_path):
    _bare(monkeypatch, tmp_path)
    res, obj = call(server, "GET", "/features", headers=AUTH)
    assert res.status == 200
    assert obj == {"ok": True, **{p: False for p in PARTS}}
    assert "/features" in app.CORS_PATHS


def test_each_part_turns_on_from_its_own_sign(server, signed_in, monkeypatch, tmp_path):
    _bare(monkeypatch, tmp_path)
    from agent_media_server import org

    root = tmp_path / "org"
    root.mkdir()
    (root / org.profile().capture_file).write_text("* inbox\n")
    monkeypatch.setenv("MEDIA_DUCK_ROOMS_STREAM", "am-music")
    (tmp_path / "config" / "agent-media").mkdir(parents=True)
    (tmp_path / "config" / "agent-media" / "google-oauth-client.json").write_text("{}")
    bridge = tmp_path / "mautrix-signal"
    bridge.mkdir()
    (bridge / "config.yaml").write_text(
        "appservice:\n  id: signal\n  port: 29328\n  hostname: 127.0.0.1\n"
        "provisioning:\n  shared_secret: s3cret\n")
    personas = tmp_path / "personas"
    personas.mkdir()
    monkeypatch.setenv("MEDIA_PERSONAS_DIR", str(personas))
    _, obj = call(server, "GET", "/features", headers=AUTH)
    assert obj == {"ok": True, **{p: True for p in PARTS}}


def test_music_from_the_play_history(signed_in, monkeypatch, tmp_path):
    _bare(monkeypatch, tmp_path)
    from agent_media_core.state import StateStore

    assert features.detect()["music"] is False
    StateStore().note_play("music", "yt:https://www.youtube.com/watch?v=aaaaaaaaaaa", title="A song")
    assert features.detect()["music"] is True


def test_a_check_that_fails_is_a_part_not_shown(monkeypatch, tmp_path):
    _bare(monkeypatch, tmp_path)

    def boom():
        raise RuntimeError("no")

    monkeypatch.setitem(features.CHECKS, "bridges", boom)
    assert features.detect()["bridges"] is False


# The refusals are test_contract's GATED_GETS, which lists /features.
