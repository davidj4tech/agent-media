# Proposal: Jev answers the soft half of "free now?" (1 Oct 2026)

Status: **proposed 1 Oct 2026, nothing built, the Jev API not yet tried
from red5.** An addition to roadmap item 14
([speaks-when-youre-free](2026-10-01-speaks-when-youre-free.md)), not a
replacement. David asked how Sasonica might use Jev and picked this one
first.

## What Jev is

TypeSafe AI's "System One" model: it takes state plus a typed question
and returns a constrained answer with a probability, not prose. Three
kinds of question (`/v1/systemone`): **choice** (one of a set), **score**
(a level on a rubric) and a yes/no probability. Hosted; about 0.35–1 s a
call. Also on OpenRouter as `typesafe/jev-1.13`, so it can go through
the gateway's existing `OPENROUTER_API_KEY`
(`~/projects/agent-gateway/install.sh:19`). Docs:
[docs.typesafe.ai](https://docs.typesafe.ai/). Open lookalikes
([kev](https://github.com/jaredpalmer/kev)) run locally if hosted ever
becomes a problem.

## Why it fits item 14

`free.py` as proposed is rules: `call`, `quiet`, `meeting`, `night`,
`manual`. The first two are facts and should stay rules. The rest hide
judgment calls the rules make badly:

1. **Is this calendar event really busy?** The rule is "busy, not
   all-day, not declined". But "Focus time", "Lunch", "Dentist", a
   tentative invite and "Gym" are all marked busy and differ in whether a
   spoken sentence in the ear is welcome.
2. **Should this one break through?** The rule is "`URGENT` or `needs`".
   But a normal-level reply that says "the deploy failed and prod is
   down" deserves to break through, and an `--alert` about a disk at
   81 % does not.
3. **Is the catch-up worth a voice, or only a card?** One reply that says
   "done" is not worth interrupting the walk out of a meeting.

Each is a typed question with a small answer set, asked a few times an
hour: what Jev is built for, and too slow and costly for a Claude turn.

## The shape

[[visual: left, "free.py" box with two lanes. Top lane "hard facts: call, quiet ringer, manual" → "busy" directly. Bottom lane "soft: meeting event, a held item, the catch-up" → small box "jev.py: choice/score + probability" → "≥ threshold: act; below: fall back to the rule". A dashed arrow from jev.py to "jev-decisions.jsonl (question, answer, p, what the rule said)".]]

A new `core/jev.py`, one function per question, each with the rule from
item 14 as its fallback:

- `event_busy(title, location, attendees_n, status, minutes) →
  {hold, light, free}` and a probability. `light` means hold replies but
  let alerts through. Asked once per event when the phone first reports
  it, then cached by event id.
- `breaks_through(kind, level, thread_title, first_lines) → yes/no`
  and a probability. Asked only while busy, only for an item the rule
  would hold. The text sent is the first 300 characters.
- `catchup_worth(items) → {speak, card}`. Asked once on the
  busy → free edge.

**The rule decides when Jev can't.** If there's no key, a timeout of
1.5 s, an error, or a probability under the threshold (0.7 to start), the
item 14 rule answers. Jev can only move a decision, never block one, so
the gate still fails open.

**Every answer is logged** in `jev-decisions.jsonl`: the question, the
answer, the probability, what the rule would have said, and later
whether David pressed Speak now or Catch me up. That log is how we find
out if Jev is right, and it is how to tune the threshold.

## Order

1. Try it by hand: a script that asks the three questions on ten real
   cases (David's calendar this week, a day of held replies) and prints
   Jev's answer next to the rule's. No wiring yet. **This decides whether
   to go on.**
2. `core/jev.py` with the fallback and the log; tests with Jev stubbed.
3. Wire `breaks_through` into the item 14 gate (once item 14's step 1
   exists), shadow mode first: log only, the rule still decides.
4. After a week of shadow log, switch it on; then `event_busy`, then
   `catchup_worth`.

## Open questions

1. **What leaves red5.** Event titles and the first lines of replies go
   to TypeSafe (or OpenRouter). Fine, or event titles only, or local
   only (kev)?
2. **Cost.** Probably cents a day at this volume, but unmeasured. Is a
   paid key acceptable, or OpenCode's free `jev-1.13-free` only?
3. **Shadow week.** Worth the wait, or switch on straight after step 1?

## Later, if it earns its place

The same pattern serves the other ideas from the first conversation:
routing a phone or TV-mic message to a thread, ranking "Needs you",
screening a `phone_ask` for plausibility, and choosing between speaking a
reply and summarising it.

## Verification

- Step 1's table, reviewed with David.
- `pytest packages/core/tests/test_jev.py`: timeout, error, missing key
  and low probability all return the rule's answer; the log line has
  every field.
- `media doctor`: `jev=ok|nokey|down`, `jev_p50_ms=`, `jev_overrides_24h=`.
