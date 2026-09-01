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


# --- when the pull itself cannot happen -------------------------------------
# The phone spent an afternoon two commits behind on a wifi that accepted the
# TCP connection to github.com and then dropped the TLS handshake. git waits;
# the ssh call burns its whole budget; the host reads as "unreachable" when it
# is sitting right there answering ssh. Only its git remote is out of reach.


def test_the_remote_pull_is_bounded():
    assert "timeout 90 git" in cli._REMOTE_FIX
    # And still works where `timeout` is missing, rather than skipping the pull.
    assert "command -v timeout" in cli._REMOTE_FIX


def test_a_hung_pull_is_named_for_what_it_is():
    assert "no route to its git remote" in cli._REMOTE_FIX
    assert "124" in cli._REMOTE_FIX


def test_the_remote_script_is_valid_sh():
    """It runs under whatever /bin/sh the far host has, which on the phone is
    not bash."""
    import subprocess
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".sh") as fh:
        fh.write(cli._remote_fix())
        fh.flush()
        assert subprocess.run(["sh", "-n", fh.name]).returncode == 0


def test_a_host_that_could_not_be_fixed_stops_being_merely_behind(monkeypatch,
                                                                  tmp_path):
    """Otherwise the next run tries the same doomed pull, hourly, forever —
    and the ledger goes on calling it stale, which reads as "nobody has
    deployed yet" rather than "this needs a person"."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("MEDIA_DOCTOR_HOSTS", "p8a")
    monkeypatch.setattr(cli, "_local_repo_state", lambda repos, echo=True: ({}, {}))
    monkeypatch.setattr(cli, "_scan_local", lambda: False)
    monkeypatch.setattr(cli, "_scan_hosts",
                        lambda hosts, h, b: (["p8a"], [], []))
    monkeypatch.setattr(cli, "_fix_host",
                        lambda host: (False, ["fix=agent-media FAILED: "
                                              "no route to its git remote"]))
    import argparse
    cli.cmd_doctor(argparse.Namespace(fix=True, json=False, quiet=False))

    ledger = (tmp_path / "agent-media" / "version-skew.log").read_text()
    assert "p8a!" in ledger, ledger
