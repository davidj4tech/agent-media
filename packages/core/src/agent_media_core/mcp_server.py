"""MCP control surface for agent-media.

Two entrypoints over the same tool definitions:

  * `media-mcp`      — stdio transport, for Claude Code (user-scope
                       registration via `claude mcp add`).
  * `media-mcp-http` — streamable-HTTP transport, for remote callers
                       (sp4r, HA, anything off-box). Bind via
                       MEDIA_MCP_HOST / MEDIA_MCP_PORT (defaults
                       127.0.0.1:8765 — set MEDIA_MCP_HOST to the
                       Tailscale IP to expose on the tailnet).

Tools cover the surface RESTRUCTURE.md called for: speech.{pause,
resume,stop,now_playing,history,replay_last} and music.{play,pause,
resume,stop,volume,now_playing,seek} plus a convenience `say` that
submits a one-shot Event through the same intake pipeline the hooks
use.

Replaces the legacy Node `packages/media-mcp/server.js` end-to-end.
"""

import logging
import os

from mcp.server.fastmcp import FastMCP


def _host() -> str:
    return os.environ.get("MEDIA_MCP_HOST", "127.0.0.1")


def _port() -> int:
    try:
        return int(os.environ.get("MEDIA_MCP_PORT", "8765"))
    except ValueError:
        return 8765

from .intake._env import load_env_file
from .route import coerce_content_type, detect_content_type
from .sinks import SinkMusic, SinkSpeech
from .state import StateStore
from .types import Event, Priority, Source, Target


log = logging.getLogger(__name__)

mcp = FastMCP("agent-media", host=_host(), port=_port())


# --- shared singletons ----------------------------------------------------

def _state() -> StateStore:
    if not hasattr(_state, "_v"):
        _state._v = StateStore()  # type: ignore[attr-defined]
    return _state._v  # type: ignore[attr-defined]


def _speech() -> SinkSpeech:
    if not hasattr(_speech, "_v"):
        _speech._v = SinkSpeech()  # type: ignore[attr-defined]
    return _speech._v  # type: ignore[attr-defined]


def _music() -> SinkMusic:
    if not hasattr(_music, "_v"):
        _music._v = SinkMusic()  # type: ignore[attr-defined]
    return _music._v  # type: ignore[attr-defined]


def _target(name: str) -> Target:
    return Target(name=name or "local")


# --- say: one-shot synthesize+play ----------------------------------------

@mcp.tool()
def say(text: str,
        voice: str = "",
        engine: str = "",
        target: str = "local",
        priority: str = "normal") -> dict:
    """Synthesize `text` and play it through sink-speech.

    Args:
        text: What to speak.
        voice: Override the render voice (engine-specific). Empty = use default.
        engine: Override engine (edge / openai / qwen / realtime).
            Empty = use MEDIA_RENDER_ENGINE default.
        target: Sink target. Default "local".
        priority: "low" / "normal" / "high" / "urgent".
    """
    from .intake.submit import submit_event

    try:
        prio = Priority(priority)
    except ValueError:
        prio = Priority.NORMAL

    history_id = submit_event(Event(
        text=text, source=Source.MCP,
        priority=prio,
        voice=voice or None,
        engine=engine or None,
        target=_target(target),
        metadata={"kind": "say"},
    ), state=_state())
    return {"history_id": history_id}


# --- speech sink controls --------------------------------------------------

@mcp.tool()
def speech_pause(target: str = "local") -> dict:
    """Pause the speech sink (mid-clip). Use `speech_resume` to continue."""
    _speech().pause(_target(target))
    return {"ok": True}


@mcp.tool()
def speech_resume(target: str = "local") -> dict:
    """Resume the speech sink."""
    _speech().resume(_target(target))
    return {"ok": True}


@mcp.tool()
def speech_stop(target: str = "local") -> dict:
    """Stop the speech sink. Drops the current clip."""
    _speech().stop(_target(target))
    return {"ok": True}


@mcp.tool()
def speech_now_playing(target: str = "local") -> dict:
    """What the speech sink is currently playing — path + position."""
    t = _target(target)
    s = _speech()
    return {"idle": s.idle(t), "position_ms": s.position(t)}


