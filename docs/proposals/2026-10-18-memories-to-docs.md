# Memories that should be docs

Status: done 18 Oct 2026 — docs written (6473cd4, 750c9f4, ed7f391, sasonica-app 5438a95), agent notes in the context hook (8e8b7d7), 80 memories moved to memory/_moved/
Date: 2026-10-18

The agent memories in `~/.claude/projects/-home-ryer-projects-agent-media/memory/`
are seen only by Claude Code sessions started in this folder on red5. This
audit read all 136 memory files (plus `MEMORY.md` and the seven hubs) and
gives each one a single destination:

- **A, personal**: stays a memory.
- **B, developer docs**: goes into a repo doc.
- **C, user FAQ**: something any user would run into.
- **D, built-in agent context**: agent-media's own hooks deliver it to every install.
- **E, obsolete**: archived, superseded, or already documented in a repo.

Coverage was checked by grepping agent-media `docs/`, `AGENTS.md`, every
`packages/*/README.md`, sasonica-app `README.md` and its `docs/`.

## Summary

| destination | count |
|---|---|
| A personal (stays a memory) | 28 |
| B repo developer docs | 46 |
| C user docs / FAQ | 11 |
| D built-in agent context | 7 |
| E obsolete / already documented | 44 |
| **total** | **136** |

### Highest-value moves

1. **The speech-etiquette rules become built-in context (D).** These are
   replies-are-spoken-keep-them-short, ask-question-lead-prose,
   bash-descriptions-feed-the-phone, commands-in-code-blocks and
   links-clickable. Every Sasonica install speaks replies and shows the step
   list, but today only David's sessions in this one folder know how to
   behave for it. `media-hook-context` already injects a once-per-session
   note (the thread link), so the mechanism exists.
2. **Add a "Testing without touching a real device" section to agent-media
   `AGENTS.md`.** It draws on tests-reach-the-real-phone,
   debug-harness-writes-real-state, replay-unmutes, book-play-defaults-to-the-phone,
   ci-runs-from-packages-core and worktree-tests-use-main-venv. These are the
   most expensive mistakes on record, and none of them is in a repo.
3. **Give sasonica-app an `AGENTS.md` (with `CLAUDE.md` as a symlink, as in
   agent-media).** It would cover the shared tree, the What's new rule, the
   three web builds, and phone-test hygiene (no `monkey`, no HOME presses,
   end with stop). That repo has no agent notes at all today.
4. **Write a new `docs/reference/claude-code-hooks.md`.** It would cover what
   `media-setup profile` registers and why (timeouts, async, the
   load-bearing `AskUserQuestion` matcher, `PostToolUseFailure` for typed
   answers), how asks, answers and slash commands reach the transcript, and
   that hand edits get rewritten.
5. **Write a new `docs/reference/speech-queue.md`.** It would cover ordering,
   barge-in, supersede, the priority-inheritance cycle fix, the paused-reply
   step-aside, and how to find the token holder.
6. **Extend `docs/reference/follow-along.md` with "The app's clock".** That
   covers `play_started_at` + `clip_starts_s`, the two-clocks skew,
   follow-trace.log, and following on the clock rather than polling the
   phone player.
7. **Start a user FAQ (`docs/faq.md`).** First entries: replies not
   auto-playing (silent/DND hold), speech pausing by itself (Android System
   Intelligence holding the mic), `~/.cache/agent-media` being the only copy
   of clips, a stale tmux env masking config edits, and silent speech at
   media volume 0.
8. **Record three standing design rules as decisions.** The app stays
   generic and the server knows the setup; nothing new may rely on the
   tailnet; and GPL code stays out of process, never in the Apache code.
9. **Delete or archive the 44 E files.** About half are companion-app and
   ABS-era history that the hubs already mark retired.

### Stale or contradictory memories found along the way

- `questions-as-multiple-choice.md` still says to `media say` a lead-in
  before AskUserQuestion. `ask-question-lead-prose.md` reversed that on
  23 Sep, so the two contradict each other. Fix the first one before
  anything is promoted.
