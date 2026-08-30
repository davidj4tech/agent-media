"""Tests for media-setup's `server` role (rooms audio hub wiring)."""
import argparse
import os

from agent_media_core import setup


def test_merge_env_defaults_idempotent(tmp_path):
    p = tmp_path / "agent-media.env"
    added = setup._merge_env_defaults(p, [("A", "1"), ("B", "2")], dry_run=False)
    assert set(added) == {"A", "B"}
    assert "A=1" in p.read_text() and "B=2" in p.read_text()
    # second run: A already set is NOT overwritten; only the new key is added
    again = setup._merge_env_defaults(p, [("A", "9"), ("C", "3")], dry_run=False)
    assert again == ["C"]
    txt = p.read_text()
    assert "A=1" in txt and "A=9" not in txt and "C=3" in txt


def test_merge_env_defaults_ignores_comments(tmp_path):
    p = tmp_path / "agent-media.env"
    p.write_text("# A=not-really-set\n")
    assert setup._merge_env_defaults(p, [("A", "1")], dry_run=False) == ["A"]


def test_merge_env_defaults_dry_run_no_write(tmp_path):
    p = tmp_path / "agent-media.env"
    assert setup._merge_env_defaults(p, [("A", "1")], dry_run=True) == ["A"]
    assert not p.exists()


def test_server_refuses_non_systemd(monkeypatch):
    # Termux/runit hosts stay snapclients (and keep the openal AO default) —
    # the server role must not touch them.
    monkeypatch.setattr(setup, "_service_backend", lambda *a, **k: "runit")
    args = argparse.Namespace(backend="auto", music=False, now=False, dry_run=True)
    assert setup.cmd_server(args) == 1


