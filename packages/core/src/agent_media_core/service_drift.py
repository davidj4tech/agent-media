"""Service files that are copies of this checkout's, so a `git pull` never reaches them.

p8a's `phone-jobs` ran copies of `run` and `phone-jobs.py` made on 3 Oct: every
pull since updated the checkout and nothing the service ran (found 18 Oct
2026). A service installed by copying looks exactly like one installed by
linking until the two drift, and then the host runs old code while doctor
calls its checkout current.

`scan()` looks where services are installed and compares each regular file
with the checkout file it is a copy of:

* **runit** (Termux's `$PREFIX/var/service`, `/etc/service`): a service dir
  named like a template here (`packages/*/services/<name>/`,
  `deploy/*/service/<name>/`), and each file the template ships (`run`,
  `log/run`, `finish`, a worker script) that is a regular file there rather
  than a link into this checkout.
* **systemd --user** (`$XDG_CONFIG_HOME/systemd/user`): a unit file named like
  one here (`packages/*/systemd/*`, `packages/*/deploy/*.service`,
  `deploy/systemd/*`) that is a regular file. A unit `media-setup
  install-services` generated (its `ExecStart=/bin/sh` runs a `run` in this
  checkout) is not a copy: the code it runs is the checkout's.
* **Windows** has neither: its tasks (Task Scheduler, `sasonica install`)
  name a program and hold no copy of a file, so there is nothing to scan.

Each finding is a `Drift`: `same` when the copy still matches (reported as
"copy, not linked", not a problem yet), else it differs (a problem: the host
is running something no pull will change). `MEDIA_DRIFT_OK` (space or comma
separated: a service dir, a unit file name, or `<service>/<file>`) leaves a
deliberate local copy out. `relink()` is the fix (`media-setup
link-services`).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

#: Never compared: runit's live state, and caches.
SKIP_PARTS = {"supervise", "__pycache__"}
UNIT_SUFFIXES = (".service", ".timer", ".socket", ".path")


@dataclass
class Drift:
    kind: str      # runit | systemd
    label: str     # "<service>/<file>" or "<unit>"
    path: Path     # the installed file
    source: Path   # the checkout file it is a copy of
    same: bool     # identical bytes


def _files(d: Path) -> Iterable[Path]:
    """Regular files under a template dir, skipping runit state and caches."""
    for p in sorted(d.rglob("*")):
        rel = p.relative_to(d)
        if SKIP_PARTS & set(rel.parts):
            continue
        if p.is_file() and not p.is_symlink():
            yield p


def runit_templates(root: Path) -> "dict[str, Path]":
    """Service name → its template dir in the checkout (first one wins)."""
    out: dict[str, Path] = {}
    pats = ("packages/*/services/*", "deploy/*/service/*", "deploy/*/services/*")
    for pat in pats:
        for d in sorted(root.glob(pat)):
            if d.is_dir() and not d.name.startswith("_") and (d / "run").is_file():
                out.setdefault(d.name, d)
    return out


def unit_templates(root: Path) -> "dict[str, Path]":
    """Unit file name → its file in the checkout (first one wins)."""
    out: dict[str, Path] = {}
    for pat in ("packages/*/systemd/*", "packages/*/deploy/*", "deploy/systemd/*",
                "deploy/*/systemd/*"):
        for f in sorted(root.glob(pat)):
            if f.is_file() and f.name.endswith(UNIT_SUFFIXES):
                out.setdefault(f.name, f)
    return out


def _ignored() -> "set[str]":
    raw = os.environ.get("MEDIA_DRIFT_OK", "")
    return {w for w in raw.replace(",", " ").split() if w}


def _linked(path: Path, root: Path) -> bool:
    """`path` is, or resolves to, a file inside the checkout."""
    try:
        real = path.resolve()
    except OSError:
        return False
    return root == real or root in real.parents


def _same(a: Path, b: Path) -> bool:
    try:
        return a.read_bytes() == b.read_bytes()
    except OSError:
        return False


def scan_runit(root: Path, svdir: "Optional[Path]") -> "list[Drift]":
    if svdir is None or not svdir.is_dir():
        return []
    templates = runit_templates(root)
    skip = _ignored()
    out = []
    for entry in sorted(svdir.iterdir()):
        tpl = templates.get(entry.name)
        if tpl is None or entry.name in skip or not entry.is_dir():
            continue
        if _linked(entry, root):
            continue                       # the whole dir links in: nothing to drift
        for src in _files(tpl):
            rel = src.relative_to(tpl)
            live = entry / rel
            label = f"{entry.name}/{rel.as_posix()}"
            if label in skip or not live.is_file() or _linked(live, root):
                continue
            out.append(Drift("runit", label, live, src, _same(live, src)))
    return out


def _generated(text: str, root: Path) -> bool:
    """A unit media-setup wrote: its ExecStart runs a `run` in this checkout."""
    for line in text.splitlines():
        if line.strip().startswith("ExecStart=") and str(root) in line \
                and line.rstrip().endswith("/run"):
            return True
    return False


def scan_systemd(root: Path, unit_dir: "Optional[Path]") -> "list[Drift]":
    if unit_dir is None or not unit_dir.is_dir():
        return []
    templates = unit_templates(root)
    skip = _ignored()
    out = []
    for name, src in sorted(templates.items()):
        live = unit_dir / name
        if name in skip or not live.is_file() or live.is_symlink() or _linked(live, root):
            continue
        try:
            text = live.read_text(errors="replace")
        except OSError:
            continue
        if _generated(text, root):
            continue
        out.append(Drift("systemd", name, live, src, _same(live, src)))
    return out


def unit_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "systemd" / "user"


def scan(root: "Optional[Path]" = None, svdir: "Optional[Path]" = None,
         units: "Optional[Path]" = None) -> "list[Drift]":
    """Every copy of a checkout file installed as a service file on this host.
    Defaults: this checkout, the runit dir in use, the systemd user dir."""
    from . import cli

    root = root or cli._checkout_root()
    if root is None:
        return []
    if svdir is None:
        svdir = cli._sv_dir()
    return scan_runit(root, svdir) + scan_systemd(root, units or unit_dir())


def facts(found: "Optional[list[Drift]]" = None) -> "dict[str, str]":
    """selfcheck facts: `drift=` the copies that differ, `copies=` the ones
    that still match."""
    found = scan() if found is None else found
    out = {}
    differ = [d.label for d in found if not d.same]
    same = [d.label for d in found if d.same]
    if differ:
        out["drift"] = ",".join(differ)
    if same:
        out["copies"] = ",".join(same)
    return out

# --- the fix: links in place of copies ---------------------------------------------


def _daemon_reload() -> None:
    if shutil.which("systemctl"):
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True,
                       timeout=30)


def backup_path(path: Path) -> Path:
    """`<path>.bak-<YYYYMMDD>`, numbered when that is taken. Beside the file:
    runsv only runs `run`/`finish`/`log/run`, and systemd only loads unit
    suffixes, so a backup there is inert."""
    base = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d')}")
    cand, n = base, 1
    while cand.exists() or cand.is_symlink():
        cand = base.with_name(f"{base.name}.{n}")
        n += 1
    return cand


def relink(found: "list[Drift]", *, force: bool = False, dry_run: bool = False,
           echo: "Callable[[str], None]" = print) -> int:
    """Replace each copy with a symlink to its checkout file, the copy kept as
    `<file>.bak-<date>`. A copy that differs is only reported unless `force`:
    it may hold a local edit someone meant. Exit code: 0 all linked (or
    nothing to do), 1 a differing copy left alone or a link that failed."""
    rc = 0
    reload_ = False
    for d in found:
        if not d.same and not force:
            echo(f"{d.label}: differs from {d.source} — left alone (compare them; "
                 "--force links it, keeping the copy as .bak-<date>)")
            rc = 1
            continue
        bak = backup_path(d.path)
        if dry_run:
            echo(f"# would link {d.path} -> {d.source} (copy kept as {bak.name})")
            continue
        try:
            mode = d.path.stat().st_mode
            os.replace(d.path, bak)
            try:
                d.path.symlink_to(d.source)
            except OSError:
                os.replace(bak, d.path)
                raise
        except OSError as e:
            echo(f"{d.label}: could not link: {e}")
            rc = 1
            continue
        if mode & 0o111 and not os.access(d.source, os.X_OK):
            echo(f"{d.label}: note: {d.source} is not executable")
        echo(f"{d.label}: linked to {d.source} ({'identical' if d.same else 'differed'};"
             f" copy kept as {bak.name})")
        reload_ = reload_ or d.kind == "systemd"
    if reload_:
        try:
            _daemon_reload()
        except (OSError, subprocess.SubprocessError) as e:
            echo(f"systemctl --user daemon-reload failed: {e}")
    if not found:
        echo("no service file here is a copy of this checkout's: nothing to link")
    return rc


def main(argv: "Optional[list[str]]" = None) -> int:
    """`media-setup link-services [--force] [--dry-run]`."""
    import argparse

    ap = argparse.ArgumentParser(prog="media-setup link-services")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    return relink(scan(), force=a.force, dry_run=a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