- `speech-ordering-and-barge-in.md` ends with the same outdated "same-turn
  prose is dropped" claim. `replies-are-spoken-keep-them-short.md` calls
  that prose "*never* spoken".
- Eight `[[links]]` point at memory files that do not exist:
  media-lane-pinned-phone, mpv-ipc, no-test-playback-left-on-the-phone,
  p8a-opensles-audio-broken, pnr-pinenote, red5-memory-oom,
  speech-feed-quiet-window and speech-speaker-routing.
- `packages/engine-piper/README.md` says the package is MIT. The licensing
  decision says all of agent-media is Apache-2.0.
- `speech-follow-along-assumptions.md` says to find the token holder in
  `/proc/locks`. `speech-queue-priority-cycle.md` says `/proc/locks` shows
  nothing on red5's kernel. A reference doc should give only the method
  that works.

## A: personal (stays a memory)

| memory | gist | target | notes |
|---|---|---|---|
| ai-accounts-api-vs-subscription | which of David's emails each AI vendor account uses | stays | — |
| notmuch-reads-forwarded-mail | check mail delivery in red5's notmuch, don't ask David | stays | — |
| galaxy-a14-access | ssh/adb to David's Galaxy A14; Auto Blocker blocks installs | stays | — |
| pine-note-eink | David's Pine Note runs DU4, so tune figures for 4 greys | stays | The general e-ink tuning is already in packages/visual README (`MEDIA_VISUAL_MONO`, `?eink=1`). |
| browser-mcp-servers | where the browser MCPs run (red5, p8a, Pine Note) | stays | The Termux Playwright shim belongs in agent-config, not these repos. |
| driving-a-browser-on-hpo | a visible Windows browser via `schtasks /it`, CDP from the Windows side | stays | — |
| hpo-wsl-restarts-and-interop | hpo's WSL idle timeouts and the binfmt interop timer | stays | — |
| cloudflare-token-and-zones | which Cloudflare credentials exist on red5 and what they can do | stays | Credentials stay in memory only. |
| memory-namespace-split-sam-ryer | the Hippocampus store's location and the sam→ryer merge | stays | If anywhere, it belongs in the agent-memory repo. Global CLAUDE.md still defaults to `ryer,sam`. |
| agent-sessions-bin-is-live | agent-sessions edits go live at the next 30-min tick | stays | Belongs in agent-sessions `docs/` if anywhere. |
| meridian-jobs-pile-up | every Meridian call leaves a `~/.claude/jobs` record | stays | Listing code already filters it (server-contract, `MEDIA_SESSIONS_EXCLUDE_CWD`). |
| relay-mailbox-watcher | Sam's relay inbox nudges the pane (peek, edge-triggered) | stays | tmux-relay may be retired (cloudflare-token-and-zones says 18 Sep). If so, it becomes E. |
| relay-reply-short-first | mail to Cece: short speakable message first, detail second | stays | — |
| sam-cece-voice-protocol | turn-taking between Sam and Cece | stays | The `speech-hold` mechanism is already in reference/input-claim.md, reference/control-surface.md and proposals/2026-08-11-converse-reply.md. Only the Cece specifics remain. |
| podman-orphan-layers-are-not | never hand-delete under podman storage; ask podman | stays | A red5 operating rule, not agent-media's. |
| resume-stopped-agents | resume a stopped background agent with SendMessage at once | stays | A candidate for global CLAUDE.md, not these repos. |
| rtk-rewrites-git-diff | use /usr/bin/git for a patch you will apply (rtk) | stays | — |
| sasonica-queue-no-goahead | build, merge and deploy queued items without asking; still ask real questions | stays | David's autonomy preference. |
| agent-mail-deliver-starts-sessions | deliver starts a NEW session when the target is headless | stays | The bug belongs in agent-config's agent-mail docs. |
| amux-start-needs-client | spawn a Claude TUI as a tmux new-window in an attached session | stays | The convention ("How a session ends") is already in docs/README.md. The rest is David's tmux layout. |
| phone-single-checkout | p8a's one checkout, deploy = pull + uv pip install + sv restart | stays | The general Termux deploy steps could become a C entry ("Updating a Termux install"). |
| edge-tts-patched-on-red5 | red5's edge-tts is hand-patched to the msedgeservices host | stays | Drop the note once an edge-tts release carries #491. |
| music-phone-backend | which player and endpoint music uses on David's hosts | stays | Mostly host-specific and partly stale. The datacentre-IP rule moves via youtube-downloads-on-the-phone. |
| adb-shell-via-self-pairing | Termux adb self-paired to 127.0.0.1; the agent-phone-adb tool | stays | The tool's docs live in agent-config (`phone-adb` skill). |
| sasonica-web-fork | red5's ABS serves the React fork, built in CI | stays | The musl/glibc mount trap belongs in the sasonica-web README if that fork lives on. |
| landscape-watch | weekly digest of similar projects in docs/landscape | stays | David's own timer. The output is already in the repo. |
| contacts-sources | the phone and David's Google Cloud project as contact sources | stays | The general design is in server-contract §6.25 and proposals/2026-10-17-contacts.md. |
| firefox-nightly-focus-interrupt | Firefox Nightly 159 stays paused after Sasonica's transient focus | stays | Still OPEN. If it reproduces on stable Firefox, add a C entry. |

