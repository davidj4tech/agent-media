"""`sasonica <command> [args…]` — every command the server's packages have.

    sasonica serve        the canvas, which serves the app's API (media-visual-canvas)
    sasonica sessiond     the session holder for headless chats (media sessiond)
    sasonica tunnel       the quick tunnel the app reaches it through (media-tunnel run)
    sasonica pair         a pairing link for the app (media-visual-canvas pair), as
                          the app's pairing screen says: `sasonica pair --device NAME`
    sasonica install      shims on PATH, the two services, this host's config
    sasonica update       the newest release in place of this binary
    sasonica commands     every command it can run
    sasonica version
    sasonica <script> …   any console script of agent-media's packages or
                          edge-tts, by its own name: `sasonica media say hi`

A command is found by its console-script name in the installed packages'
metadata, so a script added to a pyproject.toml is a `sasonica` command in the
next build with nothing to add here.
"""

from __future__ import annotations

import os
import sys
from importlib import metadata
from pathlib import Path

#: Distributions whose console scripts are commands. Ours, and edge-tts: the
#: default render engine runs `edge-tts` as a program (render/engines.py).
_DISTS = ("agent-media-", "sasonica", "edge-tts")

#: Short names for what a service runs: (console script, arguments before
#: the caller's).
ALIASES = {
    "serve": ("media-visual-canvas", []),
    "sessiond": ("media", ["sessiond"]),
    "tunnel": ("media-tunnel", ["run"]),
    # The app's pairing screen and messages name `sasonica pair --device NAME`.
    "pair": ("media-visual-canvas", ["pair"]),
}


def _ours(dist_name: str) -> bool:
    name = (dist_name or "").lower().replace("_", "-")
    return any(name == d or (d.endswith("-") and name.startswith(d)) for d in _DISTS)


def commands() -> dict[str, metadata.EntryPoint]:
    """Console-script name → entry point, for our distributions only."""
    found: dict[str, metadata.EntryPoint] = {}
    for dist in metadata.distributions():
        if not _ours(dist.metadata["Name"]):
            continue
        for ep in dist.entry_points:
            if ep.group == "console_scripts" and ep.name != "sasonica":
                found.setdefault(ep.name, ep)
    return found


def build_version() -> str:
    """The binary's version: the BUILD file the build writes beside this
    module, else the package's own (a checkout)."""
    try:
        return (Path(__file__).with_name("BUILD")).read_text().strip()
    except OSError:
        return metadata.version("sasonica")


def binary_path() -> str | None:
    """The launcher this runs under. PyApp puts its path in $PYAPP (built with
    PYAPP_PASS_LOCATION); "1" or unset means a plain Python install."""
    p = os.environ.get("PYAPP", "")
    return p if p not in ("", "1") and os.path.isabs(p) else None


def run_script(name: str, args: list[str]) -> int:
    ep = commands().get(name)
    if ep is None:
        print(f"sasonica: no command '{name}' (sasonica commands lists them)", file=sys.stderr)
        return 2
    # The script sees itself as called by its own name, as from its shim.
    sys.argv = [name, *args]
    rc = ep.load()()
    return rc if isinstance(rc, int) else (0 if rc is None else 1)


def _utf8_on_windows(args: list[str]) -> int | None:
    """Windows reads and writes text in its code page (cp1252) unless Python
    is in UTF-8 mode, and everything here is UTF-8. PyApp starts Python
    isolated (-I, so PYTHONUTF8 is ignored); run again with `-X utf8`. None
    when nothing needs doing."""
    if os.name != "nt" or sys.flags.utf8_mode or os.environ.get("SASONICA_NO_REEXEC"):
        return None
    import subprocess

    return subprocess.call([sys.executable, "-X", "utf8", "-I", "-m", "sasonica", *args])


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    if argv is None:
        rc = _utf8_on_windows(args)
        if rc is not None:
            return rc
    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0
    cmd, rest = args[0], args[1:]
    if cmd in ("version", "--version"):
        print(f"sasonica {build_version()}")
        return 0
    if cmd == "commands":
        for name in sorted([*ALIASES, *commands()]):
            print(name)
        return 0
    if cmd == "install":
        from sasonica import install
        return install.main(rest)
    if cmd == "update":
        from sasonica import update
        return update.main(rest)
    if cmd in ALIASES:
        script, pre = ALIASES[cmd]
        return run_script(script, [*pre, *rest])
    return run_script(cmd, rest)


if __name__ == "__main__":
    sys.exit(main())
