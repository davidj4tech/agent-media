# agent-media: session title falls back to ID on large transcript (383b7f33)

Moved from agent-mail #67 (sam → sam, 2026-10-15) on 2026-10-16.

Sasonica shows this session's title as "383b7f33" (session-ID fallback) instead of its /rename name.

Session: claude red5 383b7f33-5483-4f4e-9589-4aee68a4f863 (cwd ~/org)
Transcript: ~/.claude/projects/-home-ryer-org/383b7f33-5483-4f4e-9589-4aee68a4f863.jsonl — ~14 MB, mostly base64 screenshots.
It DOES contain {"type":"custom-title","customTitle":"Hetzner Cloud account setup complete"} several times, the last near the very end (byte ~13,980,015).
Earlier in the same session the app showed a proper title, so it regressed as the file grew.

Hypothesis (unverified): something between GET of the session list/feed and session_feed._asked() gives up on large transcripts (timeout or size cap) and falls back to the ID. _asked() itself reads the whole file line by line and would find the title.
Ask: find the fallback path, fix it (e.g. read custom-title/ai-title from the file tail first, or cache titles), add a test with a big transcript.
Screenshot of the symptom on p8a: /sdcard/Pictures/Screenshots/Screenshot_20261016-071234.png

## Outcome (16 Oct 2026)

Not the transcript size, and not the server. 383b7f33 is a headless session; `_headless_title` answers the shelf's name, else `session_feed._asked` (35 ms on the 15 MB file, the substring check skips the screenshots), else sessiond's `first_text`, so a `/targets` row for it always has a name. `/targets` names it "Hetzner Cloud account setup complete".

The bare id is sasonica-app's thread header (`app/routes/thread.tsx`): its title came from the list that linked there, the rows seen in this page load, or the *saved* list on disk, never from the server. A thread opened another way (a notice, a link, a cold start) for a chat newer than the saved list kept `session.slice(0, 8)` for good. Fixed there: when the saved list has no name for it, the page asks `/targets` once and saves the answer. `test/rename.mjs` §7 opens a chat the saved list has never seen and checks the header.