## B: repo developer docs

| memory | gist | target | notes |
|---|---|---|---|
| tests-reach-the-real-phone | the shell's MEDIA_* config makes a real SinkSpeech play on the phone | agent-media AGENTS.md, new "Testing without touching a real device" | Not documented. The conftest scrubbers exist, but nothing explains them. |
| debug-harness-writes-real-state | importing another test's helpers skips its autouse isolation | same AGENTS.md section | Not documented. |
| replay-unmutes | `media replay` sets mute=false, so muting does not silence a test | same AGENTS.md section | Not documented. Keep the frames-port observer recipe. |
| book-play-defaults-to-the-phone | an untargeted `media book play` rsyncs the file to the phone | same AGENTS.md section (the private-mpv + `--target local` recipe) | Not documented. |
| ci-runs-from-packages-core | CI runs pytest from packages/core; a monotonic clock's zero is boot | same AGENTS.md section, plus a "when adding a playback route, extend the conftest scrubber" line | Not documented (only in handover 2026-08-17-night). |
| worktree-tests-use-main-venv | the .venv is editable from main, so a worktree's tests run main's code | same AGENTS.md section (the PYTHONPATH recipe) | Only in a handover. |
| concurrent-sessions-commit-sweep | the shared tree: sweeps, hunk splits, autostash, restarting shared services | agent-media AGENTS.md "Git" (extend) + new sasonica-app AGENTS.md | Partly covered: AGENTS.md has "stage by path". Add: check `git diff` of each staged file for foreign hunks, never autostash, check who is active before a restart or APK install. |
| headless-sessions-have-no-pane | a live phone-started session has no tmux pane | agent-media AGENTS.md "Is another session live?" | Not documented. Check the transcript mtime before touching another session's files. |
| app-stays-generic-server-knows-setup | the app renders what the server answers; no host knowledge in the app | both repos' AGENTS.md "Design rules" + docs/decisions/2026-09-21-app-stays-generic.md | Partly covered by proposals/2026-09-21-what-a-chat-can-be-pointed-at.md. A rule needs to be somewhere every agent reads. |
| software-off-the-tailnet | new links use public HTTPS + scoped auth or outbound connections | both AGENTS.md "Design rules" + docs/decisions/2026-10-18-off-the-tailnet.md | Partly covered by proposals/2026-10-03-off-the-tailnet.md and server-contract (transport). |
| licensing-apache-south-pen | Apache-2.0 © South Pen Labs; ABS forks stay GPL; never copy GPL code in | docs/decisions/2026-09-22-licensing.md + an AGENTS.md line | Partly covered by the sasonica-app README "Licence" section and proposals/2026-08-20-monetization.md. The engine-piper README says MIT. |
| plain-http-hides-sec-fetch-headers | no `Sec-Fetch-*` on plain http; a 127.0.0.1 test hides it | packages/visual/README.md "Gotchas" + AGENTS.md | Only in a handover. |
| cloudflare-eats-502-504 | Cloudflare swaps origin 502/504 for CORS-less HTML; the server remaps them via Cf-Ray | server-contract.md transport / §19 | Not documented. |
| canvas-carries-words-not-art | the canvas shows the reply's words and figures; ambient art is off | docs/decisions/2026-09-21-canvas-carries-words.md + `MEDIA_VISUAL_AMBIENT` in packages/visual README | Not documented. The env value itself is David's. |
| canvas-focus-ring | one mode state machine; never add a parallel focus flag; harness T16 | packages/visual/README.md "Focus ring" | Mostly covered. Add the "never a parallel flag" rule. |
| speech-hooks-owned-by-profile | `media-setup profile` owns the hooks; timeouts; hand edits get rewritten | new docs/reference/claude-code-hooks.md | Partly covered by server-contract and version-skew-monitoring. Nothing explains why each value is what it is. |
| ask-answer-cut-needs-posttooluse | the matcher is load-bearing; typed answers fire PostToolUseFailure | claude-code-hooks.md | Not documented (only in the grok proposal). |
| questions-and-answers-in-the-transcript | `extras.ask` list; answers via PostToolUse; task notifications stripped | claude-code-hooks.md | Not documented. |
| slash-commands-in-the-transcript | `slash.py` is the one rule: settings commands drop, work commands become a chip, /loop records once | claude-code-hooks.md | Not documented. |
| slash-menu-from-init-event | `claude -p` stream-json's init line lists every command, free | new docs/reference/harnesses.md | Only in proposals. The app-side file names are ABS-era and need updating. |
| codex-and-pi-in-sasonica | how Codex and pi sessions are found, resumed and started | harnesses.md | Partly in proposals (headless, grok, gemini). Also a C line: Codex waits on "Hooks need review" at the desk. |
| narrow-pane-hides-working | a phone-width pane truncates the footer; test captures at 34 cols | harnesses.md | Not documented. |
| ghost-prompt-in-sasonica | the ghost prompt is scraped from the pane; the Stop hook writes a fallback | harnesses.md | Partly covered (server-contract `suggestion`, `MEDIA_FOLLOWUP`). Add the truncation rationale. |
| ask-from-the-phone-tmux-plumbing | four traps when opening a Claude TUI from systemd | harnesses.md (use `reply.open_window`) | In handover 2026-09-09 only. The "deleting a conversation" half is ABS-era and can go. |
| speech-ordering-and-barge-in | ranks; same-session canonical order; URGENT barge-in; supersede | new docs/reference/speech-queue.md | `supersede` is in server-contract. The ordering design is not documented. Drop the stale loose end. |
| speech-queue-priority-cycle | the cross-session deferral cycle; `_claim_rank` inheritance; paused step-aside | speech-queue.md | Not documented. |
| speech-follow-along-assumptions | the follow loop holds the global token; breaker thresholds | speech-queue.md "Diagnosing late speech" | Not documented. Reconcile the `/proc/locks` advice (see above). |
| phone-player-stuck-on-a-clip | "speaking, nothing playing" = lost frames; frames-timing.log; LOST_S | new docs/reference/speech-phone-lane.md (diagnostics) | Only in handover 2026-10-17, which is untracked. |
| phone-player-reads-trip-breaker | never poll the phone player; follow on the clock with an answer-only check | follow-along.md "The app's clock" | Not documented. |
| speech-goes-through-a-relay | `media-ipc-relay` keeps spare connections to the phone's player port | speech-phone-lane.md | Only in the roadmap. The red5 env line is David's. |
| follow-along-clock | the bold = `play_started_at` + `clip_starts_s`; replies use the clip lane | docs/reference/follow-along.md, new "The app's clock" | follow-along.md covers the tmux surfaces only. |
| follow-two-clocks | `/speech/now` pos is summed clips, elapsed is wall time; holes become skew | follow-along.md "The app's clock" | Not documented. |
| follow-trace-log | the app logs each bold move to follow-trace.log | follow-along.md "Diagnosing" | Not documented. |
| pinned-scaletempo2-eats-speed | a pinned scaletempo2 never gets speed changes; pin only on openal | docs/reference/channels.md (mpv notes) or packages/core README "sinks" | Only in handovers. |
| mpris-channel-exposure | MPRIS on red5; identify our mpv by PID and fail closed | channels.md "MPRIS" | packages/core README mentions MPRIS. The PID rule and the bus-name clash are not documented. |
| youtube-downloads-on-the-phone | YouTube blocks datacentre IPs, so the phone fetches | docs/reference/channel-architecture.md | channel-architecture mentions yt-dlp but not the constraint. Also worth a C line ("YouTube won't download on a VPS"). |
| book-misses-voice-sessions | dictation goes through call_guard, a voice session through the app's focus policy; BookHold | sasonica-app docs/ (holds) or channels.md | Partly in proposals/2026-09-19-companion-into-sasonica. The names are companion-era. |
| runit-services-lack-xdg-runtime-dir | runit services have no XDG_RUNTIME_DIR, so pactl fails and openal plays silently | packages/core README "Termux services" | Not documented. Debt: the phone-local mpv-music run scripts are not in the repo. |
| call-guard-host-roles | host roles and peers in config.toml; the `force-include` packaging trap | packages/core README (roles; "a new data dir needs force-include") | Roles are partly in proposals/2026-10-18-roles and server-contract. `force-include` is not documented anywhere. |
| notes-tab-org-notes | the Organiser's server/app halves; redeploy and test traps | packages/org-paragtd README + server-contract §6.10 | Feature already documented. Add the traps (install paragtd before the canvas restart; `pkill -f` kills its own shell). |
| no-monkey-launches-on-p8a | `adb shell monkey … 1` sends random input; launch with `am start -n` | sasonica-app AGENTS.md "Testing on a real phone" | Not documented. The device-specific part stays personal. |
| no-home-presses-on-a-phone-in-use | never drive the UI of a phone in use; measure with logcat | sasonica-app AGENTS.md "Testing on a real phone" | Not documented. |
| test-playback-on-davids-phone | end a playback test with stop (not pause); restore the position | sasonica-app AGENTS.md "Testing on a real phone" | The :8772 details are ABS-app era. Keep the rule. |
| sasonica-web-three-builds | a web change deploys to build/, build-app/ and app.sasonica.com | sasonica-app AGENTS.md "Deploying a web change" | The README covers build-app. The "rebuild all three, grep the assets" checklist is missing. app.sasonica.com is David's. |
| sasonica-brand-icons | icons come from regen.py; the adaptive icon must fit the 66 dp circle | sasonica-app README "Android shell" | Not documented, and `brand/` still lives in the old fork (debt). |
| sasonica-keep-upstream-mergeable | fork logic in fork-only files; small `// Sasonica:` blocks; never reindent | sasonica-abs / sasonica-web README (CONTRIBUTING) | Applies only to the ABS forks now. sasonica-app is its own code. |

