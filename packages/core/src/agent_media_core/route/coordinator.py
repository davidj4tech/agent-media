"""Speech ↔ music interruption coordinator.

When sink-speech is about to play a clip, the coordinator looks at what
sink-music is doing and applies the right interruption strategy (duck
or pause-and-resume) for the current content type. When speech ends, it
restores.

Replaces the polling-based `aar-mopidy-duck` daemon — event-driven,
content-type aware, observable via the state store.
"""

from __future__ import annotations

import logging
import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from .. import snapcast
from ..sinks.book import SinkBook
from ..sinks.music import SinkMusic
from ..sinks.music_router import SinkMusicRouter
from ..sinks import speech as _speech
from ..state import StateStore
from ..types import ContentType, Target
from . import _android, _mpris
from .concurrency import FOCUS_BOOK, bed_level, resolve
from .policy import (
    DEFAULT_POLICY,
    InterruptionPolicy,
    InterruptionStrategy,
    coerce_content_type,
    detect_content_type,
    policy_for,
)


def _env_duck_level() -> Optional[int]:
    """User override for the duck level. MEDIA_DUCK_VOLUME wins;
    legacy AAR_MOPIDY_DUCK_VOLUME falls through. Returns None when
    unset so the per-content-type policy default applies.
    """
    for var in ("MEDIA_DUCK_VOLUME", "AAR_MOPIDY_DUCK_VOLUME"):
        v = os.environ.get(var)
        if v:
            try:
                return max(0, min(100, int(v)))
            except ValueError:
                continue
    return None


def _rooms_duck_stream() -> Optional[str]:
    """Snapcast stream whose client volumes get ducked under speech, so the duck
    reaches the rooms regardless of which player feeds the stream (Mopidy on any
    host, the phone's local mpv, …) — unlike the Mopidy ``setvol`` duck below,
    which only lands when *this* coordinator's Mopidy is the one playing.

    Set ``MEDIA_DUCK_ROOMS_STREAM`` to the music stream id (e.g. ``am-music``) on
    the rooms hub to enable. Unset (default) disables this path entirely, so it's
    behaviour-preserving for hosts that aren't the rooms snapserver.
    """
    s = (os.environ.get("MEDIA_DUCK_ROOMS_STREAM") or "").strip()
    return s or None


def _remote_resume_settle_s() -> float:
    """Seconds to wait before resuming a remote player (Android via SSH).

    When mpv reports idle, the speech audio is still draining on the remote
    end (Snapcast buffer) and the speech stream is the phone's active media
    session — so a `dispatch play` sent immediately is routed to it (or
    dropped) instead of resuming music, and the resume only lands on the
    next pause/play cycle. Waiting for the drain fixes that.

    Tunable via MEDIA_REMOTE_RESUME_SETTLE_MS; defaults to the Snapcast
    latency (MEDIA_SNAPCAST_LATENCY_MS, default 500) plus a 400ms margin.
    """
    env = os.environ.get("MEDIA_REMOTE_RESUME_SETTLE_MS")
    if env is not None:
        try:
            ms = int(env)
        except ValueError:
            ms = 900
    else:
        try:
            base = int(os.environ.get("MEDIA_SNAPCAST_LATENCY_MS", "500"))
        except ValueError:
            base = 500
        ms = base + 400
    return max(0.0, ms / 1000.0)


log = logging.getLogger(__name__)


