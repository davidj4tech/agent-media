# Mail in Sasonica

David, 20 Oct 2026, after the Gmail app turned out to take no search link
and Outlook was not on the phone: "On second thought, I think I'd like to
handle emails from Sasonica." He chose **both** places (mail in Messages,
by person, *and* a Mail tab with the full inbox) and **Gmail now, Outlook
next**. Status: **plan, nothing built.** Open questions at the end.

## Why

A contact's card already lists their newest mail (contract §6.25 "Their
newest mail", `contacts_mail.py`), but a tap leaves Sasonica for a browser
tab, and replying means another app. Messages already folds a person's
SMS, Signal, Messenger, Instagram, Teams and WhatsApp rooms into one row
("people, not rooms", `docs/proposals/2026-10-17-contacts.md`); email is
the one channel still outside it.

## What there is

- **Fifteen Gmail accounts synced to red5** by mbsync (`~/.mbsyncrc`:
  davidj4, ryerorg, d4ryer, s4ryer, davidjtwo, davidj422, davidj4tech,
  davidj4artha, davidj4retail, elevenyounge, dtoshack, southpenlabs,
  davidj4food, davidj4phs, ryeremedial), each with app-password login
  (`PassCmd "cat ~/.mail-secrets/<name>"`).
- **Only Inbox, Sent and Spam sync** (`Patterns "INBOX" "[Gmail]/Sent Mail"
  "[Gmail]/Spam"`, `Expunge Both`). Archived mail (Gmail's All Mail
  without the Inbox label) is not on red5, so a person's older
  conversations are missing; `~/Mail` is 9.6 GB as it is.
- **notmuch** over `~/Mail` (`maildir.synchronize_flags=true`, no hooks).
  Tags: one per account (`personal`, `org`, `davidj4phs`, …), `inbox`,
  `sent`, `spam`, `unread`, `replied`, `flagged`, `attachment`, `signed`,
  `encrypted`.
- **Freshness:** `mail-sync.timer` runs `~/.local/bin/mail-sync` every
  10 minutes, so new mail reaches red5 up to ten minutes late.
- **Sending:** msmtp is set up for all sixteen accounts (`~/.msmtprc`,
  `smtp.gmail.com:587`, `passwordeval` from the same secrets). Nothing in
  agent-media sends mail yet.
- **agent-media reads notmuch twice already:** `contacts_mail.py` (a
  person's newest mail for the card, live, nothing stored) and
  `timeline.py`'s email adapter (counts per person per day, headers only).
  Both leave out lists, bulk mail, no-reply senders, spam and the Chats
  label, and both act for the owner's book only.
- **The app:** Messages lists threads and Matrix rooms (§6.1 "Threads",
  "Matrix rows"), folds rooms into one row per person by `/targets`'
  `contact`, and switches between a person's rooms at the top of the
  thread. Spaces are planned (`2026-10-19-messages-spaces.md`).
- **Microsoft:** the calendar plan (`2026-10-20-organiser-calendar.md`,
  "Sources" 2) already needs Microsoft Graph with a one-off app
  registration and a device-code sign-in; Outlook mail shares it.

## The shape

### On the server: a `mail` module over notmuch

- **Threads** are notmuch threads. `GET /mail/threads?q=&account=&tag=&
  before=` lists them (subject, people, last date, unread, count,
  accounts, tags), newest first, paged; the query is notmuch's own syntax
  behind the app's filters. `GET /mail/threads/<id>` gives the messages.
- **Reading safely:** each message's text part, else its HTML turned into
  sanitised HTML (an allow-list of tags, no scripts, no forms, links
  rewritten to open outside the app) and plain text for speech. **Remote
  images are off** by default, with a per-message "Show images" and a
  per-sender "always". Attachments are listed with size and type and
  fetched on tap (`GET /mail/messages/<id>/parts/<n>`), never inlined.
- **The archived-mail gap** has two answers, chosen by David (below):
  sync Gmail's All Mail into notmuch (complete and local, but several
  times the disk), or keep the local three folders and ask Gmail's API
  for search beyond them, reading an older conversation on demand.
- **Sending and replying:** msmtp is already configured, so step one
  sends through it, From chosen per account (a reply's From is the
  account it came to). Replies thread by `In-Reply-To`/`References`; the
  sent copy appears when Gmail's Sent Mail next syncs (or is added to
  notmuch at once). Drafts live on red5 (a draft per thread, not synced
  to Gmail at first). The Gmail API is the alternative if app passwords
  go away; msmtp needs nothing new.
