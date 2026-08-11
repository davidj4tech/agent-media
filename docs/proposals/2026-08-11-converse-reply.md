# Proposal: `media converse-reply` — let Cece answer a waiting `converse`

Status: **implemented** 2026-08-11 — `media converse-reply` (+ `--pending`,
`--wait`) in `cli.py` / `capture/rendezvous.py`; the doorbell in
`capture/doorbell.py`. Outstanding: one line in Cece's app instructions, which
lives outside this repo.
Date: 2026-08-11

## Motivation

`converse` speaks a question and blocks on a unix socket for the answer. Today
that answer can only arrive one way: David speaks, HA Assist transcribes,
`tmux-voice-bridge` calls `rendezvous.offer()`. That makes the tool useless for
talking to **Cece** — Claude live (voice mode) in the Claude Android app.

Traced 2026-08-11, the acoustic route is closed twice over:

- HA's only audio source is the Pixel 8a Companion app's push-to-talk.
  `wake_word_entity` is `null` on all three pipelines in
  `.storage/assist_pipeline.pipelines`, and the entity registry holds no
  `assist_satellite` / wyoming entity — only `stt.openai_stt` and two TTS
  engines. There is no passive listening anywhere.
- A live Cece session already owns the phone mic, so Companion push-to-talk
  cannot capture while she is talking anyway.

But nothing about the rendezvous is voice-specific. `offer()` is ~20 lines of
stdlib socket client; voice-bridge is merely its only caller today. Cece already
runs `media speech-hold` / `media pause` through tmux-relay. So the answer
should travel as **text**, not as sound.

## Key facts (verified 2026-08-11)

- `d1-runner` runs as an **active systemd --user service on red5**, same user,
  `XDG_RUNTIME_DIR=/run/user/1000` — the same path `rendezvous.socket_path()`
  resolves. **No ssh hop is needed**; a relayed command reaches the socket
  directly. (Contrast `play-video`, which does need `ssh p8ar`.)
- Relay latency: `d1-runner.sh` polls every `RELAY_POLL_INTERVAL` (default 5s),
  so a reply lands ~5–10s after Cece sends it. `converse`'s default
  `timeout_s=90` absorbs that comfortably, but see "Timeouts" below.
- The rendezvous is **single-slot**: a second `converse` raises `Busy`.
- `handle_command` in the shim runs *before* `do_inject`. A textual reply
  bypasses it entirely, so nothing of Cece's can be swallowed as a
  "switch to <host>" target change.
- A mailbox already exists — `relay-msg.sh --to cece` — with boxes named
  exactly `sam` and `cece`. It is explicitly **not a push**: Cece sees it when
  she next checks.

## Proposed CLI

```
media converse-reply "<text>"     # hand text to a waiting converse
media converse-reply --pending    # print the armed question, or nothing
```

Exit codes matter more than output here, because the caller is an agent:

| Exit | Meaning |
|---|---|
| 0 | a waiting `converse` took the text (it acked) |
| 3 | nobody is listening — no socket, or stale |
| 4 | socket live but the peer did not ack (do not assume delivery) |

Implementation is one thin `cmd_converse_reply` in `cli.py` over the existing
`capture.rendezvous.offer()` — no new transport, no new state.

```python
def cmd_converse_reply(a) -> int:
    """Answer a waiting `converse` as text, from wherever."""
    from .capture import rendezvous
    if a.pending:
        q = rendezvous.pending_question()      # see "The doorbell" below
        if q:
            print(q)
            return 0
        return 3
    if not rendezvous.socket_path().exists():
        print("converse: nobody waiting", file=sys.stderr)
        return 3
    return 0 if rendezvous.offer(a.text) else 4
```

Parser, matching the `speech-hold` shape:

```python
s = sub.add_parser("converse-reply",
                   help="answer a waiting `converse` with text (for agents "
                        "that cannot speak into HA Assist)")
s.add_argument("text", nargs="?", default=None)
s.add_argument("--pending", action="store_true",
               help="print the question currently awaiting an answer")
s.set_defaults(func=cmd_converse_reply)
```

## The doorbell — the actual open question

The verb is the easy half. Cece cannot *hear* Sam's question (`converse` speaks
it through the media channel on red5), so she needs to learn a question is
waiting and what it says.

