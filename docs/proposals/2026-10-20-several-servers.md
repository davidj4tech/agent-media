# Several servers in one app, and settings by server

David, 20 Oct 2026: "with multi-server pairing, there are some settings and
sign in features that could be managed per server rather than per device.
maybe under an administrator role?" Status: **proposal**. Builds on
docs/proposals/2026-10-18-roles.md (admin, member, guest) and roadmap
item 20 (sasonica.com/account lists an account's servers).

## What there is

- **One server at a time.** The app keeps one address
  (`sasonica.chat.baseUrl`) and one device record (`sasonica.chat.device`:
  token, device id, `server{name, base, lookup}`, account; SecureStore on
  Android). Pairing again overwrites both, so the old server's token is
  lost on the phone while its row stays on the server until revoked.
  `relocate()` follows a moved address through `server.lookup`.
- **Most settings already live on the server**, so they are "per server"
  only in the sense that they go to whichever one is current: voice
  (`/speech/voice`), speech default, language and accents
  (`/settings/language`, also mirrored in localStorage), coding agents'
  keys, logins and setup (§6.6), presets, project roots (§6.24), Google
  Contacts sign-in, bridge sign-ins, persona scope, devices and pairing
  codes, Organiser setup, Connect assistant.
- **The rest is the phone's**: text size, pictures, follow-along and its
  lead, Advanced, inspectable, speak in this browser, notifications and
  alert levels (native `BackgroundNotify`, which follows `serverBase()`),
  speech here, quiet replies, calendar holds, the music app, the mic, ADB,
  and UI state (sorts, filters, drafts, recent searches, what's new). None
  of it is keyed by server.
- **Every server setting is server-wide.** `/speech/voice` is the house's
  voice, not the caller's; there is no per-person preference on a server,
  because every device is the owner (roles proposal, "One scope").

## Three kinds of setting

Settings splits into three sections, named by where the setting lives:

| Section | Who may change it | What is in it |
| --- | --- | --- |
| **This phone** | whoever holds it | text size, pictures, follow-along, speech here, quiet replies, mic, calendar holds, music app, Advanced, ADB; which servers notify (below) |
| **You on <server>** | any role but guest | your voice and speech default, your language, your presets' order, your notification levels for this server |
| **<server>** (admin) | admin only | coding agents (keys, logins, setup), presets, project roots, personas and their scope, bridges, Google Contacts, Organiser setup, devices and pairing, Connect assistant, the server's default voice |

The admin section is the roles proposal's "admin" column, rendered: a
member sees it read-only with "Ask <admin name> to change this", a guest
does not see it. The server refuses regardless (`not_admin`).

**You on <server>** is new on the server: a preference row keyed by the
device's account when it has one (`iss|sub`, so all of one person's devices
share it) and by device id otherwise, in `<state_dir>/prefs.json`. Reads
fall back to the server-wide value, so a server with one owner behaves
exactly as today. `GET/PUT /me/prefs {voice, speech_default, language,
notify}`; the existing `/speech/voice` and `/settings/language` stay, and
become the admin's server-wide default once roles exist.

## Several pairings

- **A list, not a slot.** `sasonica.chat.servers`: one entry per server,
  keyed by a server id the server already knows (its name plus the device
  id is enough; better, a stable `server_id` on `/pair` and `/me`), each
  with its own token, `base`, `lookup`, account and `role`. Tokens stay in
  SecureStore, one key per entry. `sasonica.chat.active` names the current
  one; `serverBase()` reads it. The old single record migrates into the
  list as its only entry.
- **Adding one does not drop another.** Pairing (code or account sign-in)
  appends or refreshes an entry. Unpair removes one entry and, while the
  token still works, revokes its row on that server (today the row is left
  behind).
- **A switcher.** The server's name in the header opens the list: each
  server, its role, online or not, unread count; tap to switch. Snapshots
  (IndexedDB), drafts and recent searches gain the server id in their keys
  so switching never shows one server's threads under another's name.
- **Add from your account.** Pairing gains "Your servers": the account's
  list from item 20's `/api/servers`, each with Add, which runs the
  existing account enrol (§9) against that server. One sign-in, every
  server.
- **Notifications from all of them.** `BackgroundNotify` holds a list of
  (base, token, levels) instead of one, so a "needs you" on the desk server
  arrives while the phone is showing the house server. Which servers notify
  is a This phone switch per server; the levels are You on <server>.

## What the app asks of the server

- `/me` and `/pair` answer `server_id` (stable, generated once into
  `<state_dir>/server-id`) and `role` (roles step 1).
- `GET/PUT /me/prefs` as above.
- `POST /devices/revoke` for the caller's own device (unpair), allowed to
  any role.

Nothing here touches the tailnet: each entry is reached the way it is
today, by its own address and `lookup` (docs/decisions/2026-10-18-off-the-tailnet.md).

## Order

1. **Roles step 1** (roles proposal): `role` on the row and on `/me`,
   `may_admin`. Without it there is nothing to split Settings by.
2. **Settings in three sections**, still one server: This phone, You (all
   server prefs still server-wide for now), and the admin section hidden or
   read-only by role.
3. **`server_id`, the pairing list and the switcher**, with snapshots and
   drafts keyed by server; migration of the single record.
4. **Notifications from every server.**
5. **`/me/prefs`**: per-person voice, speech default, language and levels,
   falling back to the server's.
6. **Add from Your servers** on the pairing screen.

## Open questions

- **One list or one at a time?** The switcher shows one server's threads.
  A merged Threads list (each row marked with its server) is nicer with two
  servers and noisy with five; a merged "Needs you" alone may be the middle
  way.
- **Share target and the phone's speech**: which server gets a shared file,
  and which server's replies does Speech here play when two are talking?
  Probably the active one, and "needs you" from either.
- **Language**: the app's UI language is a phone thing, the reply language
  a You thing; today one setting does both.
- **The legacy ABS token** (`sasonica.chat.token`): keep it global until
  ABS leaves, or drop it here.
- **Who is admin on a new server**: the first device paired at the desk, as
  today, or the account that installed it (the server's own enrol account)?
