"""What `media doctor --fix` is allowed to touch.

The fix is a live deploy onto machines that render speech, so the interesting
tests are all about restraint: which hosts qualify, and what the remote script
refuses to do.
"""
from agent_media_core import cli


def test_only_stale_hosts_are_deployed():
    assert cli.fix_targets(["p8a", "pn"], []) == ["p8a", "pn"]


def test_a_broken_host_is_never_pulled():
    """`red5!` in the ledger means a dead install, services down or a crash
    loop. A pull fixes none of those, and restarting a crash-looping service
    only spins it faster while the output claims work was done."""
    assert cli.fix_targets(["p8a", "red5"], ["red5"]) == ["p8a"]


def test_local_is_not_an_ssh_target():
    """`local` reaches the unhealthy list by a different route (selfcheck, not
    a probe) and is not a host name — ssh'ing to it would hang or worse."""
    assert cli.fix_targets(["local"], []) == []
    assert cli.fix_targets([], ["local"]) == []


def test_remote_fix_refuses_a_dirty_checkout():
    """An unattended stash/reset on a host David may be mid-edit on is a worse
    outcome than a warning that stays up."""
    assert "status --porcelain" in cli._REMOTE_FIX
    assert "skipped: uncommitted changes" in cli._REMOTE_FIX
    assert "--ff-only" in cli._REMOTE_FIX, "a fix must never rewrite history"
    for forbidden in ("reset --hard", "git stash", "checkout -f", "pull -f"):
        assert forbidden not in cli._REMOTE_FIX


def test_remote_fix_restarts_after_pulling():
    """A host that pulled but didn't restart reads as fixed to the next doctor
    run (HEAD matches) while its services still execute the old code — the
    silent-wrong-code state the ledger exists to catch."""
    assert cli._REMOTE_FIX.index("git -C") < cli._REMOTE_FIX.index("restart-services")
