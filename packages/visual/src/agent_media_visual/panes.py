"""Moved to `agent_media_server.panes`; this name is kept so old imports resolve.

Not a copy and not a re-export: importing this module hands back the server's
module object itself, so `agent_media_visual.panes.send` and a monkeypatch of
it are the very function the server calls.
"""

import sys

from agent_media_server import panes as _panes

sys.modules[__name__] = _panes
