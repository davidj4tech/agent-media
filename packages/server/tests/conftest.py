"""Test isolation for the server package.

The same isolation `packages/visual/tests/conftest.py` gives the canvas, and
for the same reasons. core's cli.py calls load_env_file at module import, so a
combined run pulls the machine's real ~/.config/agent-media.env into
os.environ during collection; scrub every MEDIA_* var so these tests always
see package defaults, and set what a test needs with monkeypatch.setenv.

And point the state store at a throwaway dir. The routes here read and write
the speech history, drafts and the book-tracks shelf; a test module that
skipped this would write into David's real ones.
"""

import os

import pytest


@pytest.fixture(autouse=True)
def _clean_media_env(monkeypatch, tmp_path):
    for k in list(os.environ):
        if k.startswith("MEDIA_"):
            monkeypatch.delenv(k, raising=False)
    # The layout (agent_media_core/layout.py) is pinned to David's, which is
    # what these tests were written against: detection reads the real HOME
    # (~/.amux, ~/.claude/settings.json), so unpinned they would pass on red5
    # and fail anywhere else. test_layout.py unpins it where it needs to.
    monkeypatch.setenv("MEDIA_LAYOUT", "projects-per-tmux-session")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    # The alert store files TODOs in ~/org/inbox.org: never David's here.
    monkeypatch.setenv("MEDIA_ALERTS_INBOX", "0")
    # Paired devices live under that state dir too (devices.json, the pairing
    # codes); the in-process bits — the parsed-file cache and the per-address
    # pairing-failure counts — are reset so one test's refusals cannot
    # rate-limit the next.
    from agent_media_server import devices

    devices._reset_for_tests()
    # Recaps are read out of Claude Code's transcripts: point that at an empty
    # throwaway dir, so a test never reads David's real ones, and forget what
    # the last test's files held (the cache is keyed by path and inode).
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    # The other harnesses' stores are read the same way (the thread list is
    # every harness's conversations, not only Claude's), so point those at
    # throwaway dirs too — unset, a test would list this machine's real
    # Codex, pi, Hermes and opencode conversations.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    from agent_media_server import recaps

    recaps._reset_for_tests()
    # The same for the messages read out of those transcripts, and no thread
    # stream's watcher left running from another test.
    from agent_media_server import thread_events, transcript

    transcript._reset_for_tests()
    thread_events._reset_for_tests()
    from agent_media_server import session_events

    session_events._reset_for_tests()
    # Memory for /sessions/state is read from /proc: an empty fake root, so
    # a test sees "unknown" unless it builds a process tree of its own, and
    # no pid left over from a real sweep in another test.
    from agent_media_server import procmem, sessions

    (tmp_path / "fake-proc").mkdir(exist_ok=True)
    monkeypatch.setattr(procmem, "PROC", tmp_path / "fake-proc")
    monkeypatch.setattr(sessions, "_PIDS", {})
    # A session's directory is kept once found; the subagents' scans too.
    monkeypatch.setattr(sessions, "_CWDS", {})
    # Titles read out of a transcript are cached by (size, mtime); a tmp_path
    # file can land on the same pair as the last test's.
    monkeypatch.setattr(sessions, "_HEADLESS_TITLES", {})
    monkeypatch.setattr(sessions, "_STORED_TITLES", {})
    monkeypatch.setattr(sessions, "_FIRST_PROMPT", {})
    from agent_media_server import agents

    agents._reset_for_tests()
    # The search index lives under the state dir above; forget the last
    # test's connection and its short-lived caches.
    from agent_media_server import search

    search._reset_for_tests()