@mcp.tool()
def speech_history(limit: int = 10) -> list[dict]:
    """The last N speech clips. Most recent first."""
    # Exclude "Claude is waiting" notif clips: they're alerts, not responses.
    # Over-fetch so filtering still leaves `limit` real responses.
    rows = _state().recent_history(sink="speech", limit=max(limit * 4, limit + 50))
    rows = [r for r in rows
            if not (isinstance(r.get("extras"), dict)
                    and r["extras"].get("kind") == "notif")]
    return rows[:limit]


@mcp.tool()
def speech_replay_last(target: str = "local") -> dict:
    """Replay the most recent speech clip."""
    # Skip "Claude is waiting" notif clips: replay the last real response.
    rows = _state().recent_history(sink="speech", limit=50)
    rows = [r for r in rows
            if not (isinstance(r.get("extras"), dict)
                    and r["extras"].get("kind") == "notif")]
    if not rows:
        return {"ok": False, "reason": "no history"}
    uri = rows[0].get("uri")
    if not uri:
        return {"ok": False, "reason": "history row missing uri"}
    _speech().play(uri, _target(target))
    return {"ok": True, "uri": uri}


# --- music sink controls --------------------------------------------------

@mcp.tool()
def music_play(uri: str, replace: bool = True, target: str = "local",
               content_type: str = "") -> dict:
    """Play a URI on the music sink (Mopidy) — music or longform alike.

    Args:
        uri: Mopidy URI — e.g. `yt:https://...`, `https://stream.url`,
            `local:track:...`.
        replace: Clear the queue first (default True).
        content_type: How speech should interrupt this. `music`/`dj-set`/
            `ambient` duck the volume; `audiobook`/`podcast` pause and
            resume (with a short rewind) so you don't miss narration.
            Defaults to auto-detection from the URI — which classifies a
            bare YouTube/HTTP URL as music, so set `audiobook` explicitly
            for spoken-word content from YouTube.
    """
    _music().play(uri, _target(target), replace=replace)
    ct = coerce_content_type(content_type) or detect_content_type(uri)
    _state().set_music_intent(uri, ct.value)
    return {"ok": True, "uri": uri, "content_type": ct.value}


@mcp.tool()
def music_pause(target: str = "local") -> dict:
    """Pause music."""
    _music().pause(_target(target))
    return {"ok": True}


@mcp.tool()
def music_resume(target: str = "local") -> dict:
    """Resume music."""
    _music().resume(_target(target))
    return {"ok": True}


@mcp.tool()
def music_stop(target: str = "local") -> dict:
    """Stop music and clear the playlist."""
    _music().stop(_target(target))
    _state().clear_music_intent()
    return {"ok": True}


@mcp.tool()
def music_volume(level: int, target: str = "local") -> dict:
    """Set music volume (0-100). For temporary ducking during speech,
    let the route coordinator handle it — this is for the listener's
    own preference.
    """
    _music().duck(_target(target), max(0, min(100, level)))
    return {"ok": True, "level": level}


@mcp.tool()
def music_now_playing(target: str = "local") -> dict:
    """Current track URI + playback position in ms."""
    t = _target(target)
    m = _music()
    return {"uri": m.now_playing_uri(t), "position_ms": m.position(t)}


@mcp.tool()
def music_seek(position_ms: int, target: str = "local") -> dict:
    """Seek the current music track to absolute position (ms)."""
    _music().seek_cur(_target(target), max(0, position_ms))
    return {"ok": True, "position_ms": position_ms}


# --- entrypoint -----------------------------------------------------------

def _configure_logging() -> None:
    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main() -> None:
    """stdio entrypoint — for Claude Code and other local MCP clients."""
    load_env_file("media-mcp")
    _configure_logging()
    mcp.run()


def main_http() -> None:
    """streamable-HTTP entrypoint — for remote callers over Tailscale."""
    load_env_file("media-mcp-http")
    _configure_logging()
    log.info("media-mcp http listening on %s:%d", _host(), _port())
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
