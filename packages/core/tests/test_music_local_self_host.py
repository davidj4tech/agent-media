"""sink-music-local: the fetch host might be this machine.

Every command in that module went over ssh unconditionally, which was right
while only the hub called it — red5 asks the phone, the phone plays. The share
listener runs `media music play --where phone` **on the phone**, so the phone
ssh'd to itself and the first real share out of the Android share sheet died on
`Host key verification failed`.
"""

import socket

import pytest

from agent_media_core.sinks import music_local


@pytest.mark.parametrize("host", [
    "127.0.0.1", "::1", "localhost",
    "",          # unset: nothing to ssh to
    "   ",
])
def test_loopback_spellings_are_this_machine(host):
    assert music_local.is_self(host)


def test_this_hosts_own_name_is_this_machine():
    assert music_local.is_self(socket.gethostname())
    # ...including the fully-qualified tailnet form, matched on the short name.
    assert music_local.is_self(socket.gethostname().split(".")[0] + ".example.ts.net")


def test_another_host_is_not():
    assert not music_local.is_self("some-other-box")
    assert not music_local.is_self("some-other-box.example.ts.net")


def test_a_remote_host_still_goes_over_ssh():
    argv = music_local.host_argv("some-other-box", "echo hi")
    assert argv[0] == "ssh"
    assert argv[-2:] == ["some-other-box", "echo hi"]
    assert "BatchMode=yes" in argv  # the ControlMaster options are still there


def test_this_machine_runs_a_shell_instead(monkeypatch):
    argv = music_local.host_argv(socket.gethostname(), "echo hi")
    assert argv == ["sh", "-c", "echo hi"]
    # The command is the same string either way, so the two forms cannot drift.
    assert argv[-1] == music_local.host_argv("elsewhere", "echo hi")[-1]


def test_the_local_form_actually_runs(tmp_path):
    # Cheap end-to-end: the argv we build is runnable, which is the property
    # the ssh form had for free and this one has to earn.
    import subprocess

    out = subprocess.run(music_local.host_argv("127.0.0.1", "echo hello"),
                         capture_output=True, text=True, timeout=10)
    assert out.returncode == 0 and out.stdout.strip() == "hello"


# ---- the question the phone lane actually asks ---------------------------
#
# is_self() is not enough there: Android gives every Termux install the
# hostname `localhost`, so p8a does not know it is called p8a and no name
# comparison can tell it. The endpoint's shape can.

def test_a_unix_socket_endpoint_means_we_are_the_phone(monkeypatch):
    monkeypatch.setenv("MEDIA_MUSIC_LOCAL_SSH", "p8a")
    monkeypatch.setenv("MEDIA_MUSIC_LOCAL_ENDPOINT",
                       "/data/data/com.termux/files/home/.local/state/"
                       "agent-media/mpv-music.sock")
    assert music_local.fetch_is_local()
    assert music_local.phone_argv("echo hi") == [
        "sh", "-c", 'cd "$HOME" && echo hi']


def test_a_tcp_endpoint_means_the_phone_is_elsewhere(monkeypatch):
    # What the hub sees: the same mpv, bridged over Tailscale.
    monkeypatch.setenv("MEDIA_MUSIC_LOCAL_SSH", "p8a")
    monkeypatch.setenv("MEDIA_MUSIC_LOCAL_ENDPOINT", "tcp://100.94.14.59:6601")
    assert not music_local.fetch_is_local()
    argv = music_local.phone_argv("echo hi")
    assert argv[0] == "ssh" and argv[-2:] == ["p8a", "echo hi"]


def test_no_endpoint_is_not_local(monkeypatch):
    # The backend is unconfigured; nothing should be assumed about where it is.
    monkeypatch.setenv("MEDIA_MUSIC_LOCAL_SSH", "p8a")
    monkeypatch.delenv("MEDIA_MUSIC_LOCAL_ENDPOINT", raising=False)
    assert not music_local.fetch_is_local()


def test_a_hostname_match_still_wins_when_it_can(monkeypatch):
    # An ordinary host that IS the fetcher and names itself properly does not
    # need the endpoint hint.
    monkeypatch.setenv("MEDIA_MUSIC_LOCAL_SSH", socket.gethostname())
    monkeypatch.setenv("MEDIA_MUSIC_LOCAL_ENDPOINT", "tcp://100.94.14.59:6601")
    assert music_local.fetch_is_local()


def test_the_phone_hostname_trap_is_covered(monkeypatch):
    # The exact shape of the bug: hostname says localhost, ssh host says p8a,
    # endpoint is a unix socket. Before the endpoint rule this asked ssh to
    # connect p8a -> p8a and died on host key verification.
    monkeypatch.setattr(socket, "gethostname", lambda: "localhost")
    monkeypatch.setenv("MEDIA_MUSIC_LOCAL_SSH", "p8a")
    monkeypatch.setenv("MEDIA_MUSIC_LOCAL_ENDPOINT",
                       "/data/data/com.termux/files/home/.local/state/"
                       "agent-media/mpv-music.sock")
    assert not music_local.is_self("p8a"), "the name comparison cannot see it"
    assert music_local.fetch_is_local(), "but the endpoint can"


def test_the_default_fetch_host_is_still_the_phone(monkeypatch):
    # The default must not change: on the hub this module is still talking to
    # p8a over ssh, and that is the path that carries every fetch today.
    monkeypatch.delenv("MEDIA_MUSIC_LOCAL_SSH", raising=False)
    assert music_local.ssh_host() == "p8a"


def test_the_local_form_starts_from_home(monkeypatch, tmp_path):
    """`ssh host cmd` runs in the login directory and the commands here are
    written for that: the fetch helper defaults to the *relative*
    `bin/play-local`. Run from the caller's cwd instead and it is not found —
    which is exactly how the share listener failed on a phone that has it."""
    import os
    import subprocess

    monkeypatch.setenv("MEDIA_MUSIC_LOCAL_SSH", "127.0.0.1")
    home = tmp_path / "home"
    (home / "bin").mkdir(parents=True)
    helper = home / "bin" / "play-local"
    helper.write_text("#!/bin/sh\necho fetched \"$1\"\n")
    helper.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    out = subprocess.run(music_local.phone_argv("bin/play-local a-uri"),
                         cwd=elsewhere, capture_output=True, text=True,
                         timeout=10, env={**os.environ, "HOME": str(home)})
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "fetched a-uri"