`Rendezvous.__enter__` should write a sidecar next to the socket —
`converse.question`, JSON with `{text, asked_at, timeout_s}` — and unlink it in
`__exit__` alongside the socket. That is what `--pending` reads. It costs one
file write and keeps the socket itself unchanged.

**Built 2026-08-11 — and the finding is that the doorbell cannot ring Cece.**
There is no inbound push into a Claude app conversation: she acts only when
David speaks to her. Every design collapses to the same shape — Sam makes sure
*David* knows, and he turns to her. That is the protocol's own principle
("whoever knows, fires") rather than a workaround.

So `capture/doorbell.py` rings the phone, not Cece: on arm it ssh's a
`termux-notification` carrying the question and the command to answer it; on
return it removes the same id. Both halves matter. Ringing runs in a daemon
thread because a dozed phone holds an ssh open for the full timeout and the
human may already be answering. Clearing is synchronous on an 8s leash,
because a shade still reading "Sam is asking" after the question expired
invites an answer to a rendezvous that no longer exists — and because a caller
that exits first would leave it there. `MEDIA_CONVERSE_NOTIFY=0` disables both.
Host resolution is `_miss_notify.miss_host()`, so "the phone" has one
definition.

The other half is `--wait`: once David has told her, Cece calls
`media converse-reply --pending --wait 120` and blocks until the question
arms, rather than racing the relay's 5s poll and being told nothing is
waiting.

How Cece is *told* is a separate decision, and the options are already built:

1. **`relay-msg.sh --to cece`** at converse time. Reuses the mailbox, but it is
   not a push — she must check, so it only works mid-conversation when she is
   already checking.
2. **Poll `--pending`** on her side. Simple, but burns relay round-trips.
3. **Do nothing automatic** — Sam asks in the same relay message that carries
   the question. Honest and zero new machinery; the question travels as text
   and only the *answer* uses the rendezvous.

Option 3 is the one to build first. It needs no sidecar at all, and it makes
`--pending` an optional convenience rather than a dependency.

**Built anyway** (2026-08-11): the sidecar is six lines and makes the verb
self-describing, so an answerer can confirm *what* was asked rather than
trusting the relay message to still be accurate. It is published **before** the
bind — the socket appearing is what tells an answerer to look, so the other
order leaves a window where the rendezvous is armed and the question reads as
absent. `pending_question()` returns None when the sidecar outlives its socket:
a media-mcp that died mid-converse leaves a question nobody is listening for.

## Timeouts

With relay latency plus Cece's own turn, 90s is tight for a real exchange.
`converse(timeout_s=…)` is already a parameter — callers expecting Cece should
pass ~180s. Worth noting that the whole window holds the single rendezvous slot,
so David cannot answer a different `converse` meanwhile.

## Sync or async?

Blocking is a *feature* when the answer gates the rest of the task — that is
what `converse` is for, and it is right for a human who is present and can
answer in seconds. It is the wrong shape for Cece:

- The single rendezvous slot is held for the whole window, so David cannot
  answer a different `converse` meanwhile.
- Relay latency plus her own turn means the block is tens of seconds at best,
  and a Cece who simply doesn't check her box burns the full timeout for
  nothing.
- On timeout the question is *lost* — `converse` returns `{"reply": None}` and
  nothing persists.

But the async version should **not** be built into `converse`. It already
exists: `relay-msg.sh --to cece` is a mailbox with durable rows, and
`check_messages` / `--in-reply-to` close the loop. Async here means "post the
question, end the turn, collect the answer next turn" — which is exactly
mailbox semantics, and an agent turn cannot suspend and resume anyway.

So the rule:

| Can Sam do useful work without the answer? | Use |
|---|---|
| No — it gates the next step | `converse` (sync), short timeout, human expected |
| Yes | relay mailbox (async), answer collected next turn |

`converse-reply` stays worth building for the first row only: the case where
Sam genuinely must block and Cece is the one answering. It is the low-latency
last mile, not the general channel.

## Not in scope

- Speaker attribution on the acoustic path. Unchanged: a transcript is still
  just a transcript.
- Any wyoming / always-on satellite. The trace above rules it out as a route to
  Cece specifically; it remains a separate question for hands-free David.

Related: `packages/core/src/agent_media_core/capture/rendezvous.py`,
`packages/voice-bridge/src/tmux_voice_bridge/shim.py` (`offer_to_converse`).
