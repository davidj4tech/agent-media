# Play all unread replies in a thread; fold the prompt suggestion (16 Oct 2026)

## Done, pushed
- agent-media 732b27b, f9096f3, e1a9969: opening a thread plays its held unheard
  replies oldest first (toast.take_for_opened → oldest; `replay --id` of a held
  unheard row chains `_next_unheard` via the follower's `--then-id`). e1a9969:
  the phone's clock-lane follower now calls `_then()` too (it never did — the
  first live test read only the first of two). Live on red5 (replay runs as a
  fresh process; the canvas was restarted for take_for_opened).
- sasonica-app 98907e7: the suggestion has a ▾/▸ arrow that folds it to one line,
  kept in localStorage `sasonica.chat.suggestionTucked`; test/suggestion.mjs.

## Open
- Build 1261 (98907e7) installed on p8a; then aa3eb94 (folded = the arrow's height) installed too, web builds + app.sasonica.com republished.
- CI 38888000786 (9843917): only server-Windows failed, in test_search.py
  (search finds nothing on Windows) — not touched by this work. Newer run
  38889003213 (7cbbaa8) was in progress; check whether it repeats.
- Not yet confirmed by David live: a thread with 2+ unread replies reading them
  all on the phone after e1a9969.
