"""sasonica — Sasonica's server as one command.

The single binary (deploy/binary/build.sh, a PyApp launcher with Python and
every package already installed inside it) runs `python -m sasonica`. PyApp
does not pass on the name it was called by, so a `media` symlink to the binary
cannot say which command it wants: every command is a word after `sasonica`
instead, and `sasonica install` writes a two-line shim for each one into
~/.local/bin so the names hooks and run scripts already use keep working.

docs/proposals/2026-09-29-single-binary.md.
"""
