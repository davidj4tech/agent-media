# agent-media: permanent server address (off-the-tailnet)

Moved from agent-mail #63 (sam → sam, 2026-10-14) on 2026-10-16.

For a NEW session in /home/ryer/projects/agent-media. David asked for this on 2026-10-14.

Goal: the cross-platform pairing gap. On phone/macOS/Linux the server pairs with the app over a Cloudflare quick tunnel whose address changes on every restart (sasonica.com/start tells users to re-enter it or re-run `sasonica pair`). Make the address permanent, per docs/proposals/2026-10-03-off-the-tailnet.md, likely via the hosted relay that Sasonica Shell already uses (optional Sasonica account; accounts must stay optional for self-hosters).

Coordinate with mail #62 (Windows server pairing, "Sasonica Shell Windows support" thread) since both may share the relay.

Reply in this thread with a plan before large changes. No Co-Authored-By/AI trailers; ask David before pushing/releasing.
