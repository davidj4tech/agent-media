"""opencode's conversations: one SQLite database, JSON in its rows."""

import json
import sqlite3

import pytest

from agent_media_core import harnesses

OC = "ses_f2fa343dcffeKPfrR1z5h6u4NN"
SUB = "ses_f2fa343dcffeKPfrR1z5h6u4NM"
T0 = 1790202133665


def make_db(path):
    """The tables and columns this reads, as opencode 1.18 writes them."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as c:
        c.executescript("""
            create table session (id text primary key, parent_id text, directory text not null,
                title text not null, time_created integer not null, time_updated integer not null,
                time_archived integer);
            create table message (id text primary key, session_id text not null,
                time_created integer not null, data text not null);
            create table part (id text primary key, message_id text not null,
                session_id text not null, time_created integer not null,
                time_updated integer not null, data text not null);
        """)
    return path


def add(path, session, *, parent=None, title="New session - 2026-09-23T22:22:13.539Z",
        directory="/home/x/scratch", at=T0, archived=None, messages=()):
    with sqlite3.connect(path) as c:
        c.execute("insert into session values (?,?,?,?,?,?,?)",
                  (session, parent, directory, title, at, at + 10_000, archived))
        for n, (role, extra, parts) in enumerate(messages):
            mid = f"msg_{session[-6:]}{n:03d}"
            c.execute("insert into message values (?,?,?,?)",
                      (mid, session, at + n * 1000, json.dumps({"role": role, **extra})))
            for k, p in enumerate(parts):
                c.execute("insert into part values (?,?,?,?,?,?)",
                          (f"prt_{mid}{k:03d}", mid, session, at + n * 1000 + k,
                           at + n * 1000 + k, json.dumps(p)))


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    for var, sub in (("CODEX_HOME", "codex"), ("PI_CODING_AGENT_DIR", "pi"),
                     ("CLAUDE_CONFIG_DIR", "claude"), ("HERMES_HOME", "hermes")):
        monkeypatch.setenv(var, str(tmp_path / sub))
    db = make_db(tmp_path / "data" / "opencode" / "opencode.db")
    add(db, OC, messages=[
        ("user", {}, [{"type": "text", "text": "Read a.txt", "synthetic": False},
                      {"type": "text", "text": "<file contents>", "synthetic": True}]),
        ("assistant", {"finish": "stop"}, [{"type": "text", "text": "Done."}]),
    ])
    add(db, SUB, parent=OC, title="a subagent's own session")
    return db


def test_an_opencode_id_is_a_session_id():
    assert harnesses.SESSION_ID.fullmatch(OC)
    assert harnesses.is_opencode(OC) and not harnesses.is_opencode("20260921_102508_f74b02")
    assert not harnesses.is_opencode("ses_short")


def test_it_is_found_and_described(store):
    assert harnesses.harness_of(OC) == harnesses.OPENCODE
    assert harnesses.harness_of("ses_" + "Z" * 26) == ""
    assert harnesses.cwd_of(OC) == "/home/x/scratch"
    # The synthetic part is opencode's attachment, not what was typed.
    assert harnesses.first_prompt(OC) == "Read a.txt"


def test_the_placeholder_title_is_no_title(store):
    assert harnesses.title_of(OC) == ""
    add(store, "ses_" + "a" * 26, title="Fix the flaky test")
    assert harnesses.title_of("ses_" + "a" * 26) == "Fix the flaky test"


def test_stored_lists_top_level_sessions_only(store):
    add(store, "ses_" + "b" * 26, archived=T0)
    rows = [r for r in harnesses.stored() if r.harness == harnesses.OPENCODE]
    assert [r.session for r in rows] == [OC]
    assert rows[0].at == pytest.approx((T0 + 10_000) / 1000.0)


def test_no_database_is_no_conversations(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "nothing"))
    assert harnesses.opencode_rows("select 1") == []
    assert harnesses.harness_of(OC) == ""


def test_resume_and_recipe():
    assert harnesses.resume_argv(harnesses.OPENCODE, OC) == ["--session", OC]
    assert harnesses.fresh_argv(harnesses.OPENCODE, OC) == []
    r = harnesses.RECIPES[harnesses.OPENCODE]
    assert r.package == "opencode-ai" and r.login == ("auth", "login")
    # `auth logout` with no provider is a picker: not something to run blind.
    assert r.logout == ()


LIST_NONE = ("┌  Credentials \x1b[90m/home/x/.local/share/opencode/auth.json\n│\n"
             "└  0 credentials\n")
LIST_ENV = LIST_NONE + ("\n┌  Environment\n│\n●  Anthropic \x1b[90mANTHROPIC_API_KEY\n│\n"
                        "●  Venice AI \x1b[90mVENICE_API_KEY\n")


def test_auth_counts_stored_and_environment_keys():
    assert harnesses._opencode_auth(LIST_ENV) == ("in", "keys for Anthropic, Venice AI")
    one = LIST_NONE.replace("0 credentials", "1 credential")
    assert harnesses._opencode_auth(one) == ("in", "1 credential")


def test_no_sign_in_is_unknown_not_out():
    # Its free models need none, so "out" would wrongly refuse a new chat.
    state, detail = harnesses._opencode_auth(LIST_NONE)
    assert state == "unknown" and "free models" in detail


# --- speech: the plugin says "idle", the hook reads the reply back --------------


def test_the_last_reply_is_everything_said_since_the_prompt(store):
    sid = "ses_" + "c" * 26
    add(store, sid, messages=[
        ("user", {}, [{"type": "text", "text": "first"}]),
        ("assistant", {}, [{"type": "text", "text": "old answer"}]),
        ("user", {}, [{"type": "text", "text": "second"}]),
        ("assistant", {}, [{"type": "reasoning", "text": "hm"},
                           {"type": "text", "text": "Looking."}]),
        ("assistant", {"finish": "stop"}, [{"type": "text", "text": "Found it."}]),
    ])
    mid, text = harnesses.opencode_last_reply(sid)
    assert text == "Looking.\n\nFound it."
    assert mid.endswith("004")
    assert harnesses.opencode_last_reply("ses_" + "d" * 26) == ("", "")


@pytest.fixture
def spoken(store, tmp_path, monkeypatch):
    from agent_media_core.intake import hook_opencode

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    said = []
    monkeypatch.setattr(hook_opencode, "run",
                        lambda source, prefix, *, text, metadata: said.append((text, metadata)) or 0)
    return hook_opencode, said


def test_the_hook_speaks_a_reply_once(spoken):
    hook, said = spoken
    assert hook.main(["--session", OC]) == 0
    assert hook.main(["--session", OC]) == 0
    assert said == [("Done.", {"session": OC})]


def test_a_subagents_idle_is_not_spoken(spoken, store):
    hook, said = spoken
    add(store, "ses_" + "e" * 26, parent=OC, messages=[
        ("assistant", {"finish": "stop"}, [{"type": "text", "text": "subagent notes"}])])
    hook.main(["--session", "ses_" + "e" * 26])
    hook.main(["--session", "not-an-id"])
    assert said == []
