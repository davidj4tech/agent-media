"""Moved to `agent_media_server.harnesses`; this name is kept so old imports resolve.

Importing this module hands back the server's module object itself, so a
monkeypatch through the old name reaches the code the routes run.
"""

import sys

from agent_media_server import harnesses as _harnesses

sys.modules[__name__] = _harnesses