## C: user docs / FAQ

| memory | gist | proposed FAQ question | notes |
|---|---|---|---|
| silent-ringer-holds-watched-replies | silent or DND holds every reply, even the open thread's | "Why aren't replies playing automatically?" | The setting is in server-contract (`held_why`, `quiet_kind`). Answer: the ringer is silent or DND is on, "Hold replies while on silent" is set, and Speak anyway is available. David's own choice stays in memory. |
| mic-baseline-is-permanent | Android System Intelligence samples the mic constantly, so speech pauses at random | "Speech keeps pausing by itself, or voice typing says another app is using the mic" | Android-wide, so any Pixel user hits it. Answer: the appops command and the fact that it reverts. The MicWatch rules (unsilenced only, VOICE_COMMUNICATION wins) also go to sasonica-app docs (B). |
| cache-clear-loses-speech-clips | `~/.cache/agent-media` holds the only copy of the rendered clips | "Can I delete ~/.cache/agent-media to free disk?" | Not documented. Also a code question: should clips live in `~/.local/state`? |
| tmux-server-env-masks-config | the env loader only fills unset vars; a long-lived tmux server pins stale values | "I changed agent-media.env and nothing changed" | Not documented outside a handover. Also add a line to packages/core README "Configuration". |
| phone-silent-check-volume | mpv plays into Android's media stream; 0/25 is silent while everything reports healthy | "Speech is silent on the phone but everything looks healthy" | `media doctor` reports it. Not in docs. |
| phone-renders-its-own-speech | the phone renders speech itself (Edge voice, Google fallback per sentence) | "Why does the voice sometimes change to a different one?" | Fold in edge-429 (mobile-data 429s) and how to read alert_log. `MEDIA_SPEECH_RENDER_SASONICA` is in server-contract. |
| app-text-follows-android-font-scale | the WebView follows Android's font scale | "Why is text bigger in the app than in the browser?" | Mentioned in the roadmap. David's choice (no textZoom pin) stays in memory. |
| termux-native-wheels-from-pkg | pip-built cryptography fails to dlopen on Termux; use `pkg` | "On Termux, `cryptography` fails with 'cannot locate symbol PyLong_Type'" | Not documented. |
| ytdlp-nightly-on-phone | YouTube 403s mean stable yt-dlp is behind; install the nightly | "YouTube music fails with HTTP 403" | The dotfiles self-repair is David's. The diagnosis order is general. |
| mopidy-4-upgrade-path | Mopidy 4: pin setuptools<81, the Iris patch, the pipx inject trap | "Mopidy/Iris won't start after upgrading to Mopidy 4" | Archived as done. Still useful to anyone on Mopidy 4. Could live in reference/music-local-and-snapcast.md instead. |
| vimium-canvas-exclusion | keyboard extensions such as Vimium intercept the canvas's keys | "Keys on the canvas do strange things (open a search, scroll)" | The device and exclusion details stay in memory. |

