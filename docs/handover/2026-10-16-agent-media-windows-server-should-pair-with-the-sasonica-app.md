# agent-media: Windows server should pair with the Sasonica app

Moved from agent-mail #62 (sam → sam, 2026-10-14) on 2026-10-16.

For the "Sasonica Shell Windows support" thread (session 6c9f78b2, project /home/ryer/projects/agent-media). David asked for this on 2026-10-14.

Goal: close the cross-platform gap on Windows. Today phone/macOS/Linux run the full Sasonica server and pair with the Android app; Windows only has Sasonica Shell on the hosted relay, which "doesn't pair with the app yet" (sasonica.com/start). Windows audio + local speech are already on main (audio-seam, win-followups).

Wanted: a plan, then the work, to make Windows a full Sasonica server that pairs with the app like macOS/Linux (installer, pairing link, services, speech). Coordinate with the separate "permanent address" effort (agent-media docs/proposals/2026-10-03-off-the-tailnet.md) since pairing over the relay may solve both.

Reply in this mail thread with the plan before large changes. No Co-Authored-By/AI trailers on commits; ask David before pushing/releasing.
