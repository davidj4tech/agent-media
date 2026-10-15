# Mail badge on the home screen's project chips

David, 16 Oct 2026, after testing the agent-mail notification: "It might be nice to have a little indicator on the project chip that there's mail in a project too." He agreed to this outline ("I'll go with your instincts").

## Why
The only signal for waiting mail is agent-mail-inbox-hook (agent-config), which runs when a session starts. So you only learn there's mail once you've opened a chat in that project. A badge on the chip shows it before you pick where to go.

## Where
- **App:** sasonica-app `app/routes/home.tsx`, `QuickStart` (~line 554). These are the "<place> · Claude" chips built from `data.places`. Draw a small count, or a dot, on a chip whose place has mail. Tapping does what it does now (`/new?cwd=…`); the new session's hook reports the mail and asks before claiming it. Don't badge the `row-project` label on conversation rows: one project can have several chats, so it gets noisy. If the app lists projects anywhere else, badge those too.
- **Server:** `GET /targets` → `places` (server-contract.md ~line 252/311). Add `mail: <n>` to each place. n is the number of **pending** (unclaimed) messages in that place's mailbox; claimed and acked ones don't count. Leave out the shared `sam` mailbox.

## Place → mailbox
Use the same rule as agent-config's `bin/agent-handoff` `mailbox_of`. In a git repo, the mailbox is the repo name, and a worktree maps to its main repo: `git rev-parse --path-format=absolute --git-common-dir`, then the parent of `.git`. Outside git, it's the folder's name. Example: `~/projects/websites-somahz-home` → `websites`. Cache it per path; places only change occasionally.

## Counting
The mailbox lives on red5, the same host as agent-media. Read it the cheap way: one query or one `agent-mail --json inbox` per mailbox, cached for a few seconds. `/targets` is polled, so it mustn't call agent-mail for every place on every poll. Check skills/agent-mail/SKILL.md in ~/projects/agent-mail for the DB path and its CLI vs DB conventions.

## Done when
- The contract documents `places[].mail`, and the server has tests.
- The app draws the badge, and nothing when there are 0 messages or the field is missing (older servers).
- When you reply to this mail with the outcome, include a screenshot if you can.