- **State back to Gmail**, so the phone's Gmail and Sasonica agree: read
  and unread already round-trip (notmuch's `synchronize_flags` and
  mbsync); **archive** (drop `inbox`) and **delete** need Gmail label
  moves that mbsync's three folders cannot express. Either sync All Mail
  (then archive is "move out of INBOX", which mbsync carries), or use
  **lieer** (`gmi`, the Gmail API, labels as notmuch tags both ways) in
  place of mbsync for the Gmail accounts. afew can tag on arrival either
  way.
- **New mail:** the server watches notmuch after each sync (or a
  post-new hook) and publishes `mail` events on the same stream the app
  already reads (§11); a notice for mail in the Inbox from a contact, not
  for lists or bulk mail; quiet hours as the rest.
- **Privacy:** mail stays on red5; it is never sent to a model unless
  David asks in a conversation ("summarise this"), never spoken unless he
  asks, never logged (counts only, as today). Each account's mail belongs
  to the owner's book only (§6.25 "Whose book"); another account's book
  sees none of it. A household member's own mail would be their book's,
  with their own sign-in.

### Outlook, next: Microsoft Graph

The calendar plan's Graph sign-in (one app registration, a device-code
sign-in on the phone, the refresh token kept on red5) also asks for
`Mail.ReadWrite` and `Mail.Send`. Graph gives search, a `webLink` to each
message, and folder moves, so Outlook needs no local copy: the `mail`
module gets a second backend behind the same routes. The work account
(Microsoft 365) may need the organisation's IT to approve the app; the
personal outlook.com never does.

### In the app

- **Mail tab:** account switcher (all accounts, or one), the folders
  (Inbox, Sent, Starred, Archive when available, Spam), unread first or
  newest, search box (notmuch's query behind it), swipe to archive, a
  long-press for labels. A thread opens as a conversation; Reply, Reply
  all, Forward.
- **Mail in Messages, by person:** a person's email conversations are one
  more "room" on their row (a mail badge beside SMS, Messenger…), so the
  thread switcher at the top includes **Email**; the newest of their
  mail counts in the row's preview and unread. Lists and bulk mail never
  appear in Messages, only in the Mail tab.
- **Compose:** To from contacts (the picker Messages' ✎ will use), From
  per account, subject, text; attachments from the phone; Send, or save
  as a draft. The card's "Email them" opens this instead of a mailto.

## Steps (smallest useful first)

1. **Read one person's mail in Sasonica:** the card's newest mail opens
   in an in-app reader (`/mail/threads/<id>`, sanitised, images off)
   instead of a browser tab.
2. **Reply from Sasonica** through msmtp, From the account it came to;
   the sent copy shown at once.
3. **Email in Messages by person:** a person's mail as a room on their
   row, with the thread switcher's Email entry.
4. **The Mail tab:** accounts, folders, search, unread; compose new
   mail; drafts on red5.
5. **State back to Gmail:** archive, delete and labels (All Mail or
   lieer, as decided).
6. **New-mail notices** from contacts.
7. **Outlook** through Graph, with the calendar's sign-in.

## Decided (David, 20 Oct 2026)

- **Archived mail:** asked of Gmail when needed (its API), nothing extra
  stored; red5 keeps Inbox, Sent and Spam as now.
- **Sync:** labels mirrored both ways (lieer over Gmail's API), so read,
  archive and labels agree with the Gmail app.
- **Spam:** hidden; a Spam folder in the Mail tab, never in Messages or
  notices.
- **Read aloud:** only when David asks (a Play button on an email).
- **Messages:** every account's mail can sit on a person's row, and each
  account is a space (2026-10-19-messages-spaces.md) to filter by.
- **Notices:** new mail from people in his contacts only.

## Open questions for David

- **Archived mail:** sync All Mail for all fifteen accounts (complete,
  several times today's 9.6 GB), only for a few accounts, or use Gmail's
  API for anything not local?
- **mbsync or lieer** for the Gmail accounts? lieer gives labels both
  ways (archive, delete, labels match the phone's Gmail) but replaces a
  working setup; mbsync keeps it and needs All Mail for archive.
- **Spam:** hide Spam from the Mail tab, show it as a folder, or never
  sync it?
- **Read aloud:** may a reply in Sasonica read an email to him when he
  asks, and should new mail from a contact ever be spoken?
- **Which accounts in Messages:** all fifteen, or the personal ones only
  (the business accounts in their spaces, per the spaces plan)?
- **Notices:** for every contact's mail, or only some labels/people?
