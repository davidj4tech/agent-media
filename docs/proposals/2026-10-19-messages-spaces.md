# Messages: spaces

David, 19 Oct 2026: "I think we need a way to better manage the messages
tab. maybe we can use spaces as well? What do you think?" Then "y" to a
written plan before any code. Status: **plan only.**

## Why

Messages holds every bridged chat at once: SMS, Messenger, Instagram,
Teams and soon WhatsApp. That's 105 rooms on David's account today, and
growing as group chats and new people arrive. Show, Sort, the box and
"one row per person" (17–19 Oct) narrow the list, but they have no way to
say *these chats belong together*: family, a business, a workplace. Matrix
already has that idea, the **space**. A space is a room that lists other
rooms. It's shared with every Matrix app, so a grouping made here shows up
in Element too, and the reverse.

## What there is

- **The server holds bridge rooms as threads** (`matrix.py`, docs
  2026-09-23-matrix-in-the-app.md). The owner's loop (`_Owner`) takes in
  every room a bridge in `MATRIX_OWNER_BRIDGES` is in (`discover()`,
  `_invited()`). `rows()` makes the §6.1 "Matrix rows" for `/targets`, each
  with its `network` (from `m.bridge` state, `_network()`) and its
  `contact` (§6.25).
- **No space state is read.** Nothing in `matrix.py` looks at
  `m.room.create`'s `type`, `m.space.child` or `m.space.parent`. Because
  `discover()` takes any room a bridge bot is in, **the bridges' own space
  rooms are probably being taken in as empty threads** today. That's worth
  checking and fixing first, whatever else is decided.
- **The spaces on ryer.org today** (read-only, 19 Oct; names and child
  counts only):

  | Space | Rooms | Made by |
  |---|---|---|
  | Google Messages (davidj4@gmail.com) | 34 | gmessages bridge |
  | Facebook Messenger (David J Toshack) | 28 | Messenger bridge |
  | Instagram (David J T) | 3 | Instagram bridge |
  | Peninsula Hot Springs | 21 | Teams bridge |
  | PHS- Peninsula Hot Springs (Fingal) | 3 | Teams bridge |
  | PHS & MEHS - All Employees | 1 | Teams bridge |
  | PHS - Spa Therapy | 1 | Teams bridge |

  91 of the 105 rooms sit in at least one space. Teams brings its own
  structure: an organisation space with a space per team inside it.
  WhatsApp gets one when it's signed in. **David has made no spaces of his
  own yet.**
- **The app's Messages** (`app/lib/messagesView.ts`, `routes/threads.tsx`):
  - Show: Active, Unread, Archived, Everything, plus one service, the same
    choice as the existing **service chip strip**.
  - Sort: Most recent, Unread first, Name A–Z, By service.
  - The box filters by name or last message.
  - People folds a person's rooms into one row.
- **Contact labels** (§6.25 "Labels"): Google's contact groups and "My
  Contacts" on each person, with Any/All filtering in People.

## The shape

**Spaces become data the server answers. The app stays generic and draws
what it's given.**

1. **Read spaces.**
   - The owner's loop reads `m.room.create` `type: m.space` and each
     space's `m.space.child` events, and keeps a small map: space → name,
     avatar, children (rooms and sub-spaces), and who made it (a bridge,
     or David).
   - A space room is never a thread.
   - Each Matrix row gains `spaces: [id…]`.
   - A new `GET /spaces` lists them: id, name, avatar, parent, count,
     unread count, and `kind: bridge | own`, in a stable order: David's
     own first, then the bridges'.
2. **Make and change spaces, as the owner, through the homeserver.**
   - `POST /spaces {name}` creates one, owned by David's account, so it's
     his in every Matrix app.
   - `POST /spaces/<id>/rooms {room, add|remove}` writes or clears the
     `m.space.child` state (with `via`), plus the room's `m.space.parent`
     where allowed.
   - A room can sit in several spaces.
   - Bridge spaces are left as the bridges keep them: read-only to us, as
     the bridges rewrite them.
3. **Label → space rules.**
   - A rule ties a contact label to a space ("Family" → Family). When a
     person carries the label, their rooms are proposed for, or added to,
     the space (open question below).
   - Rules live per book (`contacts.json` / a `space_rules` table) and are
     re-checked after each contacts sync.
   - Rooms are only ever *added* by a rule. Removing one stays a person's
     act.
4. **Gated like the rest.** Owner only for creating and changing. Every
   account reads only the spaces of rooms in its own book. Contract §6.1
   and a new §6.27.

## In the app

- **A chip row across the top of Messages:** All · Unread · then spaces
  (David's own, then the bridges'), each with its unread count. Tap to
  narrow the list, which keeps its Sort, box and People folding.
- **The chip row replaces today's service strip.** A bridge's space *is*
  its service, so one row does both.
- A chip's long-press offers Rename, Remove from the row, and Edit rooms.
- A room's ⋮ gets **Add to space…**: tick the spaces it belongs in.
- **+ Space** at the end of the row makes one.
- Teams' nesting (organisation → team) shows as one chip that opens its
  sub-spaces, so the row stays short.
- Business spaces (South Pen Labs, Rye Remedial) are ordinary own spaces.
  They're a lighter first step for "business profiles in Messages"
  (2026-10-17-contacts.md). They become separate accounts only if a
  business needs its own sign-ins or its own people.

## Steps

1. **Stop space rooms showing as threads.** Read `m.room.create`'s type in
   the owner's loop and skip space rooms. A fix on its own, and the
   smallest useful change.
2. **Read spaces:** `spaces` on Matrix rows, `GET /spaces`. Then the
   app's chip row from the bridges' spaces, replacing the service strip.
   *Gain: Messenger, SMS, Instagram and Teams as chips, Teams' teams as
   sub-chips.*
3. **Own spaces:** create, add or remove rooms, Add to space…, + Space.
   *Gain: Family, South Pen Labs, Rye Remedial.*
4. **Label → space rules**, after David answers how they should act.

## Decided (David, 19 Oct 2026)

- **Chats, threads or both:** "it would be good to have the option for
  either or". A space can hold chats, coding threads, or both; each space
  says which. South Pen Labs can gather its chats and its coding threads.
- **Label rules move chats automatically.** A rule "Family label → Family
  space" keeps the space filled; taking a chat out by hand sticks and the
  rule never puts it back.
- **Unread is a chip; Archived stays in Show.**
- **Unread at a glance:** "it would also be good to see which ones have
  unread messages at a glance". Every space chip carries its unread count
  (a badge; none when nothing is unread), the bridges' spaces included.
  Bridge spaces are shown; David's own spaces come first, then the
  bridges'; each can be hidden.

## Open questions for David

- **Coding threads:** should spaces hold coding threads too (a "South Pen
  Labs" space with its projects' threads), or stay Messages-only? Matrix
  spaces can only hold rooms, so coding threads would need a
  Sasonica-only grouping beside them.
- **Unread and Archived:** chips in the row, or kept in the Show menu?
  The plan has Unread as a chip and Archived in Show.
- **Label rules:** should they move rooms into a space automatically, or
  only suggest ("3 Family chats aren't in Family yet — add them?")?
- **Bridge spaces:** shown as chips by default, or opt-in per bridge?
- **Order:** David's own spaces first, or most recently active first?
