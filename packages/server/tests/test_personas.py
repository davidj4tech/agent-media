"""Personas and roles in presets and /ask (personas.py, contract §6.24).

`persona-render` is a tiny fake in tmp that logs its arguments; no real
~/agent-personas, no tmux, no panes (the fakes are test_layout_desk.py's).
"""

import json
import os
import stat
import sys
import uuid

import pytest

from agent_media_server import auth_abs, driver, persona_scope, personas, presets, routing, send, sessions

FAKE_RENDER = r'''#!{python}
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_RENDER_LOG"], "a") as fh:
    fh.write(json.dumps(args) + "\n")
if args == ["--list", "--json"] and os.environ.get("FAKE_JSON"):
    print(json.dumps({{"personas": [
        {{"id": "zara", "kind": "persona", "adapters": ["pi", "claude"], "projects": None, "private": True}},
        {{"id": "sam", "kind": "persona", "adapters": None, "projects": ["agent-media"], "private": False}}],
        "roles": [
        {{"id": "reviewer", "kind": "role", "adapters": ["claude", "codex"], "projects": None, "private": False}},
        {{"id": "planner", "kind": "role", "adapters": None, "projects": None, "private": False}}],
        "adapters": ["claude", "pi"]}}))
    sys.exit(0)
if args == ["--list"]:
    print("personas:\n  zara\n  sam\nroles:\n  reviewer\n  planner\nadapters:\n  claude\n  pi")
    sys.exit(0)
if args[0] == "broken":
    print("Traceback (most recent call last):", file=sys.stderr)
    print("ValueError: zara.md has no front matter", file=sys.stderr)
    sys.exit(1)
if args[0] not in ("zara", "sam", "reviewer", "planner"):
    print(f"Unknown persona/role {{args[0]!r}}", file=sys.stderr)
    sys.exit(1)
opts = dict(zip(args[1::2], args[2::2]))
print(f"# Persona Render: {{args[0]}}\n\nadapter={{opts.get('--adapter')}} "
      f"role={{opts.get('--role', '')}} mode={{opts.get('--mode')}}")
'''


@pytest.fixture
def repo(monkeypatch, tmp_path):
    root = tmp_path / "agent-personas"
    (root / "bin").mkdir(parents=True)
    script = root / "bin" / "persona-render"
    script.write_text(FAKE_RENDER.format(python=sys.executable))
    script.chmod(0o755)
    log = tmp_path / "render.log"
    monkeypatch.setenv("MEDIA_PERSONAS_DIR", str(root))
    monkeypatch.setenv("FAKE_RENDER_LOG", str(log))
    monkeypatch.setenv("MEDIA_PERSONAS_CONFIG", str(tmp_path / "personas.toml"))
    run = tmp_path / "run"
    run.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(run))

    def calls():
        try:
            return [json.loads(ln) for ln in log.read_text().splitlines()]
        except OSError:
            return []

    ns = type("Repo", (), {})()
    ns.root, ns.run, ns.calls = root, run, calls
    return ns


@pytest.fixture
def asker(monkeypatch):
    """A fresh pane chat with everything faked: what was opened and typed."""
    got = {"opened": [], "typed": [], "session": str(uuid.uuid4())}
    monkeypatch.setattr(send, "_record_turn", lambda s, t, p="": None)
    monkeypatch.setattr(send, "_agent_unready", lambda agent: "")
    monkeypatch.setattr(send, "_settle", lambda p, timeout=5.0: None)
    monkeypatch.setattr(send, "_ensure_submitted", lambda p, t, timeout=3.0, agent="claude": True)
    monkeypatch.setattr(send, "_send_to_pane", lambda p, t: got["typed"].append(t) or "")
    monkeypatch.setattr(auth_abs, "abs_identity", lambda b: ({"username": "d", "type": "root"}, 200))
    monkeypatch.setattr(sessions, "session_of_pane",
                        lambda p, timeout=10.0, agent="claude": got["session"])
    monkeypatch.setattr(send, "open_window",
                        lambda s, cwd, *, resume, host="", flags=(), agent="claude":
                        got["opened"].append((agent, list(flags))) or ("%9", ""))
    return got


