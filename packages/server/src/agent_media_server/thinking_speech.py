"""Optional, live read-out of exposed reasoning and public progress updates.

One server-owned watcher, independent of SSE subscribers. Old parts are a
baseline, never a playback backlog. Speech is disposable and uses core's
ordinary renderer/player, with cancellation checked between sentences.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import logging
from pathlib import Path
import threading
import time
import uuid

from agent_media_core import private, sealed, speak_priority
from agent_media_core.intake import toast
from agent_media_core.types import Event, Priority, Source

from . import auth, sessiond, sessions, transcript
from ._jsonmap import JsonMap

log = logging.getLogger(__name__)
POLL_S = 0.5
SETTLE_S = 0.5
_PREFS = JsonMap("thinking-speech.json")
_DEFAULT = JsonMap("thinking-speech-default.json")
_HARNESS = frozenset({"claude", "codex", "pi", "gemini", "grok", "opencode"})


def _supported(session: str) -> bool:
    harness, _ = transcript.transcript_of(session)
    return (harness or sessions.agent_of(session)) in _HARNESS


def _token(session: str) -> str | None:
    own = _PREFS.get(session)
    if own is False:
        return None
    if isinstance(own, str) and own:
        return own
    default = _DEFAULT.get("token")
    return default if default and _supported(session) else None


def _preferences() -> dict[str, str]:
    prefs = _PREFS.all()
    effective = {s: t for s, t in prefs.items() if isinstance(t, str) and t}
    default = _DEFAULT.get("token")
    if default:
        candidates = set(sessions.live_sessions())
        # Phone-started conversations have no pane and are absent from live_sessions.
        candidates.update(r["session"] for r in sessiond.records() if r.get("pid"))
        for session in candidates:
            if session not in prefs and _supported(session):
                effective[session] = default
    return effective


def _read(session: str) -> tuple[tuple, list[dict]]:
    harness, path = transcript.transcript_of(session)
    identity = (harness, path)
    if path:
        st = Path(path).stat()
        # Appending is normal; a new inode or shrinking file is a new baseline.
        identity += (st.st_dev, st.st_ino, st.st_size)
    got = transcript.messages(session, limit=30)
    return identity, got[0] if got else []


def _parts(messages: list[dict]) -> dict[tuple[str, int], str]:
    return {(m["id"], i): str(p.get("text") or "").strip()
            for m in messages if m.get("role") == "assistant"
            for i, p in enumerate(m.get("parts") or [])
            if ((p.get("type") == "reasoning" and not p.get("redacted"))
                or (p.get("type") == "text" and p.get("channel") == "commentary"))
            and str(p.get("text") or "").strip()}


def _eligible(session: str) -> bool:
    if sealed.is_sealed(session):
        return False
    level = speak_priority.level_of(session)
    return level != "quiet" and (level in speak_priority.SPEAKS
                                 or not toast.should_hold(session))


def _working(session: str) -> bool:
    # A transcript's `turn.running` means an outstanding tool, not whether
    # Codex/pi are still thinking. Use the same authority as the app/reaper.
    harness, _ = transcript.transcript_of(session)
    if harness == "codex":
        from . import codex_control

        working = codex_control.working(session)
        if working is not None:
            return working
    pane = sessions.live_sessions().get(session, "")
    return sessions.activity_of(session, pane).get("state") == "working"


@dataclass
class Cursor:
    token: str
    identity: tuple
    seen: set = field(default_factory=set)
    pending: dict = field(default_factory=dict)


class Watcher:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.cursors: dict[str, Cursor] = {}
        self.busy = False
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None

    def baseline(self, session: str, token: str) -> None:
        try:
            identity, messages = _read(session)
        except OSError:
            identity, messages = (), []
        self.cursors[session] = Cursor(token, identity, set(_parts(messages)))

    def allowed(self, session: str, token: str, message: str) -> bool:
        if self.stop.is_set() or _token(session) != token or not _eligible(session):
            return False
        try:
            _, messages = _read(session)
        except OSError:
            return False
        return bool(messages and messages[-1].get("role") == "assistant"
                    and messages[-1].get("id") == message and _working(session))

    def speak(self, session: str, token: str, key: tuple[str, int], text: str) -> None:
        try:
            if not self.allowed(session, token, key[0]):
                log.info("thinking readout discarded before submission for %s", session[:8])
                return
            log.info("thinking readout submitting %s part %s", session[:8], key[1])
            from agent_media_core.intake.submit import submit_event

            submit_event(Event(text=text, source=Source.WATCHER, priority=Priority.LOW,
                               metadata={"session": session, "thinking": True,
                                         "pane": sessions.live_sessions().get(session, ""),
                                         "private": private.is_private(session),
                                         "thinking_message": key[0], "thinking_part": key[1]}),
                         playback_allowed=lambda: self.allowed(session, token, key[0]))
            log.info("thinking readout finished for %s", session[:8])
        except Exception:
            log.exception("thinking speech failed for %s", session[:8])
        finally:
            with self.lock:
                self.busy = False

    def tick(self) -> None:
        with self.lock:
            prefs = _preferences()
            self.cursors = {s: c for s, c in self.cursors.items() if s in prefs}
            for session, token in prefs.items():
                try:
                    self._session(session, token)
                except FileNotFoundError:
                    self.cursors.pop(session, None)
                except Exception:
                    log.exception("thinking watcher failed for %s", session[:8])

    def _session(self, session: str, token: str) -> None:
        if session not in self.cursors or self.cursors[session].token != token:
            self.baseline(session, token)
            return
        cursor = self.cursors[session]
        identity, messages = _read(session)
        parts = _parts(messages)
        old = cursor.identity
        if (identity[:-1] != old[:-1] or
                (len(identity) == 5 and len(old) == 5 and identity[-1] < old[-1])):
            self.baseline(session, token)
            return
        cursor.identity = identity
        cursor.seen.intersection_update(parts)
        cursor.pending = {k: v for k, v in cursor.pending.items() if k in parts}
        latest = messages[-1].get("id") if messages and messages[-1].get("role") == "assistant" else None
        now = time.monotonic()
        for key, text in parts.items():
            if key in cursor.seen:
                continue
            pending = cursor.pending.get(key)
            if pending is None or pending[0] != text:
                cursor.pending[key] = (text, now)
                continue
            if now - pending[1] < SETTLE_S:
                continue
            # Consume even when nobody can hear it: never collect a backlog.
            cursor.seen.add(key)
            cursor.pending.pop(key, None)
            if key[0] != latest or self.busy or not _eligible(session) or not _working(session):
                continue
            self.busy = True
            self.launch(session, token, key, text)

    def launch(self, session: str, token: str, key: tuple[str, int], text: str) -> None:
        threading.Thread(target=self.speak, args=(session, token, key, text),
                         name="thinking-speech-play", daemon=True).start()

    def start(self) -> None:
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            self.stop.clear()
            def run() -> None:
                while not self.stop.is_set():
                    try:
                        self.tick()
                    except Exception:
                        log.exception("thinking watcher tick failed")
                    self.stop.wait(POLL_S)
            self.thread = threading.Thread(target=run, name="thinking-speech", daemon=True)
            self.thread.start()


_WATCHER = Watcher()


def start() -> None:
    _WATCHER.start()


def default_setting(bearer: str, *, body: dict | None = None) -> tuple[bool, dict]:
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    if body is not None:
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            return False, {"error": "enabled must be true or false", "status": 400}
        try:
            with _WATCHER.lock:
                if enabled and not _DEFAULT.get("token"):
                    _DEFAULT.put("token", uuid.uuid4().hex)
                elif not enabled:
                    _DEFAULT.drop("token")
        except OSError:
            return False, {"error": "could not save default thinking speech", "status": 500}
    return True, {"enabled": bool(_DEFAULT.get("token"))}


def setting(session: str, bearer: str, *, body: dict | None = None) -> tuple[bool, dict]:
    session = session.strip()
    if not sessions._SESSION.fullmatch(session):
        return False, {"error": "not a session id", "status": 400}
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    if not (sessions.live_sessions().get(session) or sessions.session_exists(session)
            or sessions._folder_for_session(session)):
        return False, {"error": "no such session", "status": 404}
    supported = _supported(session)
    if body is not None:
        enabled = body.get("enabled")
        if "enabled" not in body or (enabled is not None and not isinstance(enabled, bool)):
            return False, {"error": "enabled must be true, false or null", "status": 400}
        if enabled and not supported:
            return False, {"error": "this agent has no readable thinking here", "status": 409}
        try:
            with _WATCHER.lock:
                if enabled and not _PREFS.get(session):
                    token = uuid.uuid4().hex
                    _WATCHER.baseline(session, token)
                    _PREFS.put(session, token)
                elif enabled is False:
                    _PREFS.put(session, False)
                    _WATCHER.cursors.pop(session, None)
                elif enabled is None:
                    _PREFS.drop(session)
                    _WATCHER.cursors.pop(session, None)
        except OSError:
            return False, {"error": "could not save thinking speech", "status": 500}
    return True, {"session": session, "enabled": bool(_token(session)) and supported,
                  "supported": supported, "own": _PREFS.get(session) is not None,
                  "default": bool(_DEFAULT.get("token"))}
