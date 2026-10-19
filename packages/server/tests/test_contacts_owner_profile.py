"""The owner's own profile from a Facebook or Instagram export
(contacts_import.save_owner_profiles → owner-networks.json, read by
bridges.owner_networks for Settings → Bridges, §6.26 `profile`). Made-up
files only; HOME, the config dir and the state dir are throwaway, and no
MEDIA_* or MATRIX_* setting of this host leaks in."""

from __future__ import annotations

import io
import json
import logging
import os
import stat
import zipfile

import pytest

from agent_media_server import bridges, contacts, contacts_exports, contacts_import, matrix


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for k in list(os.environ):
        if k.startswith(("MATRIX_", "MEDIA_")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("MEDIA_CONTACTS_ADB", str(tmp_path / "no-such-adb") + " shell")
    monkeypatch.setattr(matrix, "_ROOMS", {})
    contacts._reset_for_tests()

    def nowhere(*a, **k):
        raise AssertionError(f"a real Google request: {a[:2]}")
    monkeypatch.setattr(contacts, "_request", nowhere)
    yield
    contacts._reset_for_tests()


def _esc(s: str) -> str:
    """Facebook's way of writing UTF-8 (each byte a code point)."""
    return s.encode("utf-8").decode("latin-1")


FB_PROFILE = {"profile_v2": {
    "name": {"full_name": _esc("Zoë Example"), "first_name": "Zoë", "last_name": "Example"},
    "emails": {"emails": ["zoe@example.invalid", "old@example.invalid"],
               "previous_emails": [], "pending_emails": []},
    "phone_numbers": [{"phone_type": "Mobile", "phone_number": "+61400000123",
                       "verified": True}],
    "username": "zoe.example",
}}

IG_PROFILE = {"profile_user": [{"title": "", "media_map_data": {}, "string_map_data": {
    "Email": {"href": "", "value": "zoe.ig@example.invalid", "timestamp": 0},
    "Phone Number": {"href": "", "value": "+61400000456", "timestamp": 0},
    "Username": {"href": "", "value": "zoe_ig", "timestamp": 0},
    "Name": {"href": "", "value": "Zoe IG", "timestamp": 0},
    "Gender": {"href": "", "value": "female", "timestamp": 0},
}}]}

FRIENDS = {"friends_v2": [{"name": "Bea Friend", "timestamp": 1600000000}]}


def _zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, obj in files.items():
            zf.writestr(name, json.dumps(obj))
    return buf.getvalue()


FB_ZIP = {
    "personal_information/profile_information/profile_information.json": FB_PROFILE,
    "connections/friends/your_friends.json": FRIENDS,
}
IG_ZIP = {"personal_information/personal_information/personal_information.json": IG_PROFILE}


def _saved() -> dict:
    return json.loads(bridges.owner_networks_path().read_text(encoding="utf-8"))


def test_facebook_profile_is_kept_with_the_friends(caplog):
    caplog.set_level(logging.DEBUG)
    got = contacts.import_export(_zip(FB_ZIP), "facebook.zip")
    assert got["profile"] == {"facebook": ["name", "email", "phone", "username"]}
    assert got["imported"].get("facebook_friends") == 1
    assert _saved() == {"facebook": {"name": "Zoë Example", "email": "zoe@example.invalid",
                                     "phone": "+61400000123", "username": "zoe.example"}}
    mode = stat.S_IMODE(os.stat(bridges.owner_networks_path()).st_mode)
    assert mode == 0o600
    assert bridges.owner_networks()["facebook"]["email"] == "zoe@example.invalid"
    # the values are never logged, only the field names
    logged = caplog.text
    for secret in ("zoe@example.invalid", "+61400000123", "zoe.example", "Zoë Example"):
        assert secret not in logged


def test_instagram_profile_alone_is_an_import():
    got = contacts.import_export(_zip(IG_ZIP), "instagram.zip")
    assert got["profile"] == {"instagram": ["name", "email", "phone", "username"]}
    assert _saved()["instagram"] == {"name": "Zoe IG", "email": "zoe.ig@example.invalid",
                                     "phone": "+61400000456", "username": "zoe_ig"}


def test_only_the_fields_present_and_other_networks_kept():
    contacts.import_export(_zip(IG_ZIP), "instagram.zip")
    contacts.import_export(_zip(FB_ZIP), "facebook.zip")
    thin = {"profile_v2": {"name": {"full_name": "Zoe New"}, "emails": {"emails": []},
                           "phone_numbers": []}}
    got = contacts.import_export(_zip({
        "personal_information/profile_information/profile_information.json": thin}), "fb2.zip")
    assert got["profile"] == {"facebook": ["name"]}
    saved = _saved()
    assert saved["facebook"] == {"name": "Zoe New"}             # replaced whole
    assert saved["instagram"]["username"] == "zoe_ig"           # left alone


def test_a_lone_json_file_and_the_path_import(tmp_path):
    got = contacts.import_export(json.dumps(FB_PROFILE).encode(), "profile_information.json")
    assert got["profile"] == {"facebook": ["name", "email", "phone", "username"]}
    p = tmp_path / "instagram-export.zip"
    p.write_bytes(_zip(IG_ZIP))
    got = contacts.import_path(p)
    assert "instagram" in got["profile"] and "facebook" in _saved()


def test_another_book_writes_nothing():
    with contacts.as_account("acct-0123456789abcdef0123"):
        got = contacts.import_export(_zip(FB_ZIP), "facebook.zip")
    assert "profile" not in got
    assert not bridges.owner_networks_path().exists()


def test_not_a_profile_is_ignored():
    assert contacts_import.profile_of({"something": 1}) is None
    assert contacts_import.profile_of([1, 2]) is None
    assert contacts_import.profiles_bytes(b"[1]") == {}


def test_the_cli_says_field_names_only(tmp_path, capsys):
    import argparse

    p = tmp_path / "facebook-x.zip"
    p.write_bytes(_zip(FB_ZIP))
    assert contacts.cmd_import(argparse.Namespace(file=str(p))) == 0
    out = capsys.readouterr().out
    assert "your Facebook profile: name, email, phone, username" in out
    assert "zoe@example.invalid" not in out


def test_the_export_steps_ask_for_the_profile():
    steps = {e["network"]: e["steps"] for e in contacts_exports.export_links()}
    assert "Profile information" in steps["facebook"]
    assert "Personal information" in steps["instagram"]