def test_the_lists_come_from_persona_render(repo):
    assert personas.listing() == {"personas": ["zara", "sam"], "roles": ["reviewer", "planner"], "private": []}


def test_a_persona_with_private_material_is_listed_private(repo):
    (repo.root / "private").mkdir()
    for name in ("zara.sops.md", "samantha.sops.md", "household-lore.sops.md", "README.md"):
        (repo.root / "private" / name).write_text("")
    assert personas.listing()["private"] == ["zara"]       # not sam: samantha is another id
    assert personas.private_ids(["household", "sam"]) == ["household"]


def test_on_windows_the_python_renderer_runs_under_our_python(repo):
    """Windows cannot exec a shebang script (WinError 193)."""
    exe = str(personas.renderer())
    assert personas._command() == ([sys.executable, exe] if os.name == "nt" else [exe])
    assert personas._command(windows=True) == [sys.executable, exe]
    personas.renderer().write_text("@echo off\r\n")      # not a Python script: as it is
    assert personas._command(windows=True) == [exe]


def test_no_repo_is_empty_lists_not_an_error(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_PERSONAS_DIR", str(tmp_path / "nowhere"))
    assert personas.listing() == {"personas": [], "roles": [], "private": []}
    monkeypatch.setattr(auth_abs, "abs_identity", lambda b: ({"username": "d", "type": "root"}, 200))
    ok, d = presets.listing("tok")
    assert ok and d["personas"] == [] and d["roles"] == [] and d["private"] == []


def test_presets_carry_a_persona_and_role_and_list_them(repo, monkeypatch):
    monkeypatch.setattr(auth_abs, "abs_identity", lambda b: ({"username": "d", "type": "root"}, 200))
    ok, d = presets.put({"name": "Review", "agent": "codex", "persona": "zara", "role": "reviewer"}, "tok")
    assert ok and (d["preset"]["persona"], d["preset"]["role"]) == ("zara", "reviewer")
    assert presets.put({"name": "Plain"}, "tok")[1]["preset"]["persona"] == ""
    ok, d = presets.put({"name": "X", "persona": "nobody"}, "tok")
    assert not ok and d["status"] == 400
    assert not presets.put({"name": "X", "role": "zara"}, "tok")[0]     # a persona is not a role
    ok, d = presets.listing("tok")
    assert d["personas"] == ["zara", "sam"] and d["roles"] == ["reviewer", "planner"]
    assert [(p["name"], p["persona"], p["role"]) for p in d["presets"]] == [
        ("Review", "zara", "reviewer"), ("Plain", "", "")]


def test_claude_gets_the_render_as_an_appended_system_prompt_file(repo, asker):
    ok, d = send.ask("hello there", "tok", agent="claude", persona="zara", role="reviewer")
    assert ok, d
    assert (d["persona"], d["role"], d["persona_via"]) == ("zara", "reviewer", "system_prompt")
    assert repo.calls()[-1] == ["zara", "--adapter", "claude", "--mode", "public", "--role", "reviewer"]
    agent, flags = asker["opened"][0]
    path = flags[flags.index("--append-system-prompt-file") + 1]
    assert path.startswith(str(repo.run / "agent-media" / "personas"))
    assert "adapter=claude role=reviewer mode=public" in open(path).read()
    if os.name != "nt":     # Windows has no POSIX modes: st_mode is 0o666 whatever was asked
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    # Kept, and named for its session too.
    assert (repo.run / "agent-media" / "personas" / f"{asker['session']}.md").read_text() == open(path).read()
    # The words go in as they were.
    assert asker["typed"] == ["hello there"]


def test_pi_gets_the_file_as_its_append_system_prompt(repo, asker):
    ok, d = send.ask("hi", "tok", agent="pi", persona="sam")
    assert ok and d["persona_via"] == "system_prompt"
    _agent, flags = asker["opened"][0]
    path = flags[flags.index("--append-system-prompt") + 1]
    assert "adapter=pi" in open(path).read()


@pytest.mark.parametrize("agent", ["codex", "hermes", "opencode"])
def test_the_rest_get_it_ahead_of_the_first_message(repo, asker, agent):
    ok, d = send.ask("fix the build", "tok", agent=agent, role="reviewer")
    assert ok and (d["persona"], d["role"], d["persona_via"]) == ("", "reviewer", "prepend")
    # A role alone is rendered as its own id.
    assert repo.calls()[-1] == ["reviewer", "--adapter", agent, "--mode", "public"]
    _agent, flags = asker["opened"][0]
    assert not any("system-prompt" in f for f in flags)
    typed = " ".join(asker["typed"][0].split())
    assert typed.startswith("# Persona # Persona Render: reviewer")
    assert typed.endswith("fix the build")


def test_unknown_ids_are_400_and_nothing_opens(repo, asker):
    ok, d = send.ask("hi", "tok", persona="nobody")
    assert not ok and d["status"] == 400 and "nobody" in d["error"]
    ok, d = send.ask("hi", "tok", role="zara")
    assert not ok and d["status"] == 400
    assert asker["opened"] == []


def test_no_repo_means_any_persona_is_unknown(asker, monkeypatch, tmp_path):
    ok, d = send.ask("hi", "tok", persona="zara")
    assert not ok and d["status"] == 400 and asker["opened"] == []


def test_a_failed_render_is_502_with_its_last_line(repo, asker, monkeypatch):
    monkeypatch.setattr(personas, "described", lambda: [{"id": "broken", "kind": "persona", "adapters": None, "projects": None, "private": False}])
    ok, d = send.ask("hi", "tok", persona="broken")
    assert not ok and d["status"] == 502
    assert d["error"].endswith("ValueError: zara.md has no front matter")
    assert asker["opened"] == []


class _FakeHeadless:
    kind = driver.HEADLESS

    def __init__(self, takes):
        self.takes, self.started = takes, []

    def takes_system_prompt(self):
        return self.takes

    def start(self, **kw):
        self.started.append(kw)
        return True, {"session": "s-1", "pane": None, "agent": kw["agent"]}


@pytest.mark.parametrize("takes", [True, False])
def test_headless_claude_passes_the_file_unless_sessiond_is_older(repo, asker, monkeypatch, takes):
    fake = _FakeHeadless(takes)
    monkeypatch.setattr(driver, "for_new", lambda agent: fake)
    ok, d = send.ask("hi", "tok", agent="claude", persona="zara")
    assert ok
    kw = fake.started[0]
    if takes:
        assert d["persona_via"] == "system_prompt" and "zara" in open(kw["system_prompt_file"]).read()
        assert kw["text"] == "hi"
    else:
        # A sessiond that would drop the file silently: the first message instead.
        assert d["persona_via"] == "prepend" and "system_prompt_file" not in kw
        assert kw["text"].startswith("# Persona\n\n# Persona Render: zara")


def test_ask_routed_takes_persona_for_a_fresh_chat_only(monkeypatch):
    seen = []
    monkeypatch.setattr(send, "ask", lambda text, bearer, **kw: seen.append(kw) or (True, {"session": "s"}))
    monkeypatch.setattr(auth_abs, "abs_identity", lambda b: ({"username": "d", "type": "root"}, 200))
    monkeypatch.setattr(sessions, "sessions_index", lambda: [])
    ok, _ = routing.ask_routed("hi", "tok", target="new", persona="zara", role="planner")
    assert ok and (seen[0]["persona"], seen[0]["role"]) == ("zara", "planner")
    routing.ask_routed("hi", "tok", target="new")
    assert "persona" not in seen[1] and "role" not in seen[1]


# Scope (persona_scope.py, docs/proposals/2026-10-17-persona-scope.md): a
# renderer with --json names each id's agents, projects and private
# material; the host's personas.toml and the user's persona-scope.json
# override it field by field.

@pytest.fixture
def described(repo, monkeypatch):
    monkeypatch.setenv("FAKE_JSON", "1")
    return repo


def test_an_older_renderer_without_json_still_lists(repo):
    rows = personas.described()
    assert [(d["id"], d["kind"], d["adapters"]) for d in rows] == [
        ("zara", "persona", None), ("sam", "persona", None),
        ("reviewer", "role", None), ("planner", "role", None)]


def test_json_listing_carries_the_front_matter_scope(described, monkeypatch):
    monkeypatch.setattr(auth_abs, "abs_identity", lambda b: ({"username": "d", "type": "root"}, 200))
    ok, d = presets.listing("tok")
    assert d["personas"] == ["zara", "sam"] and d["private"] == ["zara"]
    assert d["scope"]["zara"] == {"agents": ["pi", "claude"], "projects": None, "private": True}
    assert d["scope"]["sam"]["projects"] == ["agent-media"]
    assert d["trusted_models"] == persona_scope.DEFAULT_TRUSTED
    assert d["private_mode"] is personas.PRIVATE_MODE_READY


def test_local_then_user_layers_override_field_by_field(described, tmp_path, monkeypatch):
    (tmp_path / "personas.toml").write_text(
        'trusted_models = ["venice/*"]\n[persona.zara]\nagents = ["pi"]\nprojects = ["toys"]\n')
    got = persona_scope.resolve(personas.described())
    assert got["scope"]["zara"] == {"agents": ["pi"], "projects": ["toys"], "private": True}
    assert got["trusted_models"] == ["venice/*"]
    monkeypatch.setattr(auth_abs, "abs_identity", lambda b: ({"username": "d", "type": "root"}, 200))
    ok, d = persona_scope.put({"id": "zara", "agents": ["pi", "codex"], "trusted_models": ["venice/*", "ollama/*"]}, "tok")
    assert ok, d
    assert d["scope"]["zara"] == {"agents": ["pi", "codex"], "projects": ["toys"], "private": True}
    assert d["layers"]["local"]["persona"]["zara"]["agents"] == ["pi"]       # each layer is kept
    ok, d = persona_scope.put({"id": "zara", "agents": None}, "tok")          # cleared: back to local
    assert d["scope"]["zara"]["agents"] == ["pi"]
    assert not persona_scope.put({"id": "nobody", "agents": []}, "tok")[0]


def test_an_agent_the_scope_leaves_out_is_refused(described, asker):
    ok, d = send.ask("hi", "tok", agent="codex", persona="zara")
    assert not ok and d["status"] == 400 and "not offered for codex" in d["error"]
    ok, d = send.ask("hi", "tok", agent="claude", role="reviewer")
    assert ok, d


def test_presets_check_the_agent_too(described, monkeypatch):
    monkeypatch.setattr(auth_abs, "abs_identity", lambda b: ({"username": "d", "type": "root"}, 200))
    assert not presets.put({"name": "X", "agent": "codex", "persona": "zara"}, "tok")[0]
    assert presets.put({"name": "Y", "agent": "", "persona": "zara"}, "tok")[0]   # the host's default: not judged


def test_trusted_models_match_by_pattern():
    assert persona_scope.trusted("venice/z-ai-glm-5-3", ["venice/*"])
    assert not persona_scope.trusted("sonnet", ["venice/*"])
    assert not persona_scope.trusted("", ["*"])                # no model is the agent's own default


def test_private_mode_needs_the_switch_a_trusted_model_and_private_material(described, monkeypatch):
    rows = personas.described()
    monkeypatch.setattr(personas, "PRIVATE_MODE_READY", False)
    assert personas.mode_for("zara", "venice/x", rows, "pi") == "public"      # a host without it
    monkeypatch.setattr(personas, "PRIVATE_MODE_READY", True)
    assert personas.mode_for("zara", "venice/x", rows, "pi") == "private"
    assert personas.mode_for("zara", "sonnet", rows, "claude") == "public"
    assert personas.mode_for("sam", "venice/x", rows, "pi") == "public"       # nothing private to render


# Private threads (agent_media_core.private): a private render marks its
# session, every reader leaves a marked one alone, and the reaper seals it
# once it has ended. AGENT_PRIVATE_DIR is the conftest's tmp dir.

def test_a_private_render_marks_its_session(described, asker, monkeypatch):
    from agent_media_core import private

    monkeypatch.setattr(personas, "PRIVATE_MODE_READY", True)
    ok, d = send.ask("hi", "tok", agent="pi", persona="zara", model="venice/z-ai-glm-5-3")
    assert ok, d
    assert d["private"] is True and private.is_private(d["session"])
    assert repo_calls_mode(described) == "private"


def repo_calls_mode(repo):
    call = repo.calls()[-1]
    return call[call.index("--mode") + 1]


def test_an_untrusted_or_dropped_model_stays_public(described, asker, monkeypatch):
    from agent_media_core import private

    monkeypatch.setattr(personas, "PRIVATE_MODE_READY", True)
    # Claude drops a venice/ id and runs its own default: never trusted.
    ok, d = send.ask("hi", "tok", agent="claude", persona="zara", model="venice/x")
    assert ok, d
    assert repo_calls_mode(described) == "public" and "private" not in d
    assert not private.is_private(asker["session"])


def test_private_mode_needs_a_system_prompt_agent(described, monkeypatch):
    monkeypatch.setattr(personas, "PRIVATE_MODE_READY", True)
    rows = personas.described()
    assert personas.mode_for("zara", "venice/x", rows, "pi") == "private"
    assert personas.mode_for("zara", "venice/x", rows, "codex") == "public"   # would be its first message


def test_readers_leave_a_private_thread_alone(monkeypatch):
    from agent_media_core import catchup, private, session_feed

    from agent_media_server import reap, recaps, threads

    sid = str(uuid.uuid4())
    private.mark(sid, "/tmp")
    assert private.hidden(sid)
    monkeypatch.setattr(threads, "_conversation_text", lambda s, budget=8000: pytest.fail("read for a title"))
    assert threads.auto_title(sid) == ""
    monkeypatch.setattr(reap, "tail_text", lambda s, limit=6000: pytest.fail("read for a recap"))
    assert reap.generate_recap(sid) is None
    assert recaps.recap_for(sid) is None
    assert session_feed.turns(sid, store=object()) == []
    sent = {}
    from agent_media_core.intake import _summary

    monkeypatch.setattr(_summary, "_chat", lambda prompt, material, timeout, model=None: sent.setdefault("m", material) and None)
    catchup.summarise([{"kind": "reply", "thread": "t", "text": "the secret words", "private": True},
                       {"kind": "reply", "thread": "u", "text": "ordinary words", "private": False}], [])
    assert "the secret words" not in sent["m"] and "ordinary words" in sent["m"]


def test_the_reaper_seals_an_ended_private_thread_and_drops_its_render(repo, monkeypatch):
    from agent_media_core import private

    from agent_media_server import reap, seal

    ended, live, theirs = (str(uuid.uuid4()) for _ in range(3))
    for sid in (ended, live, theirs):
        private.mark(sid, "/tmp")
    (private.marker_dir() / theirs).write_text('{"cwd": "/tmp"}')     # agent-private's: its sweep's
    f = personas.write("decrypted persona")
    personas.name_for(f, ended)
    now = 1_000_000.0
    monkeypatch.setattr(reap, "last_message_at", lambda s: now - 3600)
    monkeypatch.setattr(seal, "_running", lambda s: s == live)
    sealed_ = []
    monkeypatch.setattr(seal, "seal", lambda s: sealed_.append(s))
    lines = reap.seal_private(now, apply=True)
    assert sealed_ == [ended], lines
    assert not f.exists() and not (f.parent / f"{ended}.md").exists()
    monkeypatch.setattr(reap, "last_message_at", lambda s: now - 3 * 3600)       # idle past two hours
    sealed_.clear()
    reap.seal_private(now, apply=True)
    assert set(sealed_) == {ended, live}
