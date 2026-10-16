# Speech lanes unified (15–16 Oct 2026) — DONE

Task: David chose to unify submit_event and submit_stream (the Pi streaming lane,
which had a NameError since 81ed8f0 — fixed in 36dc6fc, pushed).

State: merged as c9b6120 (dc426c6 + c84f06b), pushed, verified live on 16 Oct: a Claude
reply was saved with its thread title and 11 clips, no new speech errors.
submit_stream is now a thin front end over _submit_event(source=sentences).
Suite: 2645 passed, 5 skipped (+11 tests in tests/test_submit_stream_unified.py).

Resolved: a streamed reply's history write waits at most _STREAM_ARCHIVE_WAIT_S = 120 s
(as a finished reply does); on timeout history gets extras.truncated=True. Cut and
supersede are polled every 0.5 s.

Still open (not requested; nothing on red5 streams — Pi here doesn't load
agent-media's Pi package):
1. A MUTED streamed reply whose source hangs still waits with no bound (the 120 s
   bound covers the unmuted path only).
2. Cut/abort during a gap between streamed sentences only lands when the next arrives.
