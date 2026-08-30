"""Collect the service/tmux templates into the wheel, file by file.

`services/` and `tmux/` sit beside the package in the repo and have to be
shipped *inside* it, or a non-editable install has no templates and
`media-setup install-services` silently installs nothing. That used to be a
static `[tool.hatch.build.targets.wheel.force-include]` of the two directories.

It cannot stay static, because a host running the services keeps live state in
the same tree: runit is given each service directory whole (p8a symlinks them
into its supervision tree), so it writes `services/<name>/supervise/` in place —
and two of the files it puts there, `control` and `ok`, are **named pipes**.

A build that walks into one blocks forever on the open(). On p8a a
`uv pip install -e` sat inside `hatchling.build` for twenty minutes at zero CPU
with no output, which reads exactly like a slow phone and is not. The practical
effect was that the phone could not rebuild its own package while its own
services were running — discovered while installing a new console script, which
is the routine reason to rebuild.

`exclude` does not help: force-include entries are not filtered by it (verified
against this very tree, FIFO and all). So the directories are walked here
instead, and anything that is not a regular file is left behind. Nothing under
`supervise/` belongs in a wheel anyway — it is one host's live state, not a
template.
"""

from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

# (source directory in the repo, destination inside the installed package)
TEMPLATE_DIRS = (
    ("services", "agent_media_core/services"),
    ("tmux", "agent_media_core/tmux"),
    # Shell helpers with a console script in front of them. `bin/` is here for
    # the same reason as the other two: a wheel that does not carry it leaves
    # an entrypoint pointing at a file that does not exist.
    ("bin", "agent_media_core/bin"),
)

# Directory names never worth shipping: runit's live state, and the log
# directories it creates beside it.
SKIP_DIRS = {"supervise", "__pycache__"}


class TemplateHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version, build_data):
        root = Path(self.root)
        include = build_data.setdefault("force_include", {})
        for src_name, dest_name in TEMPLATE_DIRS:
            src_root = root / src_name
            if not src_root.is_dir():
                continue
            for path in self._files(src_root):
                rel = path.relative_to(src_root)
                include[str(path)] = f"{dest_name}/{rel.as_posix()}"

    def _files(self, root: Path):
        """Every regular file under `root`, skipping live-state directories.

        `is_file()` follows symlinks and is False for a FIFO, a socket or a
        device — which is the whole point. A stat that raises (a dangling
        symlink, a permission wall) drops the entry rather than the build.
        """
        for path in sorted(root.rglob("*")):
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            try:
                if path.is_file():
                    yield path
            except OSError:
                continue
