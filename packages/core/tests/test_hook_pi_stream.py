"""media-hook-pi-stream: the session id files a streamed reply under its thread."""

import io
import os

from agent_media_core.intake import hook_pi_stream


class _Stdin(io.BytesIO):
    def fileno(self):
        return self._fd


def _run(monkeypatch, argv, text):
    r, w = os.pipe()
    os.write(w, text.encode())
    os.close(w)
    stdin = _Stdin()
    stdin._fd = r
    monkeypatch.setattr(hook_pi_stream.sys, "stdin", stdin)
    monkeypatch.setattr(hook_pi_stream, "load_env_file", lambda label: None)
    seen = {}

    def fake_submit(sentences, event):
        seen["sentences"] = list(sentences)
        seen["metadata"] = event.metadata

    monkeypatch.setattr(hook_pi_stream, "submit_stream", fake_submit)
    try:
        assert hook_pi_stream.main(argv) == 0
    finally:
        os.close(r)
    return seen


def test_session_goes_into_metadata(monkeypatch):
    seen = _run(monkeypatch, ["--session", "abc-123"], "Hello there. Bye now.")
    assert seen["metadata"] == {"kind": "stream", "session": "abc-123"}
    assert seen["sentences"]


def test_no_session_still_streams(monkeypatch):
    seen = _run(monkeypatch, [], "Hello there.")
    assert seen["metadata"] == {"kind": "stream"}