class Coordinator:
    """Mediates between sink-speech and sink-music.

    Stateless across instances — pass the same StateStore in if you want
    cross-process observability.
    """

    def __init__(self, *, music: Optional[SinkMusic] = None,
                 state: Optional[StateStore] = None,
                 book: Optional[SinkBook] = None,
                 music_target: Target = Target(name="local"),
                 book_target: Target = Target(name="local")) -> None:
        # The router forwards control to whichever backend is live (Mopidy or
        # the phone's local mpv) so speech ducks phone-local playout too. With
        # no phone backend configured it short-circuits to plain Mopidy, so this
        # is behaviour-preserving for the rooms/desktop case.
        self.music = music or SinkMusicRouter()
        self.state = state or StateStore()
        # Constructing SinkBook is cheap and never spawns mpv; probes below
        # are spawn-free, so an unused book channel costs nothing here.
        self.book = book if book is not None else SinkBook()
        self.music_target = music_target
        if book_target.name in ("", "local"):
            book_target = Target(name=(
                os.environ.get("MEDIA_BOOK_DEFAULT_TARGET")
                or os.environ.get("MEDIA_SPEECH_DEFAULT_TARGET")
                or book_target.name
                or "local"))
        self.book_target = book_target
        self._mpris_paused: list[str] = []
        self._mpris_remote_paused: dict[str, list[str]] = {}
        self._remote_pause_done: Optional[threading.Event] = None
        # Hosts where we sent a media-button play-pause that we need to undo
        # after speech finishes (Android phones via SSH).
        self._android_paused: list[str] = []
        # Whether we paused the book channel for the current clip.
        self._book_paused = False
        # A duck this coordinator decided on but has not applied yet — see
        # before_speech(defer_music=True) and duck_music_now(). The lock makes
        # the hand-off single-shot: two callers race to apply it (the far
        # side's "about to play" line and the grace timer behind it) and
        # exactly one may win.
        self._deferred_music: Optional[str] = None
        self._defer_lock = threading.Lock()
        # Serialises the speaking-flag writes so a clear can never overtake the
        # set it belongs to, and keeps both off the speech path's thread.
        self._flag_writer = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="speaking-flag")

    # ---- "this loss is ours" -------------------------------------------

    def _speaking(self, on: bool) -> None:
        """Raise or lower the in-band flag that says a response is in flight.

        Read by the companion app on the phone, which holds audio focus for the
        music mpv: without it the app cannot tell our own speech from a
        navigation prompt, and ducks over the duck below. See
        `sinks.speech.set_speaking` for why watching playback cannot answer it.

        Fire-and-forget on a single worker: the write is a round trip over the
        phone bridge, and speech must never wait on a diagnostic.
        """
        speech_target = Target(name=(
            os.environ.get("MEDIA_SPEECH_DEFAULT_TARGET") or "local"))
        try:
            self._flag_writer.submit(_speech.set_speaking, on, speech_target)
        except RuntimeError:  # pragma: no cover — executor shut down
            pass

    def _title(self, text: str) -> None:
        """Tell the speech broker what to call this reply.

        Same machinery and same discipline as the flag above: one worker,
        fire-and-forget, never on the path of the audio. What it carries is the
        popup's own title — the conversation the reply belongs to — so the
        phone's speech card and the car display say what the listener is
        already reading somewhere else.
        """
        if not (text or "").strip():
            return
        speech_target = Target(name=(
            os.environ.get("MEDIA_SPEECH_DEFAULT_TARGET") or "local"))
        try:
            self._flag_writer.submit(_speech.set_media_title, text, speech_target)
        except RuntimeError:  # pragma: no cover — executor shut down
            pass

    def _reply_text(self, text: str) -> None:
        """Carry the reply's own words to the broker, beside its title.

        The title is the conversation, which is what a display with one line
        should show and what tells two replies apart. The words are what the
        phone's own list shows. Neither is the other's substitute, so both are
        sent and the surface picks.
        """
        if not (text or "").strip():
            return
        speech_target = Target(name=(
            os.environ.get("MEDIA_SPEECH_DEFAULT_TARGET") or "local"))
        try:
            self._flag_writer.submit(_speech.set_reply_text, text, speech_target)
        except RuntimeError:  # pragma: no cover — executor shut down
            pass

    def _priority(self, priority: str) -> None:
        """Carry this reply's priority to the broker, for the phone to read.

        Same worker and same discipline as the two flags above. It is written
        with the speaking flag rather than at play time because the decision it
        feeds — hold this reply, ask about it, or interrupt with it — has to be
        made before the first word is audible.
        """
        speech_target = Target(name=(
            os.environ.get("MEDIA_SPEECH_DEFAULT_TARGET") or "local"))
        try:
            self._flag_writer.submit(_speech.set_priority, priority, speech_target)
        except RuntimeError:  # pragma: no cover — executor shut down
            pass

    # ---- public API used by sink-speech --------------------------------

    def pre_pause_remote(self) -> None:
        """Start remote MPRIS and Android media detect-and-pause in a background thread.

        Call this before render_text so the ~4.8s SSH cold-connect overlaps
        with rendering.  before_speech() waits up to 6s for it to finish
        before playing audio.
        """
        # Raise the flag as early as anything happens: rendering and relaying a
        # response opens the clip on the far mpv well before it is audible, and
        # the focus loss that causes lands then — 37 s ahead of the first clip
        # on 2026-08-14. before_speech raises it again; both are idempotent.
        self._speaking(True)

        mpris_hosts = _mpris.ssh_hosts() if _mpris.enabled() else []
        android_hosts = _android.pause_hosts()
        if not mpris_hosts and not android_hosts:
            return
        self._remote_pause_done = threading.Event()

        def _work() -> None:
            for host in mpris_hosts:
                try:
                    remote = _mpris.remote_playing_players(host)
                    self._mpris_remote_paused[host] = remote
                    _mpris.pause_remote(host, remote)
                except Exception:  # noqa: BLE001
                    pass
            for host in android_hosts:
                try:
                    if _android.pause_for_speech(host):
                        self._android_paused.append(host)
                except Exception:  # noqa: BLE001
                    pass
            self._remote_pause_done.set()

        threading.Thread(target=_work, daemon=True).start()

    # ---- source-agnostic rooms (Snapcast) duck ------------------------

    def _rooms_duck(self, level: int) -> None:
        """Duck the rooms music stream by lowering each audible Snapcast
        client's volume to ``level`` for the clip. Source-agnostic: works no
        matter which player feeds ``MEDIA_DUCK_ROOMS_STREAM``. Best-effort —
        a snapserver hiccup must never break speech. No-op when disabled.
        """
        stream = _rooms_duck_stream()
        if not stream:
            return
        try:
            marker = self.state.get_rooms_duck()
            if marker and marker.get("vols"):
                # A duck is already in force (mid-response re-duck, or a strand
                # from a killed process): keep the original pre-duck volumes as
                # the restore baseline rather than re-capturing ducked ones.
                vols = {str(k): int(v) for k, v in marker["vols"].items()}
            else:
                vols = {c["id"]: int(c["percent"])
                        for c in snapcast.clients_on_stream(stream)}
            if not vols:
                return
            self.state.set_rooms_duck({"level": int(level), "vols": vols})
            for cid, prior in vols.items():
                if prior > level:
                    snapcast.set_client_volume(cid, level)
        except Exception as e:  # noqa: BLE001
            self._log_err("rooms: snapcast duck failed", str(e))

    def _rooms_unduck(self, clear: bool = True) -> None:
        """Restore client volumes ducked by :meth:`_rooms_duck`. With
        ``clear=False`` the marker is kept so a later restore still runs (used
        by the mid-response mute release)."""
        if not _rooms_duck_stream():
            return
        try:
            marker = self.state.get_rooms_duck()
        except Exception:  # noqa: BLE001
            marker = None
        if not marker:
            return
        try:
            for cid, prior in (marker.get("vols") or {}).items():
                snapcast.set_client_volume(str(cid), int(prior))
        except Exception as e:  # noqa: BLE001
            self._log_err("rooms: snapcast unduck failed", str(e))
        finally:
            if clear:
                try:
                    self.state.set_rooms_duck(None)
                except Exception:  # noqa: BLE001
                    pass

    def _rooms_reduck(self) -> None:
        """Re-apply the rooms duck after a mid-response mute is lifted, reusing
        the level and pre-duck volumes the marker recorded."""
        if not _rooms_duck_stream():
            return
        try:
            marker = self.state.get_rooms_duck()
        except Exception:  # noqa: BLE001
            marker = None
        if not marker:
            return
        level = int(marker.get("level") or 10)
        try:
            for cid, prior in (marker.get("vols") or {}).items():
                if int(prior) > level:
                    snapcast.set_client_volume(str(cid), level)
        except Exception as e:  # noqa: BLE001
            self._log_err("rooms: snapcast reduck failed", str(e))

    def _probe_book_active(self) -> bool:
        """Is the book channel playing? Asked concurrently (see before_speech)."""
        try:
            return bool(self.book.active(self.book_target))
        except Exception:  # noqa: BLE001 — surfaced by the caller
            return False

    def _probe_music_uri(self):
        """What the music channel is playing. Asked concurrently."""
        return self.music.now_playing_uri(self.music_target)

    def before_speech(self, title: str = "", priority: str = "",
                      defer_music: bool = False, text: str = "") -> None:
        """Apply interruption for whatever sink-music is currently
        playing. Records baseline volume + position so after_speech can
        restore.

        ``defer_music`` decides everything about the music duck except when it
        lands, and hands that to the caller through :meth:`duck_music_now`. It
        exists for a lane where "we are about to speak" and "sound is coming
        out" are a long way apart: the phone renders its own audio, so this
        call returns as soon as the text is handed over and the first word is
        ten seconds later. Ducking there is a hole in the music that starts
        before anything fills it. Everything else — the flag, the book, the
        remote pauses — is still applied now, because those are about the
        commitment rather than the sound.

        ``title`` names the reply for anything that displays one — the phone's
        speech card, the car — and is the same string the popup shows. Optional
        because a caller with nothing to call the reply is not a caller with a
        problem: the display keeps its own fallback.

        ``text`` is the reply itself, for a surface with room to show what was
        said rather than which conversation said it. Both travel: see
        `_reply_text`.

        ``priority`` says how much interruption this reply is worth, and the
        phone chooses between waiting for a gap, asking David with a card, and
        taking the room. Empty means normal, which is what an ordinary answer
        to a question is.
        """
        self._title(title)
        self._reply_text(text)
        self._priority(priority)
        # Source-agnostic rooms duck, applied first and independent of the
        # Mopidy now-playing gate below: lower the Snapcast music stream so the
        # duck lands even when the music is fed by a different player/host than
        # the one this coordinator can poll. No-op unless MEDIA_DUCK_ROOMS_STREAM
        # is set. Uses the same level the user/policy picks for music.
        # Start the two independent remote probes now, so they overlap each
        # other and the remote pause instead of running in series. Over the
        # phone bridge each is a ~2s round trip, and they ask unrelated
        # questions of unrelated players — "is a book playing" has never
        # depended on "what is the music". Sequencing them was habit, and on a
        # 450ms link habit cost four seconds per utterance.
        self._speaking(True)
        probes = ThreadPoolExecutor(max_workers=2)
        book_active = probes.submit(self._probe_book_active)
        music_uri = probes.submit(self._probe_music_uri)

        rooms_level = _env_duck_level()
        if rooms_level is None:
            rooms_level = policy_for(ContentType.MUSIC).duck_level
        self._rooms_duck(rooms_level)
        # Local MPRIS: pause browser/external players on this host.
        if _mpris.enabled():
            self._mpris_paused = _mpris.playing_players()
            _mpris.pause_players(self._mpris_paused)

        # Remote MPRIS + Android media-button: wait for pre_pause_remote()
        # background work to finish, or do it synchronously if it was never
        # started.
        mpris_hosts = _mpris.ssh_hosts() if _mpris.enabled() else []
        android_hosts = _android.pause_hosts()
        if mpris_hosts or android_hosts:
            if self._remote_pause_done is not None:
                self._remote_pause_done.wait(timeout=14)
                self._remote_pause_done = None
            else:
                for host in mpris_hosts:
                    try:
                        remote = _mpris.remote_playing_players(host)
                        self._mpris_remote_paused[host] = remote
                        _mpris.pause_remote(host, remote)
                    except Exception:  # noqa: BLE001
                        pass
                for host in android_hosts:
                    try:
                        if _android.pause_for_speech(host):
                            self._android_paused.append(host)
                    except Exception:  # noqa: BLE001
                        pass

        # Book channel (sink-book): a longform audiobook must *pause* for
        # speech — you can't half-hear narration. Independent of the Mopidy
        # music sink below, and handled first so it still pauses when no
        # music is playing. In-memory like the MPRIS pauses (before/after
        # wrap one clip in the same process). Spawn-free: active() is False
        # when no broker is up, so this is a no-op when the book is unused.
        try:
            if book_active.result(timeout=20):
                self.book.pause(self.book_target)
                self._book_paused = True
        except Exception as e:  # noqa: BLE001
            self._log_err("book: pause failed", str(e))

        # When a book is foregrounded with bed=pause, music is *already*
        # paused on purpose — don't duck-and-resume it for speech, or
        # after_speech would un-pause it and break the focus arrangement.
        concurrency = resolve(self.state)
        if concurrency.music_bedded_by_pause:
            return

        try:
            uri = music_uri.result(timeout=20)
        except Exception as e:  # noqa: BLE001
            self._log_err("music: now_playing_uri failed", str(e))
            return
        finally:
            probes.shutdown(wait=False)
        if not uri:
            return  # nothing to interrupt via Mopidy

        if defer_music:
            # Decided, not yet done. The probe above has already been paid for,
            # so applying it later costs one round trip rather than the whole
            # sequence again.
            with self._defer_lock:
                self._deferred_music = uri
            return
        self._interrupt_music(uri, concurrency)

    def duck_music_now(self) -> None:
        """Apply a duck that ``before_speech(defer_music=True)`` held back.

        Single-shot and safe to call from anywhere: the two callers are the far
        side saying it is about to play and the timer that does not trust it to,
        and whichever arrives first is the one that ducks. Calling it with
        nothing deferred — no music, a bedded book, a lane that never deferred —
        does nothing, which is what makes it safe to call unconditionally.
        """
        with self._defer_lock:
            uri, self._deferred_music = self._deferred_music, None
        if not uri:
            return
        self._interrupt_music(uri, resolve(self.state))

    def _interrupt_music(self, uri: str, concurrency) -> None:
        """Pause or duck the music that `uri` names, and record how to undo it."""
        content_type = self._content_type_for(uri)
        policy = policy_for(content_type)
        extras: dict = {}

        if policy.strategy == InterruptionStrategy.PAUSE:
            try:
                pos_ms = self.music.position(self.music_target)
            except Exception:
                pos_ms = None
            extras["pause_pos_ms"] = pos_ms
            extras["lead_in_ms"] = policy.lead_in_ms
            extras["strategy"] = "pause"
            try:
                self.music.pause(self.music_target)
            except Exception as e:  # noqa: BLE001
                self._log_err("music: pause failed", str(e))
                return
        else:
            env_override = _env_duck_level()
            level = env_override if env_override is not None else policy.duck_level
            extras["strategy"] = "duck"
            extras["duck_level"] = level
            # If a book is in front (bed=duck), music belongs at the bed
            # level after speech, not the normal baseline — otherwise each
            # clip pops the bedded music back up to full-ish.
            #
            # Otherwise restore what the user actually had: capture the live
            # backend's volume before ducking. The policy baseline is only the
            # fallback (unreadable volume, or a stranded duck left the current
            # volume at/below the duck level — restoring *that* would freeze
            # the music quiet).
            prior = self._music_duck_marker()
            if concurrency.focus == FOCUS_BOOK:
                baseline = bed_level()
            elif prior and prior.get("baseline") is not None:
                # A duck of ours is already in force — a mid-response re-duck,
                # or a strand left by a restore that never ran. Keep the
                # ORIGINAL pre-duck volume rather than re-capturing the ducked
                # one, exactly as _rooms_duck does. The music path lacked this
                # guard, and its absence is how 130 became 45 and then stuck.
                baseline = int(prior["baseline"])
            else:
                pre_duck = None
                try:
                    vol_fn = getattr(self.music, "current_volume", None)
                    pre_duck = vol_fn(self.music_target) if vol_fn else None
                except Exception:  # noqa: BLE001 — best-effort capture
                    pre_duck = None
                baseline = (pre_duck if pre_duck is not None and pre_duck > level
                            else self._fallback_baseline(policy.baseline_volume))
            extras["baseline_volume"] = baseline
            # Recorded before the duck, so a process killed between the two
            # still leaves the debt where the next clip can find it.
            try:
                self.state.set_music_duck({"level": int(level),
                                           "baseline": int(baseline),
                                           "target": self.music_target.name})
            except Exception:  # noqa: BLE001
                pass
            try:
                self.music.duck(self.music_target, level)
            except Exception as e:  # noqa: BLE001
                self._log_err("music: duck failed", str(e))
                return

        self.state.set_now_playing(
            sink="music",
            uri=uri,
            started_at=time.time(),
            content_type=content_type.value if content_type else None,
            target=self.music_target.name,
            extras={"interruption": extras},
        )

    def after_speech(self) -> None:
        """Restore from whatever before_speech did."""
        # Lower the flag first: from here on, a focus loss really is somebody
        # else's and the app should duck for it.
        self._speaking(False)
        # A duck decided but never applied — the utterance failed, or the far
        # side finished before anything asked for it. Dropping it here keeps it
        # from landing on the *next* reply's music, which would be a duck with
        # no speech behind it and nothing left to restore it.
        with self._defer_lock:
            self._deferred_music = None
        # Restore the source-agnostic rooms (Snapcast) duck first — it's
        # independent of the Mopidy interruption marker handled below (which is
        # absent whenever this coordinator's Mopidy wasn't the one playing).
        self._rooms_unduck()
        if self._mpris_paused:
            _mpris.resume_players(self._mpris_paused)
            self._mpris_paused = []
        for host, names in self._mpris_remote_paused.items():
            _mpris.resume_remote(host, names)
        self._mpris_remote_paused = {}
        if self._android_paused:
            # Let our speech audio drain on the phone first (see helper) so
            # `dispatch play` reaches the music session, not the speech one.
            time.sleep(_remote_resume_settle_s())
            for host in self._android_paused:
                try:
                    _android.resume(host)
                except Exception:  # noqa: BLE001
                    pass
            self._android_paused = []

        # Resume the book channel if we paused it, backed up by the audiobook
        # lead-in so the listener doesn't miss the word speech cut in on.
        if self._book_paused:
            try:
                lead_in_ms = policy_for(ContentType.AUDIOBOOK).lead_in_ms
                if lead_in_ms > 0:
                    self.book.skip(-lead_in_ms / 1000.0, self.book_target)
                self.book.resume(self.book_target)
            except Exception as e:  # noqa: BLE001
                self._log_err("book: resume failed", str(e))
            finally:
                self._book_paused = False

        # An in-force duck of ours is restored from its own marker, before and
        # independently of the now-playing row below. That row is shared, and
        # when something else clears or overwrites it between the duck and the
        # restore, the early return underneath used to leave the music at the
        # duck level for good — twice on the phone on 2026-08-14, once for two
        # hours. The debt is now its own record and is paid whatever happened to
        # the row.
        ducked = self._music_duck_marker()
        if ducked is not None:
            self._music_unduck(ducked)

        np = self.state.get_now_playing("music")
        if not np or not np.get("extras"):
            return
        interruption = (np["extras"] or {}).get("interruption") or {}
        strategy = interruption.get("strategy")

        try:
            if strategy == "duck" and ducked is not None:
                pass  # already restored, from the marker above
            elif strategy == "pause":
                # Back up by the lead-in window so the listener doesn't
                # miss the word they were on when speech cut in. Best
                # effort — if seek fails or position wasn't captured,
                # we just resume from where pause landed.
                pos_ms = interruption.get("pause_pos_ms")
                lead_in_ms = int(interruption.get("lead_in_ms") or 0)
                if pos_ms is not None and lead_in_ms > 0:
                    try:
                        self.music.seek_cur(self.music_target,
                                            max(0, int(pos_ms) - lead_in_ms))
                    except Exception:
                        pass
                self.music.resume(self.music_target)
            elif strategy == "duck":
                baseline = int(interruption.get("baseline_volume")
                               or self._fallback_baseline())
                self.music.unduck(self.music_target, restore=baseline)
        except Exception as e:  # noqa: BLE001
            self._log_err(f"music: restore ({strategy}) failed", str(e))
        finally:
            # Whether restore succeeded or not, clear the marker so a
            # stuck row doesn't poison the next clip.
            self.state.clear_now_playing("music")

    # ---- the music duck debt --------------------------------------------

    def _music_duck_marker(self) -> Optional[dict]:
        """The in-force music duck, or None. Best-effort: a state hiccup must
        never break speech."""
        try:
            return self.state.get_music_duck()
        except Exception:  # noqa: BLE001
            return None

    def _music_unduck(self, marker: dict) -> None:
        """Pay the duck debt in ``marker`` and clear it."""
        baseline = marker.get("baseline")
        restore = int(baseline) if baseline is not None else self._fallback_baseline()
        try:
            self.music.unduck(self.music_target, restore=restore)
        except Exception as e:  # noqa: BLE001
            self._log_err("music: unduck failed", str(e))
        finally:
            try:
                self.state.set_music_duck(None)
            except Exception:  # noqa: BLE001
                pass

    def _fallback_baseline(self, policy_baseline: int = 45) -> int:
        """Where to put the volume back when no clean pre-duck reading exists.

        The policy's 45 is a Mopidy-era number on a 0-100 dial. The phone's mpv
        runs `--volume=130` on a 0-170 one, so falling back to 45 there is not a
        safe default but an audible drop the listener has to undo by hand — and
        it is precisely what happened on 2026-08-14. Ask the live backend what
        normal means for it, and keep the policy number as the last resort.
        """
        fn = getattr(self.music, "nominal_volume", None)
        try:
            nominal = fn(self.music_target) if fn else None
        except Exception:  # noqa: BLE001
            nominal = None
        return int(nominal) if nominal else int(policy_baseline)

    # ---- mid-response mute toggling ------------------------------------

    def _duck_interruption(self) -> Optional[dict]:
        """The interruption marker before_speech stashed, but only when it
        ducked (not paused) music. None otherwise.

        Used by the mute-edge handlers below so they reuse the exact level
        and baseline before_speech chose, instead of recomputing.
        """
        np = self.state.get_now_playing("music")
        interruption = ((np or {}).get("extras") or {}).get("interruption") or {}
        return interruption if interruption.get("strategy") == "duck" else None

    def release_music_duck(self) -> None:
        """Temporarily un-duck music when speech goes silent mid-response
        (the user muted via the popup): muted speech needs no headroom.

        Deliberately leaves the interruption marker in place so after_speech
        still performs the authoritative restore — and only touches the duck,
        never the book/MPRIS/phone pauses, which would churn on every toggle.
        Pause-strategy music (audiobook/podcast) is left alone.
        """
        # Lift the rooms (Snapcast) duck too while speech is muted; keep the
        # marker so after_speech still owns the authoritative restore.
        self._rooms_unduck(clear=False)
        interruption = self._duck_interruption()
        if interruption is None:
            return
        baseline = int(interruption.get("baseline_volume")
                       or self._fallback_baseline())
        try:
            self.music.unduck(self.music_target, restore=baseline)
        except Exception as e:  # noqa: BLE001
            self._log_err("music: release duck (mute) failed", str(e))

    def reapply_music_duck(self) -> None:
        """Re-duck music when a mid-response mute is lifted and speech is
        audible again. Reads the same level before_speech recorded.
        """
        # Re-duck the rooms (Snapcast) stream too, reusing its own marker.
        self._rooms_reduck()
        interruption = self._duck_interruption()
        if interruption is None:
            return
        level = int(interruption.get("duck_level") or 15)
        try:
            self.music.duck(self.music_target, level)
        except Exception as e:  # noqa: BLE001
            self._log_err("music: reapply duck (mute) failed", str(e))

    # ---- helpers --------------------------------------------------------

    def _content_type_for(self, uri: str) -> ContentType:
        """Content type for what's playing on the music sink.

        Prefers the caller's queue-time intent (set by music_play, e.g.
        "this yt: URL is an audiobook") over re-sniffing the URI, since a
        YouTube/HTTP URL is otherwise indistinguishable from music and would
        be ducked rather than paused. Falls back to URI detection when no
        intent is recorded.

        The intent is trusted whenever present rather than matched against
        `uri`: Mopidy normalises a queued `yt:https://...` URL into a
        `yt:video:<id>` form by playback time, so an exact match would
        defeat the very case this exists for. The trade-off is that music
        started outside music_play (e.g. the /music skill talking straight
        to MPD) can leave a stale audiobook intent — that over-pauses music,
        which is benign and self-heals on the next music_play or music_stop.
        """
        try:
            intent = self.state.get_music_intent()
        except Exception:  # noqa: BLE001
            intent = None
        if intent:
            ct = coerce_content_type(intent.get("content_type"))
            if ct is not None:
                return ct
        return detect_content_type(uri)

    def _log_err(self, msg: str, detail: str) -> None:
        log.warning("%s: %s", msg, detail)
        try:
            self.state.log_error("coordinator", msg, extras={"detail": detail})
        except Exception:
            pass


__all__ = ["Coordinator", "InterruptionPolicy", "InterruptionStrategy",
           "DEFAULT_POLICY", "detect_content_type", "policy_for"]
