# Channels — design note (music + audiobook, concurrent)

Status: **proposal**. Captures the design discussed 2026-06-03. Nothing
here is built yet; it generalises the existing single-music-sink model in
`packages/core/src/agent_media_core/`.

## Problem

Today there is one music sink (Mopidy/MPD on p8ar) with one queue. The
`content_type` flag on `music_play` already picks the right *interruption*
behaviour — `music` ducks, `audiobook`/`podcast` pause-and-resume (see
`route/policy.py`). What it does **not** give us is independent *state*:

- An audiobook is consumed across many sessions and needs a durable
  **resume position**. Music never needs this.
- One shared Mopidy queue means "play some music" clobbers the book's
  place, and vice versa.
- The desired usage is to **bounce between** a book and music, and
  sometimes hear **both at once** (book in front, music as a quiet bed).
- Whether the music bed should **duck or pause** under the book depends on
  the music — instrumental can duck under narration, but lyrics are
  distracting and should pause. This needs to be **switchable at runtime**,
  not baked per content-type.
- Want to **manage audiobook playlists** (a book = an ordered list of
  parts/chapters with a remembered position within the list).

"You can only hear one thing" is false here: with two players mixed, you
can hear two. So a channel is **not** a second simultaneous-exclusive
thing — it is a *saved listening context that owns a share of the output*.

## Model

A **channel** is an independent player with its own queue, position
persistence, content-type default, transport profile, and a mix level on
the shared output. Two built-ins to start: `music` and `book`. Extensible
to `podcast`, `ambient`.

```
[music player: Mopidy] ──┐
                         ├─→ combined sink → Snapcast → rooms
[book player:  mpv]    ──┘
```

This fits the existing stack: **PipeWire already does per-application
volume natively**, so the "mixer" is just two streams into a combined sink
and per-stream volume gives us mix + ducking for free. Use **mpv** for the
book channel rather than a second Mopidy — mpv gives clean playback-speed,
sleep-timer and skip-±30s (book-shaped transport Mopidy/MPD does not).

### Focus is a level, not exclusive ownership

The concept that makes "both at once" *and* "bounce between" both work
without the two channels fighting:

| Action                | book channel        | music channel               |
|-----------------------|---------------------|-----------------------------|
| `focus book`          | full                | → bed level (duck) OR pause |
| `focus music`         | pause + rewind      | full                        |
| speech interrupt      | pause + rewind      | duck                        |

`focus book` defaults the music channel to **duck-to-bed** (true
simultaneous), because the stated want is both-at-once. But that single
cell is **runtime-switchable per the music that's playing**:

- `book bed duck` — instrumental: keep music quiet underneath
- `book bed pause` — lyrics: pause music entirely while the book is in front

This is a per-channel *concurrency policy* attribute, distinct from the
per-content-type *interruption* policy that already exists for speech.

## Changes to existing code

### state (`state/store.py`)

- `now_playing` PRIMARY KEY moves from `sink` → `channel` (a channel maps
  to a sink, but two channels can be live at once). Today's single row
  becomes one row per active channel.
- New durable **resume positions**, keyed by URI, surviving channel
  switches and restarts:
  ```sql
  CREATE TABLE IF NOT EXISTS resume_pos (
      uri        TEXT PRIMARY KEY,
      pos_ms     INTEGER NOT NULL,
      updated_at REAL NOT NULL
  );
  ```
  (`now_playing.pause_pos_ms` is transient interruption state; this is the
  long-lived "where was I in this book" bookmark.) The store header already
  notes "Queue persistence + replay come later" — this is that.

### policy (`route/policy.py`)

- Add a per-channel **ConcurrencyPolicy** {`bed_strategy: duck|pause`,
  `bed_level: int`} separate from the existing speech `InterruptionPolicy`.
  Default `music` channel: `bed_strategy=duck, bed_level=10` (reuse the
  existing `duck_level`).
