# astrotunes

Pick music that matches the moment: your transit chart, time of day,
mood, what you're doing, and the weather. Hands the pick to a backend
(`media-mcp` locally, Mopidy on p8ar, or just stdout).

## Status

Prototype. The recommendation logic lives in a Claude Code skill at
`~/.claude/skills/music-transit/SKILL.md` — this CLI just produces the
*inputs* (current transits, moon phase, weather, time-of-day) as JSON
and Claude does the synthesis.

## Inputs

- **Natal chart** — loaded from Sacred Brain (`sacred-search`) with
  fallback prompt
- **Current transits** — computed via `kerykeion` (Swiss Ephemeris)
- **Human Design bodygraph** — computed from the same birth data (see below)
- **Time of day** — current Melbourne local time
- **Mood + activity** — prompted at invocation
- **Weather** — Open-Meteo, no API key (Melbourne lat/lon hardcoded)

## Human Design

The full bodygraph is computed from birth data, not hand-entered. Human
Design reads two charts — the **Personality** (the birth moment) and the
**Design** (the moment the Sun was 88° of arc earlier, ~88 days before
birth, found by bisection) — and maps each of 13 bodies' ecliptic
longitudes onto the 64-gate I Ching wheel (Gate 41 at 2°00′ Aquarius =
302.0°, gates of 5.625°, lines of 0.9375°). A gate is *activated* if any
of the 26 placements falls in it; a channel is *defined* when both its
gates are activated; a center is *defined* when a channel touches it.
From the defined centers the module derives **Type, Strategy, Authority,
Profile, Definition, the incarnation cross, and signature / not-self**.

`astrotunes context` emits this in two places:

- `natal.human_design` — the static chart (type, authority, profile,
  defined / **undefined** centers, channels, cross). The *undefined*
  centers are where the environment is amplified — for music, the levers
  that hit hardest.
- `transits.human_design` — today's transiting gate activations (each
  with an I Ching keynote) plus `channels_activated`: channels today's
  transits temporarily complete in the chart.

Reference tables (`humandesign/data.py`) are verified against Jovian
Archive (the official Ra Uru Hu lineage) and corroborating
gate-by-degree sources. The music interpretation — which open center
maps to which musical lever — lives in the `music-transit` skill.

## Qualities (the translation layer)

`astrotunes context` also emits `qualities` — a deterministic, tweakable
*brief* that turns the computed facts (transits, Human Design, weather,
time, moon) into six musical axes, each `0.0–1.0` with a `label`, the
`reasons` that moved it, and an `amplified` flag:

`tempo`, `energy`, `warmth`, `brightness`, `lyric_density`, `texture`.

Plus `amplified_axes` (open-HD-center dimensions the listener feels most
acutely), `posture` (Type's listening stance), `novelty`, and `themes`
(today's HD channel keynotes).

This is the explicit half of a deliberate split: the *translation* (facts
→ tags) is transparent and lives in `qualities.py`'s CONFIG block of
nudges — tweak a nudge and every future brief changes. The *selection*
(tags → actual tracks) stays downstream judgment in the `music-transit`
skill, where the live mood/activity answer can override the brief. A
dense natal chart yields many aspects, so the aspect layer is capped
(`ASPECT_CAP`) to colour the brief without swamping time/weather/mood.

## Sources (`sources/`)

A pluggable registry of information libraries, emitted as `ctx["sources"]`.
Each source reports `available` + a `status`, and may contribute
`brief_nudges` (folded into the qualities brief, capped by `SOURCE_CAP`),
`themes`, `seeds` (concrete search queries), and `data`. Sources degrade
gracefully — one with no creds/data returns `available: false` with a
status telling you how to switch it on, never crashing the brief.

| Source | State | What it adds |
|---|---|---|
| `raga` | **live** | Hindustani raga time-theory — ragas for this time-of-day + Melbourne's (southern-hemisphere) season; rasa → brief nudges, canonical recordings → seeds |
| `genekeys` | **live** | Shadow→Gift→Siddhi for core + transit gates (joins HD's 64 gates) |
| `sabian` | scaffold | degree→symbol numbers; load a verified 360-symbol table to enable |
| `features` | config | local audio-feature retrieval — `pip install 'astrotunes[features]'` + import music to beets |
| `lastfm` | config | crowd mood tags + your loved-tracks — set `LASTFM_API_KEY` / `LASTFM_USER` |
| `ha` | config | Home Assistant heart-rate → energy/tempo — set `HA_URL` / `HA_TOKEN` / `HA_HR_ENTITY` |

MCP-only signals (Google Calendar for activity, Spotify now-playing) are
*not* sources — the CLI can't call MCP servers; the `music-transit` skill
pulls those itself. Reference data (raga samay, Gene Keys) is verified
against Bhatkhande/Jovian and genekeys.com respectively.

## CLI

```sh
astrotunes context              # print JSON: transits, moon, weather, time
astrotunes recommend             # interactive: prompts mood/activity, prints picks
astrotunes recommend --queue mpd # queue first pick via Mopidy
astrotunes recommend --queue media-mcp  # queue via local mpv-mcp HTTP
```

## Backends

| Backend     | Module                        | Notes                                |
|-------------|-------------------------------|--------------------------------------|
| `stdout`    | `astrotunes.backends.stdout`  | Default. Print tracks, you play them. |
| `mpd`       | `astrotunes.backends.mpd`     | `mpc add yt:<url>` against p8ar's Mopidy. Requires `[mpd]` extra. |
| `media-mcp` | `astrotunes.backends.media_mcp` | POST to `/api/cmd` on the local media-mcp. |

## Install

```sh
pip install --user ./packages/astrotunes        # base
pip install --user './packages/astrotunes[mpd]' # with Mopidy backend
```

## Why this lives in `agent-media`

`astrotunes` decides **what** to play. `media-mcp` controls **how** it
plays. They live in the same monorepo because the typical install runs
both on the same phone.