## D: built-in agent context

| memory | gist | context it would carry | notes |
|---|---|---|---|
| replies-are-spoken-keep-them-short | replies are read aloud in full; aim under a minute | "Your replies are spoken aloud. Lead with the answer, aim under ~150 words, and put long tables or lists in a file." | Only when speech is on. The same text is in David's global CLAUDE.md. |
| ask-question-lead-prose | same-turn prose before AskUserQuestion is spoken; never `media say` it | "Prose before a question is spoken as part of it. Keep it to two sentences and don't say it yourself." | Claude Code only. Already in global CLAUDE.md (David's). |
| bash-descriptions-feed-the-phone | Sasonica's step list shows each Bash `description` | "Give every Bash call a plain-words description; the phone shows it as the step." | Claude Code only. `_plain()` is the fallback. |
| commands-in-code-blocks | anything to copy goes in its own fenced block | "Put anything the user must copy in its own code block; they copy from a phone." | Applies to any phone reader. |
| links-clickable | places to go are full https markdown links | "Give links as markdown with full https URLs, not bare host or file names." | Thread links are already delivered (`LINK_NOTE` in intake/context.py). |
| questions-as-multiple-choice | ask questions as AskUserQuestion cards, recommendation first | "Ask questions as AskUserQuestion cards (recommended option first); they can be answered with a tap on the phone." | **Fix the stale `media say` line first.** David's taste for them stays in memory. |
| no-silent-waits | a turn must not end on a watcher that can fail silently | "Before ending a turn on a background wait, give the watcher a deadline and have it always report." | A thread in limbo is visible in the app. The hpo/Windows half stays A. |

## E: obsolete or already documented

| memory | gist | covered by / status | suggestion |
|---|---|---|---|
| thread-links | link threads as https://…/app/t/<session> | `LINK_NOTE` in intake/context.py (delivered to every session) + server-contract "Thread links" | delete |
| sessiond-restart-ends-your-own-chat | restarting sessiond kills phone chats mid-turn | AGENTS.md "Restarting services". The `set_model` / bypass-beats-plan facts are in server-contract | delete |
| restart-services-safely | use `media restart-services … --after 10` | AGENTS.md + reference/version-skew-monitoring.md | delete (the MEMORY.md rule line can go too) |
| canvas-restart-after-pull | page-only changes need no restart | AGENTS.md | delete, or keep one line: "restart the canvas on every host running one" |
| fleet-health-checks | selfcheck, doctor --fix, crash-notify, termux heal | reference/version-skew-monitoring.md | delete. First move the "when a host is renamed, grep for the old name" line to that doc. |
| speech-latency-is-the-bridge | time to first audio is bridge round trips | docs/speech-latency-notes.md | delete |
| piper-fallback-engine | Piper runs out of process (GPL) | packages/engine-piper/README.md | keep the red5 setup as A, or delete |
| control-surface-layer | independence, not reversibility; dispatch per action | docs/reference/control-surface.md + the independence test | delete |
| highlight-fullscreen-goal | status rows carry the sentence | docs/reference/follow-along.md | delete |
| canvas-headless-harness | headless Playwright rig for the canvas | packages/visual/tests/browser/README.md | delete |
| visual-canvas-spike | the history of the canvas build | packages/visual/README.md | keep as history (archive hub) |
| owui-agent-media-bridge | the OWUI bridge, merged 31 Jul | packages/visual/deploy/README.md, intake-owui README | keep as history |
| server-contract-decisions | v1 contract decisions | docs/server-contract.md | delete |
| sasonica-monetization | free app, supporter purchase, hosted tier | proposals/2026-08-20-monetization.md, reference/entitlements.md, websites README | delete |
| alert-store | watchers report through agent-alert | server-contract §6.17 + proposals/2026-09-24-alerts-and-digests | delete. Move the "units need $HOME/.local/bin/agent-alert" line to the alerts doc. |
| sasonica-whats-new | add a What's new line for user-visible changes | sasonica-roadmap.md standing decisions | delete once sasonica-app AGENTS.md carries the rule (B) |
| sasonica-app-queue | the 22 Sep work queue | docs/sasonica-roadmap.md is the live list | keep as history |
| sasonica-app-repo | the app moved to its own repo | sasonica-app README | delete |
| sasonica-fork | the 26 Sep ID swap (com.sasonica.abs vs .app) | sasonica-app README "Android shell" | keep as history |
| sasonica-home-cleanup-later | the ABS Home page cleanup, deferred | ABS app superseded | delete |
| sasonica-titles-come-from-abs | thread names: /rename > ai-title > question | server-contract (`MEDIA_AUTO_TITLE`). The ABS library was removed 27 Sep | delete |
| sasonica-control-endpoint | the ABS app's :8772/:8773 player control | proposals/2026-09-09-book-read-along.md "As built" | keep as history while sasonica-abs plays books |
| speech-in-sasonica | the migration log of speech into the app | roadmap + sasonica-app README | keep as history. The CI-masks-"sasonica" trap goes to the sasonica-app README. |
| speech-plays-on-phone-openal | the Termux mpv speech target | archived | keep as history |
| speech-lives-in-the-conversations-library | the Conversations library | removed 27 Sep | delete |
| phone-speech-history-is-local | the phone's share listener asks the origin | fixed 2b7224f | delete |
| transcript-errors-are-silent | "Nothing said yet" hid fetch errors | ABS-app era; the string is not in sasonica-app | delete |
| edge-429-on-mobile-data | Microsoft 429s on mobile data | fixed sasonica-app e89232b | fold the diagnosis into the C voice FAQ, then delete |
| mediaplayer-holds-16x | the MediaPlayer speed spike | superseded (Media3 is used now) | keep as history |
| agent-media-native-app-goal | the goal of a distributable Android app | achieved (Sasonica) | keep as history. Move the Play Protect / NotificationListener and silent-AudioTrack facts to sasonica-app docs. |
| abs-fork-direction | fork ABS to play clips natively | superseded by sasonica-app | keep as history |
| abs-series-limit-zero | ABS `/series?limit=0` returns nothing | ABS archive feature retired | delete |
| abs-app-pr-and-ci | upstream PR #2010 and CI APK traps | archived | keep as history |
| progress-duration-pinned-at-write | ABS pins the duration at write time | fixed b15c663 | keep as history |
| feed-subscribed-via-abs | the podcast feed through ABS | retired 17 Sep | delete |
| companion-app-readout-endpoint | the companion's readouts on :8770 | retired 22 Sep | keep as history |
| companion-app-startforeground-anr | startForeground must run in onStartCommand | solved d7a5087 | keep as history. The FGS rule applies to sasonica-app too (add to its docs). |
| companion-app-design-system | the companion's Style.java design rules | companion retired | keep as history |
| call-guard-pause-for-calls | call-guard history and the Automate mic detect | mic detection moved into the app; Automate retired | keep as history |
| phone-beets-web-parked | the beets config renamed to .bak | resolved | delete (keep the mtime-vs-ctime lesson in A if wanted) |
| claude-config-retired | claude-config → agent-config | done | delete. The settings.json runtime-drift gotcha belongs in agent-config. |
| red5-resize-owui-handoff | the abandoned resize and the OWUI bring-up | abandoned 6 Sep | keep as history. **Action still open: remove the resize watcher on pn.** |
| d1-quota-clips-silently | the relay's D1 scan ran out of free-tier rows | tmux-relay fixed; relay retired 18 Sep? | keep as history. The general lesson ("read D1 usage hourly; a partial index for queues") could go into sasonica-shell docs. |
| runlet-is-sasonica-shell | Runlet renamed to Sasonica Shell | done | delete (it belongs in the sasonica-shell repo) |

## Where the FAQ should live

**Recommendation: one `docs/faq.md` in agent-media, with sections "In the
app", "On the server" and "On a phone running Termux".**

- Almost every C answer is fixed or diagnosed on the server or host (`media
  doctor`, env files, Termux packages, the hold settings). Splitting by
  symptom location would send readers to the wrong repo half the time.
- The server already has a document reader (`GET /docs`, contract §6.10).
  The app could open the FAQ from Settings → Help with no second copy.
  sasonica-app's README links to it.
- Each entry follows the same shape: the question as the user would say it,
  then a two-line answer, then the check command in its own code block, then
  "see also" (the reference doc). This keeps the FAQ short; the depth stays
  in `docs/reference/`.
- App-only questions with no server side (the font scale is the only one
  today) still go in "In the app", so there is one place to search.

## How the built-in agent context (D) would be delivered

agent-media already injects context into every harness it hooks. The
pieces are:

- `media-hook-context` (Claude Code UserPromptSubmit; `note_for()` is also
  called by Codex's `media-hook-codex`, Gemini's BeforeAgent hook, Grok's
  PostToolUse hook and the pi extension). It already adds the context-band
  notes and the once-per-session thread link (`_link_note`, with a marker
  file reset after a compaction).
- `media-hook-heard` (a synchronous UserPromptSubmit hook) already adds
  "how much of your last reply was heard" and the reply language.

Proposal:

1. **One shipped file, `agent_media_core/agent_notes.md`.** Each D rule is
   one short line, tagged with the condition under which it applies: speech
   on, Sasonica paired, canvas configured, or a Claude Code-only tool
   (AskUserQuestion, the Bash `description`).
2. **A `_notes_note()` beside `_link_note()` in intake/context.py.** It
   would emit the applicable lines once per session (same marker pattern,
   re-sent after a compaction), on whichever hook each harness reads context
   from. Lines whose condition does not hold are left out. For example, a
   desk-only install with speech off gets no "replies are spoken" line.
3. **Make it configurable.** `MEDIA_AGENT_NOTES=0` turns it off;
   `MEDIA_AGENT_NOTES_FILE` points at a site's own replacement.
   `media-setup status` shows whether it is on.
4. **Consider SessionStart for Claude Code.** Claude Code accepts
   `additionalContext` from SessionStart, which fires once and again with
   `source=compact`. That would remove the marker bookkeeping there. The
   UserPromptSubmit path stays for the other harnesses.
5. **Then trim David's global CLAUDE.md.** Its "Spoken replies & the visual
   canvas" and "Prose before a question" sections are D material too. The
   `[[visual:]]` / `[[reveal:]]` marker instructions belong in the same file
   (canvas-configured condition). Once shipped, the personal copies can
   shrink to David-only preferences.

Developer rules (B) stay in `AGENTS.md`. Every harness reads it in the repo
folder, so there is no need to inject them.
