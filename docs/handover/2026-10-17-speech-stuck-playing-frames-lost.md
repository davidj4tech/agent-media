# Speech "playing" over a silent phone — handoff (17 Oct 2026)

- Cause: red5 speech_frames Model stuck "playing" after the phone lost the held-chime frames (00:21:56); behind reports ignored.
- Done: agent-media a27b756 (LOST_S=15 s: behind report taken as truth, traced "lost"), pushed, canvas restarted.
- Done: sasonica-app 36df8a9 (revert of the watchdog 5dab5f0), 7a46a5d + 25b1844 (hold frames that arrive before the player is up, expire after 15 s), pushed to main.
- Done: app build from CI run 38979982693 installed on p8a.
- Done: p8a pulled to 1b38d17, its agent-media services restarted; state-gc fixed (1b38d17, sleeps a day under runit). doctor: p8a ok.