- Runtime override via a verb (below); persisted on the channel row so it
  sticks until changed.

### route coordinator

- Generalise from "drive the one Mopidy sink" to "drive N channels."
  Foreground/background transitions apply the ConcurrencyPolicy; speech
  interruption still applies the per-content-type InterruptionPolicy across
  all live channels (book pauses, music ducks) — that logic already exists,
  it just fans out to multiple channels now.

## Verb / MCP surface

Channel-scoped verbs. `music`/`book` are the two channels; the existing
flat `music_*` tools stay as aliases for the `music` channel so nothing
breaks.

```
book play <uri|playlist>        # defaults content_type=audiobook + resume-by-uri
book resume                     # resume last book at its bookmark
book speed 1.5
book skip -30 | +30
book sleep 30m
book bed duck | pause           # how music behaves when book is in front
focus book | music              # bring a channel to the front
now                             # both channels' state, side by side

# playlists
book playlist new <name>
book playlist add <name> <uri>...
book playlist play <name>       # resumes at the list's remembered position
book playlist ls [name]
```

### Playlists

A book/playlist is an ordered list of part URIs plus a remembered
**(index, pos_ms)** so `playlist play` resumes at the exact part and
offset. Store:

```sql
CREATE TABLE IF NOT EXISTS playlists (
    name       TEXT PRIMARY KEY,
    channel    TEXT NOT NULL,          -- 'book'
    cur_index  INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS playlist_items (
    name  TEXT NOT NULL,
    pos   INTEGER NOT NULL,            -- order in the list
    uri   TEXT NOT NULL,
    title TEXT,
    PRIMARY KEY (name, pos)
);
```

Per-URI `resume_pos` already gives within-part resume; `playlists.cur_index`
gives which part. Advancing to the next part is just `cur_index += 1`.

## Implementation phases

1. **book channel as a second player** — mpv via the existing `_mpv_ipc.py`
   helper, into its own PipeWire stream; `now_playing` keyed by channel;
   `book play/resume/skip/speed`. Resume-by-URI bookmarks. (Delivers most of
   the value — independent state + resume.)
2. **concurrency policy** — `focus`, `book bed duck|pause`, bed-level mix via
   per-stream PipeWire volume. (Delivers both-at-once.)
3. **playlists** — the two tables + `book playlist *` verbs.
4. **MCP/CLI wiring + voice-command phrasing** — channel-scoped verbs,
   `music_*` aliases retained.

## Decisions (locked 2026-06-03)

1. **Combined sink**: **explicit** PipeWire combined/null sink that both
   players feed and Snapcast reads — cleaner for routing and lets us read
   per-stream volumes back for bed mixing. (Stood up in phase 2; phase 1's
   book player just uses `--ao=pulse` with a known client name so the
   stream is already identifiable.)
2. **Book player host**: **mel** — mpv local, simplest, keeps the book near
   the agent. Its own broker (`sink-book.sock`), separate from the
   sink-speech broker.
3. **`book bed pause`**: a real **pause** of the music channel (matches the
   lyrics intuition), resumed on `focus music`. Phase 2.

## Phase 1 — built 2026-06-03

Delivered: book channel as a second mpv broker on mel, independent state,
resume-by-URI bookmarks, and — added into scope because an audiobook is
useless if it keeps talking over Claude — **book pauses for speech** (with
a lead-in rewind), wired through the existing coordinator.

- `sinks/book.py` — `SinkBook`: lazy-spawned mpv broker, `play`/`pause`/
  `resume`/`stop`/`skip`/`set_speed`/`position`/`active`. `normalize_uri`
  strips a leading `yt:` so the same URIs as `music_play` work. Probe/
  control methods never spawn, so the coordinator can ask "is a book
  playing?" for free.
- `state/store.py` — `resume_pos` table (URI → ms bookmark) + `set/
  get_resume_pos`, and a `book_last_uri` meta pointer for bare `book
  resume`. SCHEMA_VERSION → 2.
