"""Claude Code intake adapter.

Reads Claude Code's hook JSON from stdin, extracts the speech text, and
hands it to `submit_event`. Replaces the legacy bash hook
(`packages/audio-relay/src/agent_audio_relay/shell/hooks/claude-code-tts-hook.sh`).

Settings.json wires it as:

    "hooks": {
      "Stop":         [{"hooks":[{"type":"command",
                                  "command":"media-hook-claude-code",
                                  "timeout":30}]}],
      "Notification": [{"hooks":[{"type":"command",
                                  "command":"media-hook-claude-code",
                                  "timeout":30}]}],
      "PreToolUse":   [{"matcher":"AskUserQuestion",
                        "hooks":[{"type":"command",
                                  "command":"media-hook-claude-code",
                                  "timeout":30}]}]
    }

PreToolUse(AskUserQuestion) is what actually reads a multiple-choice prompt
aloud — Claude Code never fires a Notification for the question modal, so the
read-out has to hang off the tool's pre-execution hook.

Behaviours preserved from the bash version:
  - Sources `~/.config/agent-audio-relay.env` (or `RELAY_ENV_FILE`) so
    OPENAI_API_KEY etc. don't have to live in settings.json.
  - Notification suppression: skip if another notif fired within 120s,
    or a Stop played within 90s.
  - Stop: read the latest assistant text from the transcript JSONL.
    Skip tool-call-only turns (no text content).
  - Dedup key (text-hash) collapses duplicate Stop / Stop+notif races.

Long-text routing (the old `CLAUDE_TTS_LONG_THRESHOLD` split into
tts-stream) is gone — Phase 3 of the restructure locked in a single
stream-only render path. The realtime engine produces audio in chunks
fast enough that long replies are no longer a special case.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path  # still used by _stamp_dir, _latest_assistant_text
from typing import Optional

from .._paths import state_dir
from ..state import StateStore
from ..types import Event, Priority, Source
from ._env import load_env_file
from ._text import strip_markdown
from .submit import submit_event


log = logging.getLogger(__name__)

# Distinguishable-accent voices used to give each tmux session its own
# voice when no explicit MEDIA_SESSION_VOICE_MAP pin matches. Override the
# whole set with MEDIA_SESSION_VOICE_POOL (comma-separated).
_DEFAULT_VOICE_POOL = (
    "en-AU-NatashaNeural",  # Australian
    "en-NZ-MollyNeural",    # New Zealand
    "en-GB-SoniaNeural",    # British
    "en-IE-EmilyNeural",    # Irish
    "en-CA-ClaraNeural",    # Canadian
    "en-GB-LibbyNeural",    # British (younger)
)


def _tmux(args: list[str], timeout: float = 2.0) -> str:
    """Run a tmux command, return stripped stdout or empty string."""
    import subprocess
    try:
        r = subprocess.run(["tmux", *args],
                           capture_output=True, text=True, timeout=timeout)
        return (r.stdout or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _session_name() -> str:
    """Current tmux session name, or "" when not running inside tmux."""
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return ""
    return _tmux(["display-message", "-p", "-t", pane, "#{session_name}"])


def _voice_for_session(sess: str) -> Optional[str]:
    """Pick a TTS voice for the given tmux session name.

    Resolution order:
      1. Return None (→ daemon default voice) when disabled via
         MEDIA_SESSION_VOICE_ENABLED=0 or when not in tmux (no session).
      2. Explicit pin from MEDIA_SESSION_VOICE_MAP, formatted
         "name=voice,name=voice"; first exact session-name match wins.
      3. Stable hash of the session name into the voice pool
         (MEDIA_SESSION_VOICE_POOL overrides the built-in accent set), so a
         given session always gets the same voice without configuration.
    """
    if not sess or os.environ.get("MEDIA_SESSION_VOICE_ENABLED", "1") == "0":
        return None

    for pair in os.environ.get("MEDIA_SESSION_VOICE_MAP", "").split(","):
        name, _, voice = pair.partition("=")
        if name.strip() == sess and voice.strip():
            return voice.strip()

    pool_env = os.environ.get("MEDIA_SESSION_VOICE_POOL", "")
    pool = [v.strip() for v in pool_env.split(",") if v.strip()] \
        or list(_DEFAULT_VOICE_POOL)
    if not pool:
        return None
    h = int(hashlib.sha1(sess.encode("utf-8")).hexdigest(), 16)
    return pool[h % len(pool)]


def _notif_label(sess: str) -> str:
    """Build a "where am I" prefix for the notification text.

    Includes:
      - hostname (short) when there's >1 tmux session running and
        MEDIA_NOTIF_LABEL_HOST != "0" (default on).
      - tmux session name (always, when in tmux)
      - pane title when set (via `select-pane -T` or terminal escape); omitted
        when empty or identical to the session name.

    Returns "" outside tmux or when the user disabled labelling
    (MEDIA_NOTIF_LABEL=0). The AskUserQuestion path uses _ask_location_label
    instead (hierarchical host/session omission + window-name pane locator).
    """
    if os.environ.get("MEDIA_NOTIF_LABEL", "1") == "0":
        return ""
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return ""

    pane_title = _tmux(["display-message", "-p", "-t", pane, "#{pane_title}"])
    sess_count_s = _tmux(["list-sessions", "-F", "#{session_name}"])

    parts: list[str] = []

    sess_count = len([s for s in sess_count_s.splitlines() if s.strip()])
    if sess_count > 1 and os.environ.get("MEDIA_NOTIF_LABEL_HOST", "1") != "0":
        import socket
        host = socket.gethostname().split(".")[0]
        if host:
            parts.append(host)

    if sess:
        parts.append(sess)

    if pane_title and pane_title != sess:
        parts.append(pane_title)

    return " / ".join(parts)


def _active_client_session() -> Optional[str]:
    """Session name of the most-recently-active attached client on this tmux
    server (the one the user last typed in), or None if no client is attached.

    Used as the "current" reference for hierarchical label omission: clients
    attach to the local server, so an attached client means the user is on this
    host (→ host is current, omit it), and its session is the one they're
    working in (→ omit that session from the label).
    """
    out = _tmux(["list-clients", "-F", "#{client_activity}\t#{session_name}"])
    best_ts, best_sess = -1, None
    for line in out.splitlines():
        ts_s, _, sess = line.partition("\t")
        try:
            ts = int(ts_s)
        except ValueError:
            continue
        if ts > best_ts and sess:
            best_ts, best_sess = ts, sess
    return best_sess


def _ask_location_label() -> str:
    """"Where is this question?" prefix for the AskUserQuestion notif.

    Announces host / session / pane, but omits — hierarchically, relative to
    the active tmux client (where the user last typed) — whatever is "current":
      - host:    dropped when a client is attached to this server (user is here)
      - session: dropped when it's the session the user is working in
      - pane:    always kept — window name + window.pane index (the window name
                 tracks the Claude conversation's title, a useful locator; the
                 *pane title* is the transient "AskUserQuestion" tool-status and
                 is deliberately not used)

    So a question from the foreground session reads just its pane; one from a
    background session adds the session; one from a host with nobody attached
    adds the host too. Returns "" outside tmux or when MEDIA_NOTIF_LABEL=0.
    """
    if os.environ.get("MEDIA_NOTIF_LABEL", "1") == "0":
        return ""
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return ""

    info = _tmux(["display-message", "-p", "-t", pane,
                  "#{session_name}\t#{window_name}\t#{window_index}\t#{pane_index}"])
    fields = info.split("\t")
    if len(fields) < 4:
        return ""
    sess, win_name, win_idx, pane_idx = (f.strip() for f in fields[:4])

    active = _active_client_session()
    parts: list[str] = []

    # host — only when nobody is attached here (so the user isn't on this host)
    if active is None and os.environ.get("MEDIA_NOTIF_LABEL_HOST", "1") != "0":
        import socket
        host = socket.gethostname().split(".")[0]
        if host:
            parts.append(host)

    # session — only when it isn't the session the user is working in
    if sess and (active is None or sess != active):
        parts.append(sess)

    # pane — always; window name + index, skipping a name that just repeats
    # the session we already announced
    idx = f"{win_idx}.{pane_idx}" if win_idx and pane_idx else ""
    if win_name and win_name != sess:
        parts.append(f"{win_name} {idx}".strip())
    elif idx:
        parts.append(idx)

    return " / ".join(parts)


def _client_focused_recently(within_seconds: int) -> bool:
    """True if our tmux pane's window is currently displayed by an
    attached client whose user-input activity is within `within_seconds`.

    Used to suppress the "Claude is waiting" notif when the user is
    clearly at the screen and will see the prompt without an audio cue.
    Uses `client_activity` (keystroke/mouse timestamp) — not session or
    window activity, which bumps on assistant output too.
    """
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return False
    state = _tmux(["display-message", "-p", "-t", pane,
                   "#{window_active}:#{session_attached}"])
    try:
        win_active, sess_attached = state.split(":", 1)
    except ValueError:
        return False
    if win_active != "1":
        return False
    try:
        if int(sess_attached or "0") < 1:
            return False
    except ValueError:
        return False
    sess = _tmux(["display-message", "-p", "-t", pane, "#{session_name}"])
    if not sess:
        return False
    out = _tmux(["list-clients", "-t", sess, "-F", "#{client_activity}"])
    now = int(time.time())
    for line in out.splitlines():
        try:
            ts = int(line.strip())
        except ValueError:
            continue
        if now - ts < within_seconds:
            return True
    return False


def _client_pane_focused() -> bool:
    """True if our pane is *the* pane the user is looking at right now: the
    active pane of its session's active window, with an attached client.

    Unlike `_client_focused_recently`, this ignores keystroke recency — it
    answers "is this exactly the focused pane?", not "was the user typing
    here lately?". Used to keep a notification from *interrupting* whatever
    is currently being spoken: a notif from the focused pane is downgraded
    from HIGH to NORMAL so it queues behind the current clip instead of
    preempting it. Requires an attached client so a detached (walked-away)
    session still gets the interrupting HIGH cue.
    """
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return False
    state = _tmux(["display-message", "-p", "-t", pane,
                   "#{window_active}:#{pane_active}:#{session_attached}"])
    try:
        win_active, pane_active, sess_attached = state.split(":", 2)
    except ValueError:
        return False
    if win_active != "1" or pane_active != "1":
        return False
    try:
        return int(sess_attached or "0") >= 1
    except ValueError:
        return False


def _stamp_dir() -> Path:
    d = state_dir() / "claude-stamps"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read_stamp(path: Path) -> Optional[int]:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _write_stamp(path: Path, value: int) -> None:
    try:
        path.write_text(str(value))
    except OSError:
        pass


def _format_ask_question(tool_input: dict) -> str:
    """Render an AskUserQuestion tool input as speakable text.

    A multiple-choice question goes out as a *tool call*, so its text never
    lands in the assistant's spoken reply — the user would otherwise only hear
    the generic "waiting for input" notification. Speak the question(s) and
    their option labels so the choice is audible. Option descriptions are
    omitted (too verbose for TTS); the labels carry the gist.
    """
    questions = tool_input.get("questions") or []
    blocks: list[str] = []
    multi = len(questions) > 1
    for i, q in enumerate(questions, 1):
        qtext = (q.get("question") or "").strip()
        if not qtext:
            continue
        lead = f"Question {i}. " if multi else ""
        opts = q.get("options") or []
        labels = [(o.get("label") or "").strip() for o in opts]
        labels = [l for l in labels if l]
        choice = "".join(f" Option {n}: {l}." for n, l in enumerate(labels, 1))
        tail = " You can pick more than one." if q.get("multiSelect") else ""
        blocks.append(f"{lead}{qtext}{choice}{tail}")
    return " ".join(blocks)


def _latest_assistant_text(transcript_path: Path) -> str:
    """Walk the JSONL transcript from the end, return the most recent
    assistant turn's joined text content. If the latest assistant turn is an
    AskUserQuestion tool call (no text), speak the synthesized question instead.
    Empty string if the latest turn is other tool-call-only or unparseable.
    """
    try:
        lines = transcript_path.read_text().splitlines()
    except OSError:
        return ""
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        msg = obj.get("message") or {}
        if msg.get("role") != "assistant":
            continue
        parts = []
        ask: Optional[dict] = None
        for c in msg.get("content") or []:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "text":
                parts.append(c.get("text") or "")
            elif c.get("type") == "tool_use" and c.get("name") == "AskUserQuestion":
                ask = c.get("input") or {}
        text = "\n".join(p for p in parts if p)
        if text:
            return text
        if ask is not None:
            spoken = _format_ask_question(ask)
            if spoken:
                return spoken
        # Other tool-use-only turn — keep searching backward for the last text.
    return ""


def _latest_ask_question(transcript_path: Path) -> str:
    """If the latest assistant turn contains an AskUserQuestion tool call,
    return its synthesized speakable text; else "".

    AskUserQuestion fires a *Notification* (the turn pauses awaiting input),
    not a Stop — and the generic notif message ("Claude is waiting for your
    input") never includes the question. At notif-fire time the AskUserQuestion
    is the live last assistant turn (no tool_result appended yet), so we read it
    here and speak the actual question + options instead of the generic nudge.
    """
    try:
        lines = transcript_path.read_text().splitlines()
    except OSError:
        return ""
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        msg = obj.get("message") or {}
        if msg.get("role") != "assistant":
            continue
        for c in msg.get("content") or []:
            if (isinstance(c, dict) and c.get("type") == "tool_use"
                    and c.get("name") == "AskUserQuestion"):
                return _format_ask_question(c.get("input") or {})
        # First assistant turn found isn't an AskUserQuestion → not our case.
        return ""
    return ""


def _ask_lead_text(transcript_path: Path) -> str:
    """Assistant prose that precedes the question in the *same* turn.

    When Claude writes an explanation and then calls AskUserQuestion, both the
    text and the tool_use live in one assistant message. PreToolUse speaks only
    the synthesized question, and Stop never fires while the turn is paused on
    the modal — so that lead-in prose would otherwise be silently dropped. Walk
    back to the latest assistant turn and, *only if* it carries the
    AskUserQuestion tool call, return its joined text blocks.
    """
    try:
        lines = transcript_path.read_text().splitlines()
    except OSError:
        return ""
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        msg = obj.get("message") or {}
        if msg.get("role") != "assistant":
            continue
        parts: list[str] = []
        has_ask = False
        for c in msg.get("content") or []:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "text":
                parts.append(c.get("text") or "")
            elif (c.get("type") == "tool_use"
                  and c.get("name") == "AskUserQuestion"):
                has_ask = True
        if not has_ask:
            return ""
        return "\n".join(p for p in parts if p).strip()
    return ""


def _dedup_seen(state: StateStore, text: str, ttl_seconds: int = 300) -> bool:
    """Crude text-hash dedup over the recent history table.

    Returns True if this exact text has been spoken in the last
    `ttl_seconds` (so caller should skip emitting again).
    """
    key = hashlib.sha1(text.encode("utf-8")).hexdigest()
    cutoff = time.time() - ttl_seconds
    for row in state.recent_history(sink="speech", limit=20):
        if (row.get("started_at") or 0) < cutoff:
            break
        extras = row.get("extras") or {}
        if isinstance(extras, str):
            try:
                extras = json.loads(extras)
            except json.JSONDecodeError:
                extras = {}
        if extras.get("dedup_key") == key:
            return True
        if (row.get("text") or "") == text:
            return True
    return False


def _emit_ask(ask: str, payload: dict, lead: str = "") -> int:
    """Speak a synthesized AskUserQuestion (question + option labels).

    Prefixes the hierarchical host/session/pane label, bypasses focus-
    suppression and the notif/stop cooldown windows (the user explicitly wants
    questions read out, and they're infrequent), and text-dedups against the
    recent history so a PreToolUse fire and a stray Notification can't double
    up. Downgrades HIGH→NORMAL when this is the focused pane so the cue queues
    behind whatever is currently speaking instead of preempting it.

    `lead` is any assistant prose that preceded the question in the same turn;
    it's spoken before the question so the explanation isn't lost to the modal.
    """
    sess = _session_name()
    label = _ask_location_label()
    body = f"{lead} {ask}".strip() if lead else ask
    msg = f"{label}: {body}" if label else body
    state = StateStore()
    if _dedup_seen(state, msg):
        return 0
    _write_stamp(_stamp_dir() / "notif-last", int(time.time()))
    priority = Priority.NORMAL if (
        os.environ.get("MEDIA_NOTIF_NO_INTERRUPT_FOCUSED", "1") != "0"
        and _client_pane_focused()) else Priority.HIGH
    submit_event(Event(text=msg, source=Source.CLAUDE_CODE,
                       priority=priority,
                       voice=_voice_for_session(sess),
                       metadata={"kind": "notif", "ask": True,
                                 "session": payload.get("session_id") or ""}),
                 state=state)
    return 0


def _handle_pretooluse(payload: dict) -> int:
    """PreToolUse path — the *real* AskUserQuestion trigger.

    Claude Code does NOT fire a Notification when an AskUserQuestion modal is
    shown (verified: a real question sat unanswered 9 minutes with zero notifs),
    so the old Notification-based read-out never actually ran on a live
    question. PreToolUse fires right as the tool is about to execute — i.e. as
    the modal appears — and hands us `tool_input` directly, no transcript walk.
    We only care about AskUserQuestion; every other tool returns immediately.
    """
    if payload.get("tool_name") != "AskUserQuestion":
        return 0
    ask = strip_markdown(_format_ask_question(payload.get("tool_input") or {}).strip())
    if not ask:
        return 0
    # Prepend any prose Claude wrote before the question in this same turn —
    # the modal swallows it and Stop won't fire while input is pending.
    lead = ""
    tp_raw = (payload.get("transcript_path") or "").strip()
    if tp_raw:
        tp = Path(tp_raw)
        if tp.is_file():
            lead = strip_markdown(_ask_lead_text(tp)).strip()
    return _emit_ask(ask, payload, lead=lead)


def _handle_notification(payload: dict) -> int:
    """Notif path: prefer Claude's `message` field, dedup-skip if a
    Stop just played or a notif fired within the cooldown windows.

    AskUserQuestion is handled by the PreToolUse path (`_handle_pretooluse`),
    not here — Claude Code doesn't emit a Notification for the modal. We still
    keep a belt-and-braces check: if the live last assistant turn *is* an
    AskUserQuestion (e.g. a future Claude Code does start notifying), speak it,
    bypassing focus-suppression / cooldowns. Text-dedup collapses any overlap
    with the PreToolUse read-out.
    """
    ask = ""
    tp_raw = (payload.get("transcript_path") or "").strip()
    if tp_raw:
        tp = Path(tp_raw)
        if tp.is_file():
            ask = strip_markdown(_latest_ask_question(tp).strip())

    if ask:
        return _emit_ask(ask, payload)

    msg = strip_markdown((payload.get("message") or "").strip())
    if not msg:
        return 0

    # If the user has been at the screen recently, don't nag them with
    # audio — they'll see the prompt. Tunable via env, 0 disables.
    focus_window = int(os.environ.get("MEDIA_NOTIF_FOCUS_SUPPRESS", "180"))
    if focus_window > 0 and _client_focused_recently(focus_window):
        return 0

    sess = _session_name()
    label = _notif_label(sess)
    if label:
        msg = f"{label}: {msg}"

    stamps = _stamp_dir()
    now = int(time.time())
    last_notif = _read_stamp(stamps / "notif-last")
    last_stop = _read_stamp(stamps / "stop-last")
    if last_notif and (now - last_notif) < 120:
        return 0
    if last_stop and (now - last_stop) < 90:
        return 0
    _write_stamp(stamps / "notif-last", now)

    # A notif from the pane the user is actively looking at shouldn't cut off
    # whatever is currently being spoken — speak it, but at NORMAL so it queues
    # instead of preempting. Tunable via env, 0 keeps the old always-HIGH behaviour.
    priority = Priority.HIGH
    if os.environ.get("MEDIA_NOTIF_NO_INTERRUPT_FOCUSED", "1") != "0" \
            and _client_pane_focused():
        priority = Priority.NORMAL

    submit_event(Event(text=msg, source=Source.CLAUDE_CODE,
                       priority=priority,
                       voice=_voice_for_session(sess),
                       metadata={"kind": "notif",
                                 "session": payload.get("session_id") or ""}))
    return 0


def _handle_stop(payload: dict) -> int:
    """Stop path: read the latest assistant text and submit it."""
    raw_path = (payload.get("transcript_path") or "").strip()
    if not raw_path:
        return 0
    tp = Path(raw_path)
    if not tp.is_file():
        return 0

    # Tight retry while the transcript flushes.
    for _ in range(5):
        try:
            ok = tp.stat().st_size > 0 and (time.time() - tp.stat().st_mtime) <= 5
        except OSError:
            ok = False
        if ok:
            break
        time.sleep(0.1)

    text = strip_markdown(_latest_assistant_text(tp))
    if not text:
        return 0

    state = StateStore()
    if _dedup_seen(state, text):
        return 0

    dedup_key = hashlib.sha1(text.encode("utf-8")).hexdigest()
    rid = submit_event(
        Event(text=text, source=Source.CLAUDE_CODE,
              priority=Priority.NORMAL,
              voice=_voice_for_session(_session_name()),
              metadata={"kind": "stop", "dedup_key": dedup_key,
                        "session": payload.get("session_id") or ""}),
        state=state,
    )
    if rid is not None:
        _write_stamp(_stamp_dir() / "stop-last", int(time.time()))
    return 0


def main() -> int:
    if os.environ.get("MEDIA_HOOK_ENABLED", "1") == "0":
        return 0
    if os.environ.get("CLAUDE_TTS_ENABLED", "1") == "0":
        return 0

    load_env_file("hook-claude-code")

    try:
        raw = sys.stdin.read()
    except Exception:  # noqa: BLE001
        return 0
    if not raw.strip():
        return 0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return 0

    event_name = payload.get("hook_event_name")
    try:
        if event_name == "PreToolUse":
            return _handle_pretooluse(payload)
        if event_name == "Notification":
            return _handle_notification(payload)
        if event_name == "Stop":
            return _handle_stop(payload)
    except Exception as e:  # noqa: BLE001
        log.warning("hook: %s handler failed: %s", event_name, e)
        try:
            StateStore().log_error("hook-claude-code",
                                   f"{event_name} failed",
                                   extras={"detail": str(e)})
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