def test_server_dry_run_systemd(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(setup, "_service_backend", lambda *a, **k: "systemd")
    monkeypatch.setattr(setup, "systemd_user_dir", lambda: tmp_path / "user")
    monkeypatch.setattr(setup, "_agent_media_env_path", lambda: tmp_path / "am.env")
    args = argparse.Namespace(backend="systemd", music=True, now=False, dry_run=True)
    assert setup.cmd_server(args) == 0
    out = capsys.readouterr().out
    assert "am-sinks.service" in out
    assert "am-snapfifo@.service" in out
    assert "MEDIA_SPEECH_AO=pulse" in out  # the PipeWire AO default
    assert "am-music" in out               # --music wired the second sink
    # dry-run writes nothing
    assert not (tmp_path / "user").exists()


# --- host roles -------------------------------------------------------------
# The regression these guard against is concrete: the phone ran both
# `call-guard` and `call-hold-consumer` for a fortnight, two processes pausing
# one speech socket, and barge-in failed intermittently the whole time.


def test_host_roles_unset_is_none_not_empty(tmp_path, monkeypatch):
    # None means "filter nothing", so a host that has never heard of roles
    # installs exactly what it installed before. An empty set would mean the
    # opposite -- every declaring service skipped -- and an upgrade that
    # silently stops installing services is worse than the bug being fixed.
    monkeypatch.delenv(setup.ROLES_ENV, raising=False)
    monkeypatch.setattr(setup.Path, "home", staticmethod(lambda: tmp_path))
    assert setup.host_roles() is None


def test_host_roles_from_env_and_file(tmp_path, monkeypatch):
    monkeypatch.setenv(setup.ROLES_ENV, "observe, render")
    assert setup.host_roles() == {"observe", "render"}
    monkeypatch.delenv(setup.ROLES_ENV)
    monkeypatch.setattr(setup.Path, "home", staticmethod(lambda: tmp_path))
    (tmp_path / ".config").mkdir()
    (tmp_path / ".config" / "agent-media-roles").write_text(
        "# this host\nrender\norigin  # the text comes from here\n")
    assert setup.host_roles() == {"render", "origin"}


def test_service_roles_read_from_repo():
    # Read from the real service dirs: the point is that these two files exist
    # and say what the installer needs, not that the parser works on fixtures.
    assert setup.service_roles("call-guard") == ({"observe"}, set())
    assert setup.service_roles("call-hold-consumer") == ({"render"}, {"observe"})


def test_undeclared_service_installs_everywhere():
    wanted, why = setup.service_wanted("sink-speech", {"render"})
    assert wanted and "no roles" in why


def test_phone_gets_the_guard_and_not_the_consumer():
    phone = {"observe", "render"}
    assert setup.service_wanted("call-guard", phone)[0] is True
    wanted, why = setup.service_wanted("call-hold-consumer", phone)
    # The bug in one assertion: the phone DOES render, so a requires-only rule
    # would install the consumer here. It is `conflicts: observe` that stops it.
    assert wanted is False
    assert "observe" in why


def test_house_host_gets_the_consumer_and_not_the_guard():
    red5 = {"render", "origin"}
    assert setup.service_wanted("call-hold-consumer", red5)[0] is True
    wanted, why = setup.service_wanted("call-guard", red5)
    assert wanted is False
    assert "observe" in why


def test_no_roles_declared_keeps_both(monkeypatch):
    # The pre-roles world, preserved exactly.
    assert setup.service_wanted("call-guard", None)[0] is True
    assert setup.service_wanted("call-hold-consumer", None)[0] is True


# --- first run ---------------------------------------------------------------


def test_init_writes_a_config_that_parses(tmp_path):
    from agent_media_core import config
    p = tmp_path / "config.toml"
    args = argparse.Namespace(config=str(p), roles="observe,render",
                              force=False, dry_run=False)
    assert setup.cmd_init(args) == 0
    # The output must be loadable by the thing that reads it, not merely
    # written: a starter config that does not parse is worse than none.
    assert config.host_roles(p) == {"observe", "render"}
    assert config.peers(p) == {}          # the peers block is commented out


def test_init_does_not_clobber_an_existing_config(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[host]\nroles = ["origin"]\n')
    args = argparse.Namespace(config=str(p), roles=None, force=False,
                              dry_run=False)
    assert setup.cmd_init(args) == 0
    assert 'roles = ["origin"]' in p.read_text()


def test_init_dry_run_writes_nothing(tmp_path):
    p = tmp_path / "config.toml"
    args = argparse.Namespace(config=str(p), roles=None, force=False,
                              dry_run=True)
    assert setup.cmd_init(args) == 0
    assert not p.exists()


def test_init_guesses_observe_only_on_termux(monkeypatch):
    monkeypatch.delenv("PREFIX", raising=False)
    assert setup._guess_roles() == ["render"]
    monkeypatch.setenv("PREFIX", "/data/data/com.termux/files/usr")
    assert setup._guess_roles() == ["observe", "render"]


def test_service_templates_are_found_inside_the_package_when_installed(tmp_path,
                                                                       monkeypatch):
    """A wheel force-includes the data dirs INTO the package; a checkout keeps
    them beside it. Installed-first, because that layout shipped broken: the
    built wheel carried no services/ at all, so install-services found no
    templates and reported success having installed nothing."""
    fake_pkg = tmp_path / "agent_media_core"
    (fake_pkg / "services" / "demo").mkdir(parents=True)
    monkeypatch.setattr(setup, "__file__", str(fake_pkg / "setup.py"))
    assert setup.service_templates_dir() == fake_pkg / "services"
    assert setup.service_template_names() == ["demo"]


# --- periodic services ------------------------------------------------------
# A template that carries a `timer` is a job, not a daemon. The distinction is
# not cosmetic: enabling a oneshot service directly runs it once at boot and
# never again, which looks like a working schedule for exactly one day.


def _template(root, name, *, timer=""):
    d = root / name
    d.mkdir(parents=True)
    (d / "run").write_text("#!/bin/sh\nexec true\n")
    if timer:
        (d / "timer").write_text(timer)
    return d


def test_a_template_with_a_timer_installs_a_oneshot_and_a_timer(tmp_path,
                                                                monkeypatch):
    templates = tmp_path / "services"
    _template(templates, "media-feed-gc", timer="OnCalendar=daily\n")
    monkeypatch.setattr(setup, "service_templates_dir", lambda: templates)
    root = tmp_path / "user"

    unit = setup._install_one_systemd("media-feed-gc", dry_run=False, root=root)

    # The timer is what gets enabled, so it is what the installer returns.
    assert unit == "agent-media-feed-gc.timer"
    svc = (root / "agent-media-feed-gc.service").read_text()
    assert "Type=oneshot" in svc
    assert "Restart=" not in svc          # a failed job waits for its window
    tmr = (root / "agent-media-feed-gc.timer").read_text()
    assert "OnCalendar=daily" in tmr
    assert "Persistent=true" in tmr       # a host that was off catches up
    assert "WantedBy=timers.target" in tmr


def test_a_template_without_a_timer_is_unchanged(tmp_path, monkeypatch):
    templates = tmp_path / "services"
    _template(templates, "media-feed")
    monkeypatch.setattr(setup, "service_templates_dir", lambda: templates)
    root = tmp_path / "user"

    unit = setup._install_one_systemd("media-feed", dry_run=False, root=root)

    assert unit == "agent-media-feed.service"
    svc = (root / "agent-media-feed.service").read_text()
    assert "Type=simple" in svc and "Restart=on-failure" in svc
    assert not (root / "agent-media-feed.timer").exists()


def test_reinstalling_a_periodic_service_rewrites_nothing(tmp_path, monkeypatch,
                                                          capsys):
    templates = tmp_path / "services"
    _template(templates, "media-feed-gc", timer="OnCalendar=daily\n")
    monkeypatch.setattr(setup, "service_templates_dir", lambda: templates)
    root = tmp_path / "user"
    setup._install_one_systemd("media-feed-gc", dry_run=False, root=root)
    capsys.readouterr()
    setup._install_one_systemd("media-feed-gc", dry_run=False, root=root)
    assert "wrote" not in capsys.readouterr().out


# --- optional integrations --------------------------------------------------
# Roles say what a host *is*, in three words. "Has an Audiobookshelf" is not
# that: it is something somebody configured, and a service that needs one is
# unwanted — not merely idle — everywhere it was never set up.


def test_a_service_needing_config_is_skipped_without_it(tmp_path, monkeypatch):
    templates = tmp_path / "services"
    d = templates / "abs-book-bridge"
    d.mkdir(parents=True)
    (d / "run").write_text("#!/bin/sh\nexec true\n")
    (d / "roles").write_text("requires: render\nrequires-config: abs-bridge.env\n")
    monkeypatch.setattr(setup, "service_templates_dir", lambda: templates)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(setup.Path, "home", lambda: tmp_path / "home")

    wanted, why = setup.service_wanted("abs-book-bridge", {"render"})
    assert wanted is False
    assert "abs-bridge.env" in why          # the reason names the fix

    cfg = tmp_path / "home" / ".config" / "agent-media"
    cfg.mkdir(parents=True)
    (cfg / "abs-bridge.env").write_text("ABS_URL=http://x\n")
    assert setup.service_wanted("abs-book-bridge", {"render"}) == (True, "roles match")


def test_the_config_gate_applies_to_a_host_with_no_roles_at_all(tmp_path,
                                                                monkeypatch):
    """Undeclared roles mean "install everything as before" — but a fresh host
    is the one most likely to have no config, and least wanting a daemon that
    logs "not configured" forever."""
    templates = tmp_path / "services"
    d = templates / "abs-cast-watcher"
    d.mkdir(parents=True)
    (d / "run").write_text("#!/bin/sh\nexec true\n")
    (d / "roles").write_text("requires-config: abs-bridge.env\n")
    monkeypatch.setattr(setup, "service_templates_dir", lambda: templates)
    monkeypatch.setattr(setup.Path, "home", lambda: tmp_path / "home")

    assert setup.service_wanted("abs-cast-watcher", None)[0] is False


def test_a_service_without_the_gate_is_unaffected(tmp_path, monkeypatch):
    templates = tmp_path / "services"
    d = templates / "sink-speech"
    d.mkdir(parents=True)
    (d / "run").write_text("#!/bin/sh\nexec true\n")
    (d / "roles").write_text("requires: render\n")
    monkeypatch.setattr(setup, "service_templates_dir", lambda: templates)
    monkeypatch.setattr(setup.Path, "home", lambda: tmp_path / "home")

    assert setup.service_config_gate("sink-speech") == ""
    assert setup.service_wanted("sink-speech", {"render"})[0] is True


# --- shipped shell helpers --------------------------------------------------


def test_the_audiobook_fetch_helper_ships_with_the_package():
    """`library.fetch_cmd` looks for this on PATH, and the console script only
    puts it there if the file is actually in the install."""
    script = setup.shipped_bin("audiobook-fetch")
    assert script.is_file()
    assert script.read_text().startswith("#!")
    assert os.access(script, os.X_OK)


def test_shipped_bin_follows_the_package_when_it_moves(tmp_path, monkeypatch):
    inside = tmp_path / "agent_media_core" / "bin"
    inside.mkdir(parents=True)
    (inside / "audiobook-fetch").write_text("#!/bin/sh\n")
    monkeypatch.setattr(setup, "_data_dir",
                        lambda name: tmp_path / "agent_media_core" / name)
    assert setup.shipped_bin("audiobook-fetch").parent == inside
