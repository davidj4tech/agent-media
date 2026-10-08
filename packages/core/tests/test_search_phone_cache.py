"""`search(channel="book")` lists the phone's cached books through the phone's
Termux worker, not `ssh p8a find` (roadmap item 15, #6)."""

import io
import json
import subprocess
import sys

from agent_media_core import mcp_server


def _run_search(monkeypatch, tmp_path, jobs: str):
    for k in ("MEDIA_BOOK_DEFAULT_TARGET", "MEDIA_SPEECH_CLIP_SSH_SASONICA",
              "MEDIA_BOOK_CACHE_SSH_SASONICA", "MEDIA_BOOK_CACHE_DIRS_SASONICA"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("MEDIA_BOOK_LOCAL_DIRS", str(tmp_path / "none"))
    monkeypatch.setenv("MEDIA_SPEECH_DEFAULT_TARGET", "sasonica")
    monkeypatch.setenv("MEDIA_MUSIC_LOCAL_SSH", "p8a")
    monkeypatch.setenv("MEDIA_PHONE_JOBS", jobs)
    monkeypatch.setattr(mcp_server, "_abs_config", lambda: ("http://abs.invalid", "t"))
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda *a, **k: io.BytesIO(json.dumps({"libraries": []}).encode()))
    seen = []

    def fake_run(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "/phone/books/Dune.m4b\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    fn = getattr(mcp_server.search, "fn", mcp_server.search)
    return fn("book", "dune"), seen


def test_the_phone_cache_is_asked_of_its_worker(monkeypatch, tmp_path):
    got, seen = _run_search(monkeypatch, tmp_path, "1")
    assert seen and seen[0][:3] == [sys.executable, "-m", "agent_media_core.phone_run"]
    assert "find" in seen[0][3]
    assert got["results"] == [{"uri": "/phone/books/Dune.m4b", "title": "Dune  [sasonica-cache]"}]


def test_without_the_worker_it_is_ssh_as_before(monkeypatch, tmp_path):
    _, seen = _run_search(monkeypatch, tmp_path, "0")
    assert seen[0][0] == "ssh" and "p8a" in seen[0]
