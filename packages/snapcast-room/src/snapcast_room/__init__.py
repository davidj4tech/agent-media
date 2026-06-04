"""snapcast-room: terse CLI over Snapcast's JSON-RPC for whole-house routing.

The snapcast/pipewire plumbing that survived the agent-media restructure —
everything else (TTS render engines, agent hooks, the watcher, the clip
server, the HTTP fan-out) moved into ``agent-media-core``. Ships the
``am-snap`` CLI (with ``aar-snap`` kept as a backward-compatible alias).
"""

__version__ = "0.4.0"
