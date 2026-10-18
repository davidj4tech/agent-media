"""Shared XDG-ish path helpers.

On Termux+proot, the proot-side `/home/<user>` is bind-mounted to the
Termux-native `/data/data/com.termux/files/home`, but services running
outside the proot (runit, MCP HTTP) only see the Termux-native path.
A second StateStore opened from native HOME diverges from the proot
one, breaking dedup and history queries across environments.

These helpers force the Termux-native HOME when it's present so all
clients share one state.db / stamp dir / audio dir.
"""

from __future__ import annotations

import os
from pathlib import Path


_TERMUX_NATIVE_HOME = Path("/data/data/com.termux/files/home")


def canonical_home() -> Path:
    """The HOME both proot and native processes should resolve to.

    Falls back to `Path.home()` off-Termux.
    """
    if _TERMUX_NATIVE_HOME.is_dir():
        return _TERMUX_NATIVE_HOME
    return Path.home()


def state_dir() -> Path:
    """`XDG_STATE_HOME/agent-media` — canonical across proot/native."""
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else canonical_home() / ".local" / "state"
    return root / "agent-media"


def cache_dir() -> Path:
    """`XDG_CACHE_HOME/agent-media` — canonical across proot/native."""
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else canonical_home() / ".cache"
    return root / "agent-media"


# ---- rendered speech -------------------------------------------------------
#
# A rendered clip is the only copy of what was said in that voice: replay, the
# feed and the app all read it back from the path its history row stores. It
# lived under `cache_dir()/audio` until 18 Oct 2026, where clearing the cache
# to free disk took replay of every past reply with it (David: "keep ~/.cache
# for true caches"). It is state now. The other folders under `cache_dir()`
# (books, docs, web-clips, matrix-audio, music) are fetched or re-rendered on
# demand, and stay.

_moved_once: "set[tuple[str, str]]" = set()


def _default_state_root() -> Path:
    return canonical_home() / ".local" / "state"


def speech_audio_dir() -> Path:
    """Where rendered speech clips live: `state_dir()/audio`, created.

    `MEDIA_AUDIO_DIR` names it outright (and nothing is migrated). Otherwise
    the first call in a process moves clips left in the old cache location
    (`migrate_speech_audio`) — but only when the cache and state roots belong
    together: a test or sandbox that points XDG_STATE_HOME somewhere of its
    own while the cache is still the real one must not carry the real clips
    off into a temporary dir.
    """
    explicit = os.environ.get("MEDIA_AUDIO_DIR")
    if explicit:
        d = Path(explicit)
        d.mkdir(parents=True, exist_ok=True)
        return d
    new = state_dir() / "audio"
    old = cache_dir() / "audio"
    key = (str(old), str(new))
    if key not in _moved_once:
        _moved_once.add(key)
        own_state = bool(os.environ.get("XDG_STATE_HOME")) and (
            Path(os.environ["XDG_STATE_HOME"]) != _default_state_root())
        if not own_state or os.environ.get("XDG_CACHE_HOME"):
            try:
                migrate_speech_audio(old, new)
            except OSError as e:
                import logging
                logging.getLogger(__name__).warning(
                    "speech clips: could not move %s to %s (%s)", old, new, e)
    new.mkdir(parents=True, exist_ok=True)
    return new


def _move_entry(src: Path, dst: Path) -> None:
    """Move one file or folder to `dst`, which does not exist. A rename where
    both are on one filesystem; across two, a copy under a hidden name that is
    renamed into place before the original goes, so nothing is ever only
    half-written or only deleted."""
    try:
        os.rename(src, dst)
        return
    except OSError as e:
        import errno
        if e.errno != errno.EXDEV:
            raise
    import shutil
    part = dst.with_name(f".{dst.name}.{os.getpid()}.part")
    if src.is_dir() and not src.is_symlink():
        shutil.copytree(src, part, symlinks=True)
        os.replace(part, dst)
        shutil.rmtree(src)
    else:
        shutil.copy2(src, part, follow_symlinks=False)
        os.replace(part, dst)
        os.unlink(src)


def migrate_speech_audio(old: Path, new: Path) -> dict:
    """Move clips from `old` (the cache) to `new` (the state dir), once.

    Idempotent, and safe for two processes at once (an flock beside `new`).
    Each entry is moved, never deleted: one whose name already exists in
    `new` is left where it is. When `old` is left empty it becomes a symlink
    to `new`, so every path stored before the move — in other hosts'
    records, logs, the clip server's `/audio/` URL root — still resolves;
    and the history rows on this host are rewritten to the new path, so a
    replay still finds its clips after the cache is cleared and the link
    with it. A missing `old` is given the link too (the clip server serves
    the cache folder). Returns {"moved", "kept", "rows"}.
    """
    import fcntl

    out = {"moved": 0, "kept": 0, "rows": 0}
    new.mkdir(parents=True, exist_ok=True)
    lock = new.parent / ".audio-migrate.lock"
    with open(lock, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        if old.is_symlink():
            return out                   # done before (or someone else's link)
        if old.is_dir():
            for entry in sorted(os.scandir(old), key=lambda e: e.name):
                dst = new / entry.name
                if os.path.lexists(dst):
                    out["kept"] += 1
                    continue
                try:
                    _move_entry(Path(entry.path), dst)
                    out["moved"] += 1
                except FileNotFoundError:
                    continue             # pruned or moved meanwhile
            try:
                os.rmdir(old)
            except OSError:
                pass                     # not empty: a writer still uses it
        elif os.path.lexists(old):
            return out                   # a file in the way: leave it be
        linked = False
        if not os.path.lexists(old):
            try:
                old.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(new, old, target_is_directory=True)
                linked = True
            except FileExistsError:
                pass
        # Every name under `old` now also exists under `new` (moved, or kept
        # because `new` had one), so pointing the rows at `new` loses nothing.
        if out["moved"] or linked:
            from .state.store import StateStore
            try:
                store = StateStore()
                try:
                    out["rows"] = store.relocate_prefix(str(old), str(new))
                finally:
                    store.close()
            except Exception as e:  # noqa: BLE001 — the files moved; the link covers it
                import logging
                logging.getLogger(__name__).warning(
                    "speech clips: history paths not rewritten (%s)", e)
    return out
