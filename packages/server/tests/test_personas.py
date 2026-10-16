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

from agent_media_server import auth_abs, driver, personas, presets, routing, send, sessions

FAKE_RENDER = r'''#!{python}
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_RENDER_LOG"], "a") as fh:
    fh.write(json.dumps(args) + "\n")
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
    monkeypatch.setattr(personas, "listing", lambda: {"personas": ["broken"], "roles": []})
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
