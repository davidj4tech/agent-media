# Speech lanes unified (15 Oct 2026)

Task: David chose to unify submit_event and submit_stream (the Pi streaming lane,
which had a NameError since 81ed8f0 — fixed in 36dc6fc, pushed).

State: a subagent did it on branch `worktree-agent-a83f1afa75aeac2bb`, commit dc426c6
(worktree .claude/worktrees/agent-a83f1afa75aeac2bb). NOT merged, NOT pushed.
submit_stream is now a thin front end over _submit_event(source=sentences).
Core suite: 2644 passed, 5 skipped (+10 tests in tests/test_submit_stream_unified.py).

Open before merging:
1. A cut streamed reply waits without a timeout for the source to end before writing
   history (finished path: 120 s). Consider a bound.
2. Cut/abort during a gap between streamed sentences only lands when the next arrives.
3. The finished-reply (Claude Code) path changed slightly: _sentence(i) loop, _mark trims
   lists, end-of-reply skip re-reads the count; MEDIA_STREAM_LEAD_S<=0 + first render
   failing now renders the rest in the background.
Nothing on red5 uses streaming (Pi here doesn't load agent-media's Pi package).

Next: review the diff of the finished path, rerun the suite, ask David, merge + push,
then watch one Claude reply speak normally on the phone.
