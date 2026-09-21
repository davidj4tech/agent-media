"""Moved to `agent_media_server.abs_item`; this name is kept so old imports resolve.

Importing this module hands back the server's module object itself, so a
monkeypatch through the old name reaches the code the routes run.
"""

import sys

from agent_media_server import abs_item as _abs_item

sys.modules[__name__] = _abs_item
