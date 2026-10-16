"""Render engines: edge (built-in) + third-party.

`edge` is the only engine core ships — it's zero-config (no API key) and is the
universal default. Every other engine (openai, qwen, realtime, …) is an
optional package registered under the `agent_media.render_engines` entry-point
group and discovered at runtime (see ../extensions.py and docs/reference/extensions.md),
so core imports none of them.

`render_text(..., engine="<name>")` dispatches `edge` directly and any other
name through the extension registry. A non-edge failure — including an engine
that isn't installed — falls back to edge when `fallback_to_edge=True`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional


EDGE_DEFAULT_VOICE = "en-US-AriaNeural"

# Microsoft's free edge-tts websocket endpoint intermittently rejects the
# handshake with 503/403 (throttling / rotated Sec-MS-GEC token). A single
# failure used to silently drop that sentence — the caller logs it and moves
# on, and since edge is the only engine core ships there's no fallback. The
# failures are transient and clustered, so a short retry-with-backoff recovers
# the sentence instead of leaving a gap in the spoken response. Tunable via
# MEDIA_EDGE_RETRIES (extra attempts) / MEDIA_EDGE_RETRY_BACKOFF_S (base delay).
_EDGE_RETRIES = int(os.environ.get("MEDIA_EDGE_RETRIES", "3"))
_EDGE_RETRY_BACKOFF_S = float(os.environ.get("MEDIA_EDGE_RETRY_BACKOFF_S", "0.6"))


# Microsoft retired the legacy synthesis host for some regions: since ~10 Oct
# 2026 `speech.platform.bing.com/consumer/speech/synthesize/readaloud` answers
# 403 from Australian IPs (it still serves from EU hosts, so this reproduces on
# some machines and not others with identical code). The fix is upstream
# edge-tts PR #491, which was CLOSED unmerged, and no release carries it — 7.3.1
# still ships only the legacy host. Patching each venv by hand does not survive
# `uv sync` or a rebuild, which silently restores the 403 (agent-media #149).
#
# So repair it in place, once per process, before the first edge render. This is
# idempotent and self-retiring: the moment a release (or anything else) provides
# the new endpoint, the check finds it and does nothing.
_EDGE_NEW_HOST = "api.msedgeservices.com"
_edge_endpoint_checked = False


def _ensure_edge_endpoint() -> None:
    """Make the installed edge_tts use the current synthesis host.

    Best-effort: a read-only or unexpected install is left alone and rendering
    proceeds, so this can only ever fix the 403, never cause one.
    """
    global _edge_endpoint_checked
    if _edge_endpoint_checked:
        return
    _edge_endpoint_checked = True
    if os.environ.get("MEDIA_EDGE_NO_ENDPOINT_PATCH"):
        return
    try:
        import edge_tts.constants as _c

        src = Path(_c.__file__)
        text = src.read_text()
        if _EDGE_NEW_HOST in text:
            return
        if "TRUSTED_CLIENT_TOKEN" not in text:
            return  # not the layout we know; do not guess
        src.write_text(
            text.rstrip("\n")
            + "\n\n# Added by agent-media (edge-tts PR #491; see agent-media #149):\n"
            + "# the legacy host 403s synthesis in some regions. Reapplied automatically\n"
            + "# after any venv rebuild; removed by upstream shipping the new endpoint.\n"
            + "WSS_URL = (\n"
            + f'    "wss://{_EDGE_NEW_HOST}/tts/cognitiveservices/websocket/v1"\n'
            + '    f"?Ocp-Apim-Subscription-Key={TRUSTED_CLIENT_TOKEN}"\n'
            + ")\n"
        )
        print(
            f"agent-media: patched {src} to use {_EDGE_NEW_HOST} "
            "(edge-tts PR #491; legacy host 403s in some regions)",
            file=sys.stderr,
        )
    except Exception as exc:  # never block a render on this
        print(f"agent-media: edge endpoint check skipped: {exc}", file=sys.stderr)


def _engine_path(bin_name: str) -> str:
    """Absolute `bin_name`, looked up beside our own interpreter first.

    Render engines are console scripts installed into the same virtualenv as
    agent-media, and they are found only because that venv's bin is on the
    caller's PATH. A hook, a systemd unit or an editor daemon inherits a
    minimal PATH — /usr/bin and little else — and then the engine is missing
    for reasons that have nothing to do with the engine. Since we know where
    our own interpreter lives, we know where its scripts live too.
    """
    if os.path.sep in bin_name:
        return bin_name
    cand = Path(sys.executable).parent / bin_name
    if cand.exists():
        return str(cand)
    return shutil.which(bin_name) or bin_name


def _render_edge(text: str, outfile: Path, *, voice: str, edge_bin: str,
                 subtitles: Optional[Path] = None) -> tuple[bool, str]:
    """Render one clip. With `subtitles`, also write the word timings.

    edge-tts reports a WordBoundary as it synthesises, and `--write-subtitles`
    turns those into an SRT cue per word in the *same* request — no second
    render, no extra latency. That is what lets a host that only receives the
    finished audio still know where each sentence starts (see subtitles.py).
    """
    _ensure_edge_endpoint()
    err = ""
    for attempt in range(_EDGE_RETRIES + 1):
        try:
            proc = subprocess.run(
                [_engine_path(edge_bin), "--text", text, "--voice", voice,
                 "--write-media", str(outfile)]
                + (["--write-subtitles", str(subtitles)] if subtitles else []),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except OSError as e:
            # "Not installed" is a documented outcome of this function — the
            # caller falls back to another engine on it. Letting it escape as
            # FileNotFoundError instead turned a missing binary into a
            # traceback out of `media`, with no fallback attempted at all.
            return False, f"{edge_bin}: {e}"
        err = proc.stderr.decode(errors="replace").strip()
        ok = proc.returncode == 0 and outfile.exists() and outfile.stat().st_size > 0
        if ok:
            return True, err
        # Last attempt failed — give up and report the error upstream.
        if attempt >= _EDGE_RETRIES:
            break
        # Exponential backoff so a brief 503/403 throttle window clears before
        # the retry (0.6s, 1.2s, 2.4s by default).
        time.sleep(_EDGE_RETRY_BACKOFF_S * (2 ** attempt))
    return False, err


# The only engine bundled with core. Everything else arrives via entry points;
# `extensions.all_engine_names()` reports built-ins + installed plugins.
KNOWN_ENGINES = ("edge",)


def _render_one(
    text: str,
    outfile: Path,
    *,
    engine: str,
    voice: Optional[str],
    edge_voice: str,
    edge_bin: str,
    subtitles: Optional[Path] = None,
) -> tuple[bool, str]:
    """Render via a single engine, no fallback. Returns (ok, err).

    `edge` is handled directly; any other engine is resolved from the
    `agent_media.render_engines` entry-point registry. `subtitles` is edge-only
    — plugins take no such argument, and passing one would break every one of
    them; a caller that asked for timings just doesn't get a file, which is how
    it tells that this render couldn't measure them.
    """
    if engine == "edge":
        return _render_edge(text, outfile, voice=voice or edge_voice,
                            edge_bin=edge_bin, subtitles=subtitles)
    from ..extensions import get_render_engine
    ext = get_render_engine(engine)
    if ext is None:
        return False, f"unknown engine: {engine!r} (not installed?)"
    try:
        return ext(text, outfile, voice=voice)
    except Exception as e:  # noqa: BLE001 — isolate plugin faults; fall back below
        return False, f"engine {engine!r} raised: {e}"


def render_text(
    text: str,
    outfile: Path,
    *,
    engine: str,
    voice: Optional[str] = None,
    edge_voice: str = EDGE_DEFAULT_VOICE,
    edge_bin: str = "edge-tts",
    fallback_to_edge: bool = True,
    on_fallback: Optional[Callable[[str, str], None]] = None,
    subtitles: Optional[Path] = None,
) -> tuple[bool, str]:
    """Render `text` to `outfile` via `engine`, with a fallback. Returns (ok, err).

    On a primary-engine failure (errored, raised, isn't installed — or, for
    edge, exhausted its retries) the render falls back to a second engine.
    The fallback is `MEDIA_RENDER_FALLBACK_ENGINE` if set, else `edge` when
    `fallback_to_edge=True` (the historical default). This lets an edge-primary
    setup fall through to e.g. `openai` when Microsoft's endpoint 503s persist,
    so a sentence is never silently dropped while *either* provider is up.
    `on_fallback(engine, err)` is called with the primary's error so callers
    can log/notify. A fallback equal to the primary (or unset) is a no-op.
    """
    ok, err = _render_one(text, outfile, engine=engine, voice=voice,
                          edge_voice=edge_voice, edge_bin=edge_bin,
                          subtitles=subtitles)
    if ok or not fallback_to_edge:
        # `fallback_to_edge=False` means "no fallback, give me the raw result".
        return ok, err

    # Fallback enabled: use MEDIA_RENDER_FALLBACK_ENGINE if set, else edge.
    fallback = os.environ.get("MEDIA_RENDER_FALLBACK_ENGINE") or "edge"
    if fallback == engine:
        return ok, err

    if on_fallback is not None:
        on_fallback(engine, err)
    # The fallback resolves its own default voice (the primary's voice belongs
    # to the primary's namespace); edge is pinned to edge_voice for back-compat.
    fb_voice = edge_voice if fallback == "edge" else None
    return _render_one(text, outfile, engine=fallback, voice=fb_voice,
                       edge_voice=edge_voice, edge_bin=edge_bin,
                       subtitles=subtitles)
