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
- **Time of day** — current Melbourne local time
- **Mood + activity** — prompted at invocation
- **Weather** — Open-Meteo, no API key (Melbourne lat/lon hardcoded)

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
