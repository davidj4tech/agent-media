"""`audiobook-fetch` — the console script in front of the shell helper.

The acquisition path is bash: ssh to the phone, yt-dlp there, rsync back,
scan, play. It stays bash because every line of it is another process on
another host, and because it works — a rewrite would be a rewrite of the one
thing in this repo that cannot be tested without a phone, a YouTube session and
a residential IP.

What it did not have was a home. It lived in the dotfiles repo, deployed by a
symlink, while the code that calls it (`library.fetch_cmd`) shipped in this
package — so a host could install agent-media, find `media book import-youtube`
in the help, and get "audiobook-fetch unavailable" for a helper the same
project wrote.

So the script ships as package data and this puts it on PATH. `execv`, not a
subprocess: the caller's exit status, stdout and signals are the script's, and
`library.start_fetch_many` detaches one of these expecting exactly that.
"""

from __future__ import annotations

import os
import sys


def main(argv=None) -> int:
    from ..setup import shipped_bin

    script = shipped_bin("audiobook-fetch")
    if not script.is_file():
        print(f"audiobook-fetch: helper missing from this install ({script})",
              file=sys.stderr)
        return 127
    args = list(argv if argv is not None else sys.argv[1:])
    # Its own shebang, not `sh`: the helper is bash and uses arrays, and
    # /bin/sh is dash on Debian — which fails at the first `urls=()` with a
    # syntax error that reads like a corrupt install rather than a wrong
    # interpreter. `bash` by name is the fallback for an install that lost the
    # executable bit (a wheel can), because the script needs bash either way.
    if os.access(script, os.X_OK):
        os.execv(str(script), [str(script), *args])
    os.execv("/bin/bash", ["/bin/bash", str(script), *args])
    return 127          # unreachable: execv does not return


if __name__ == "__main__":
    sys.exit(main())
