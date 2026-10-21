# Messages: features from the apps the bridges replace

David, 20 Oct 2026: "The apps that the bridges are replacing have lots of
functionality that we're missing in the messages tab. There are lots of
other instant messengers that we could include features from too."

Draft list to go through. Nothing here is decided.

## What a room does today

- Text in and out (`matrix.Room.send` is text only).
- A photo, video, audio clip, voice note, file or location arrives as an
  italic line ("_sent a photo_"); the thing itself is not shown.
- Edits replace the words (marked edited); a deletion removes the message.
- Search, Show and Sort, archive, one row per person, Find in this thread.
  Spaces are planned (roadmap 33).

## Basics the bridged apps all have

1. **Media shown**: photos and videos inline, a voice note playable, a file
   downloadable, a location as a small map or link, stickers and GIFs.
2. **Sending media**: a photo or file from the phone; record a voice note.
3. **Reactions**: see them under a message, add one with a long press.
4. **Replies**: quote a message when answering; show the quote on replies
   that arrive.
5. **Read state on the network**: reading a chat here marks it read in
   WhatsApp/Messenger, so the phone stops nagging; ticks for sent,
   delivered, read; "typing…".
6. **Edit and unsend** my own messages; **forward** one to another chat.
7. **Per-chat controls**: mute, pin to the top, mark unread.
8. **Group info**: members, rename, picture, leave.
9. **Link previews**.

## From other messengers

10. **Send later** (Telegram, Google Messages, iMessage).
11. **Remind me about this** (Slack): a message comes back at a time.
12. **Saved / starred messages** (WhatsApp, Telegram, Signal's note to self).
13. **Mentions inbox** (Slack, Discord): every message that names me.
14. **Per-chat media gallery** (all of them).
15. **Copy the code** on a one-time-password text (Google Messages).
16. **Polls** shown and answered (WhatsApp, Telegram, Messenger).

## Things only Sasonica can do

17. **Catch me up**: a spoken summary of what came in, per chat or all.
18. **Read aloud**: incoming messages spoken in the house or the earbuds,
    with a reply by voice.
19. **Voice notes transcribed**: the words under the play button, and
    searchable.
20. **Draft a reply**: an agent suggests one from the conversation and the
    contact card; nothing is sent without a tap.
21. **Translate** a message or a whole chat.

## Chosen (David, 20 Oct 2026)

All of the basics (1–9 as groups: media, reactions and replies, read state,
per-chat controls) and all four extras offered (catch me up and read
aloud, voice notes transcribed, draft a reply, send later and remind me).
Added by David: **read without marking it read** — sometimes open a chat
and leave it unread on the network (a peek), so marking read must be
something the app can hold back, not a side effect of opening a room.

Proposed order: media shown (receive) → reactions and replies → read state
with peek → sending media → per-chat controls → voice notes transcribed →
catch me up / read aloud → draft a reply → send later / remind me.

Confirmed by David, 22 Oct 2026: in the Messages tab, offer **attach a voice
clip** to a message, including a reply to a particular message, and **react
with an emoji** on a message. Voice clips are sent as playable audio;
recording a clip is separate from dictating text. These extend sending
media (2), reactions (3) and replies (4) above; queued, not built.

Second round, all chosen (David, 20 Oct 2026):

- In a chat: jump to the first unread (a "new" line, drafts kept per
  chat), pinned messages, link previews, polls and locations (a map).
- Across chats: saved (starred) messages, a mentions inbox, a media
  gallery per chat or person, send to several chats at once.
- Housekeeping: message requests (people not in contacts held apart until
  accepted), block and report on the network, quiet hours (only chosen
  people alert overnight), copy the code on a one-time-password text.
- Extras: translate, ask about a chat ("when did Sam say the party was?"
  over all messages), follow-up nudges (chats left unanswered a day or
  two), chat lock (behind the fingerprint).
