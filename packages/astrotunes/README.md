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
