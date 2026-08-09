"""A staging report must not outlive the transfer it describes.

`book-stage.json` is written by the copy helper and never cleared on success,
so an old failure stayed in the popup's status line indefinitely. Observed: a
44-minute-old error about a *different* document sitting where the clock
should be, while the document actually playing was fine. An indicator that
outlives its subject is worse than none — it reports the current item as
broken when nothing is wrong with it.
"""

import json
import time

import pytest

from agent_media_core import cli


@pytest.fixture
def stage(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    d = tmp_path / "agent-media"
    d.mkdir(parents=True, exist_ok=True)

    def write(status, age_s, **kw):
        payload = {"status": status, "uri": "/x.mp3", "target": "phone",
                   "title": "t", "total": 100, "copied": 0,
                   "ts": time.time() - age_s, **kw}
        (d / "book-stage.json").write_text(json.dumps(payload))
    return write


def test_a_fresh_failure_is_reported(stage):
    stage("error", 5)
    assert cli._book_stage_status(30) == "! copy failed"


def test_a_stale_failure_is_not(stage):
    stage("error", 44 * 60)
    assert cli._book_stage_status(30) is None, (
        "a 44-minute-old copy error sat over the clock of a document that "
        "was playing perfectly")


def test_a_stale_copy_in_progress_is_not_reported_either(stage):
    """A 'copying' that stopped being true is the same lie."""
    stage("copying", 44 * 60, copied=10)
    assert cli._book_stage_status(30) is None


def test_fresh_progress_still_renders(stage):
    stage("copying", 2, copied=50)
    out = cli._book_stage_status(30, bar=False)
    assert out and "%" in out


def test_the_window_is_configurable(stage, monkeypatch):
    stage("error", 120)
    assert cli._book_stage_status(30) is None
    monkeypatch.setenv("MEDIA_BOOK_STAGE_STALE_S", "600")
    assert cli._book_stage_status(30) == "! copy failed"


def test_zero_disables_the_staleness_check(stage, monkeypatch):
    monkeypatch.setenv("MEDIA_BOOK_STAGE_STALE_S", "0")
    stage("error", 44 * 60)
    assert cli._book_stage_status(30) == "! copy failed"
