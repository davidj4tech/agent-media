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


def test_the_default_fetch_host_is_still_the_phone(monkeypatch):
    # The default must not change: on the hub this module is still talking to
    # p8a over ssh, and that is the path that carries every fetch today.
    monkeypatch.delenv("MEDIA_MUSIC_LOCAL_SSH", raising=False)
    assert music_local.ssh_host() == "p8a"