- `route/coordinator.py` — `before_speech` pauses an active book and
  `after_speech` resumes it backed up by the audiobook lead-in. In-memory
  per-clip, like the MPRIS pauses.
- `mcp_server.py` — `book_play/resume/pause/stop/skip/speed/now_playing`.
  Bookmarks are saved on pause/stop/switch. The flat `music_*` tools are
  untouched.

Deferred to later phases: periodic bookmark ticker (today bookmarks save on
pause/stop/switch, not continuously); `book sleep` timer; the focus/bed
concurrency policy and explicit combined sink (phase 2); playlists
(phase 3); CLI + voice phrasing (phase 4).

## Phase 2 — built 2026-06-03

Delivered: focus/bed concurrency (with the runtime duck-vs-pause switch) and
book→rooms routing. The "explicit combined sink" decision turned out to be
already satisfied by the existing `am-music` PipeWire sink — no new sink
needed (see routing note below).

- `route/concurrency.py` — `ConcurrencyPolicy` (focus + bed) + `apply_focus`.
  `focus book` → music to a quiet bed (duck, default level 12, env
  `MEDIA_BED_LEVEL`) or paused, per `book_bed`; book to full + resume.
  `focus music` → book pauses (bookmark saved), music back to baseline.
- `route/coordinator.py` — respects the arrangement so speech doesn't fight
  it: with focus=book/duck, post-speech music restores to the *bed* level,
  not baseline; with focus=book/pause, music is left paused entirely (the
  coordinator skips its duck-and-resume). Inert until `focus` is set, so the
  existing speech path is unchanged by default.
- `state/store.py` — `focus_channel` + `book_bed_strategy` in meta.
- `mcp_server.py` — `focus`, `book_bed`, `channels_status`.

### Routing finding (the audio graph)

The topology (from `~/.config/agent-media.env` + the live graph). **Updated
2026-06-03** when whole-house music moved onto mel:

- mel exposes two PipeWire sinks, `am` and `am-music`, each captured into
  its own Snapcast stream by a `parec→fifo` service.
- The book — mel-side mpv audio, exactly like speech — rides the **`am`
  stream** to reach the rooms (`MEDIA_SPEECH_DEFAULT_TARGET=rooms`,
  `MEDIA_ROOMS_SINK=am`). Its `rooms` target maps to `MEDIA_BOOK_ROOMS_SINK`,
  falling back to `MEDIA_ROOMS_SINK` (`am`). Speech still pauses the book via
  the coordinator, so book + speech time-share `am` cleanly.
- **Music now also rides a room stream**: Mopidy runs on **mel** (systemd
  `mopidy.service`, `MEDIA_MPD_HOST=127.0.0.1`) → the **`am-music`** sink →
  am-music Snapcast stream. The room device (p8ar) runs a **second
  snapclient** (`--hostID p8ar-music`) subscribed to `am-music`, alongside its
  existing `am` snapclient.

So "both at once" mixes **at the room device, across two snapclients** —
`am` (speech/book) + `am-music` (music) — which Android's audio layer sums.
This is the real "combined sink": two independent Snapcast streams summed at
the endpoint, with each player's own volume giving the bed mix. The book's
focus/bed ducking works because the coordinator sets mel's Mopidy MPD volume,
and that Mopidy *is* the am-music source the rooms hear.

(History: music used to be Mopidy **on p8ar**, phone-local, so an earlier
draft of this note said the book had to ride `am` because nothing played
`am-music`. Now music is on mel's `am-music` and a room snapclient plays it —
see the `project-agent-media-music-on-mel` memory.)

Default book target is `rooms` (`MEDIA_BOOK_DEFAULT_TARGET=rooms` in the env
file); set it to `local` to keep books on mel's own output. Music→rooms
**validated by ear 2026-06-03**; book→rooms uses the identical proven
speech-to-rooms `am` path.
