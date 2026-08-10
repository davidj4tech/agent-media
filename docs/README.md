# Documentation

Organised by **lifecycle**, not by topic. The title already says what a file is
about; what you cannot tell from a title is the question that actually gets
asked — *when may I delete this?*

| directory | what it holds | when it goes |
|---|---|---|
| [`reference/`](reference/) | how things are now | when the code it describes goes |
| [`decisions/`](decisions/) | why we chose X | never — superseded, not edited |
| [`proposals/`](proposals/) | wanted, not built | when built, or when abandoned |
| [`notes/`](notes/) | working knowledge, findings, gotchas | trim freely |
| [`handover/`](handover/) | session state, one file per session | supersede, but keep the history |

Dated kinds are named `YYYY-MM-DD-slug.md`, so staleness is visible in a
listing. Reference docs use a bare slug — they are meant to be current.

Each dated file opens with its title, then `Status:` and `Date:`. That is
what lets this index, and the popup's document chooser, be generated rather
than maintained by hand.

## Reference

- [channel-architecture](reference/channel-architecture.md) — how the channels fit together
- [channels](reference/channels.md) — the channels in detail
- [control-surface](reference/control-surface.md) — the control layer
- [extensions](reference/extensions.md) — extending agent-media
- [hermes-voice](reference/hermes-voice.md)
- [music-local-and-snapcast](reference/music-local-and-snapcast.md)
- [restructure](reference/restructure.md) — the package layout and how it got there
- [rooms-unit-ownership](reference/rooms-unit-ownership.md)
- [sillytavern](reference/sillytavern.md)
- [speech-phone-render-grade-b](reference/speech-phone-render-grade-b.md) — the phone lane
- [version-skew-monitoring](reference/version-skew-monitoring.md)

## Decisions

- [2026-08-05 speech-state-convergence](decisions/2026-08-05-speech-state-convergence.md) — keep `/speech` and `speech-state.service` separate

## Proposals

- [2026-08-09 spoken-docs](proposals/2026-08-09-spoken-docs.md) — this layout, and playing documents from the popup
- [2026-08-09 doc-roots-org-para](proposals/2026-08-09-doc-roots-org-para.md) — how playback meets GTD, PARA, org-roam and Denote
- [2026-08-07 play-video](proposals/2026-08-07-play-video.md) — `media play-video` subcommand

## Notes

- [2026-08-05 speech-controls](notes/2026-08-05-speech-controls.md) — breadcrumb + control channel, OpenWebUI STT

## Handover

- [2026-08-10](handover/2026-08-10.md) — documents you can listen to; the phone lane leaves ssh
- [2026-08-09](handover/2026-08-09.md) — speech routing and the phone lane

Handover used to be a single `HANDOVER.md` that was **gitignored** — the
fastest-rotting document in the repo was the only one with no history, so
every session's context was overwritten by the next. One file per session,
tracked, fixes that.
