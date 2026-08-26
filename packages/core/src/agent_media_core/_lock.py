"""Cross-platform advisory file locking.

A drop-in stand-in for the subset of ``fcntl`` this package uses -- ``flock``
plus the ``LOCK_EX`` / ``LOCK_NB`` / ``LOCK_UN`` / ``LOCK_SH`` flags -- so a
module can

    from .. import _lock as fcntl

and keep calling ``fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)`` verbatim.

On POSIX this re-exports the real ``fcntl.flock``, so behaviour is byte-for-byte
unchanged: whole-file advisory locks, released on close or process death, and
shared across hosts on a networked filesystem (the tcp:// fleet leans on that).

On Windows ``fcntl`` does not exist. We emulate the same call surface with
``msvcrt.locking`` on a single sentinel byte at a fixed high offset, chosen so
the (mandatory) Windows lock never overlaps the lockfile's own contents -- a
waiter still reads the holder's signature at offset 0 while the lock is held,
which the POSIX flock path allows and the speech-lock waiter depends on. Only
the non-blocking exclusive acquire and the unlock are used in anger; the
blocking acquire is emulated as a bounded retry.
"""
from __future__ import annotations

import os

try:  # POSIX -- use the real thing, unchanged.
    from fcntl import (  # noqa: F401
        LOCK_EX,
        LOCK_NB,
        LOCK_SH,
        LOCK_UN,
        flock,
    )
except ImportError:  # Windows.
    import msvcrt
    import time

    # BSD flock flag values, mirrored so ``LOCK_EX | LOCK_NB`` composes the same.
    LOCK_SH = 1
    LOCK_EX = 2
    LOCK_NB = 4
    LOCK_UN = 8

    # Lock one byte far past any lockfile content, so the mandatory Windows lock
    # never blocks a reader fetching the holder's id/signature near offset 0.
    _SENTINEL = 0x7FFF_FFFF
    _BLOCK_RETRIES = 50  # ~5s of 0.1s retries for the (currently unused) blocking path

    def _as_fd(f) -> int:
        return f if isinstance(f, int) else f.fileno()

    def flock(f, op: int) -> None:  # noqa: D401 -- mirrors fcntl.flock's signature
        """``fcntl.flock`` work-alike over ``msvcrt.locking`` (Windows only)."""
        fd = _as_fd(f)
        saved = os.lseek(fd, 0, os.SEEK_CUR)
        os.lseek(fd, _SENTINEL, os.SEEK_SET)
        try:
            if op & LOCK_UN:
                try:
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    # flock(LOCK_UN) on an unheld file is a no-op; msvcrt raises.
                    pass
            elif op & LOCK_NB:
                # Raises OSError when the byte is held -- exactly flock's contract.
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                for attempt in range(_BLOCK_RETRIES):
                    try:
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if attempt == _BLOCK_RETRIES - 1:
                            raise
                        time.sleep(0.1)
        finally:
            os.lseek(fd, saved, os.SEEK_SET)
