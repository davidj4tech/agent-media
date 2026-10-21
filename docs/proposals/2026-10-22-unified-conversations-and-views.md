# Unified conversations and views

David, 22 Oct 2026: merge Threads and Messages, with many views drawing on
different organisation and productivity methods, including GTD, kanban and
Zettelkasten. Clarification: **not just organising work, but organising
threads of information and communication.**

Status: direction requested; design proposal only, not implemented. The
view catalogue and implementation sequence below are proposals, not settled
requirements.

## Purpose

Bring agent conversations and human messages into one place, with different
ways to follow people, ideas, subjects, ongoing conversations and work.
Information can be useful without being actionable. A family conversation,
a developing idea, a research trail and a coding session should each have
a natural place here.

Interpret “merge” as a shared collection and navigation for Threads and
Messages. Keep each conversation's identity, participants, source and
history. Combining separate transcripts into one conversation is a separate
design question.

## Existing foundations

- [Messages: spaces](2026-10-19-messages-spaces.md) already records David's
  decision that a space may contain chats, coding threads, or both.
- [Notes on plain Org](2026-09-24-notes-core-and-paragtd.md) provides general
  Org support, optional GTD profiles and org-roam linked notes.
- Roadmap item 7 covers multiple servers and a merged list with server
  filtering. This proposal adds a shared collection across conversation
  kinds and ways of viewing it.
- [Messages features](2026-10-20-messages-features.md) includes saved and
  pinned messages, reminders, voice clips and emoji reactions. These remain
  useful within a unified collection.

## One collection, many perspectives

A saved view combines a scope, filters, grouping, order and presentation.
The same conversation can appear in several views without copying its
history. Start with a few useful defaults and let people choose or make
others; no single productivity method is required.

Keep distinct concepts available: people and participants, subjects and
tags, spaces and projects, links between ideas, unread state, and optional
action state. An unread conversation is not automatically a task, and a
read one can still need a reply. Completing an action does not close the
conversation that prompted it.

Candidate views:

| Perspective | What it helps organise |
| --- | --- |
| All conversations / recent activity | Human and agent conversations together, with filters for kind, source and server |
| People and groups | Communication with a person or group across services, with each destination clearly identified |
| Spaces, subjects and tags | Family, communities, interests, businesses, research topics and other overlapping collections |
| Unread / needs a reply / waiting for someone | Communication and attention, with these states kept separate |
| Timeline / journal | How a discussion, idea or relationship developed over time |
| Linked knowledge / Zettelkasten | Notes distilled from conversations, source links, backlinks and connections between ideas |
| Outline / map | Hierarchies and connections among subjects, notes and conversations |
| Saved / reference / collections | Information to revisit without making it a task |
| GTD | Capture, clarify, next actions, waiting for, someday/maybe and review, with links to originating conversations |
| Kanban | Cards grouped by a chosen state or category, for workflows or stages of an evolving discussion or inquiry |
| PARA | Projects, areas, resources and archive across conversations and linked material |
| Agenda / calendar | Dated actions, reminders, events and conversations to revisit |
| Review / digest | Revisit selected information, unresolved discussions, developing ideas and commitments |

Notes, individual messages and extracted actions can be linked into these
views where useful. Their inclusion does not mean that all source systems
must move into one store. A knowledge note should have its own identity and
link back to the message or conversation it came from.

## Behaviour and boundaries

- Opening a conversation from any view reaches the same history.
- A view can contain human messages, agent threads, or both.
- Labels, memberships and links can overlap; filing something in one
  place need not remove it from another.
- Show the recipient and service clearly when composing. Grouping related
  conversations does not authorise forwarding between them or sharing
  their content with an agent.
- Keep source permissions and server boundaries when combining results.
- Store personal organisation separately from network read receipts,
  shared Matrix spaces and source-native task state. Define write-back
  explicitly for each action; moving a card must have a clear meaning.
- The server supplies sources, capabilities and organisation data; the
  app renders the available views, following the generic-app decision.
- Existing Threads-only and Messages-only perspectives remain available
  as filters or saved views during the transition.

## Proposed delivery

1. Design the unified collection and common metadata, including stable
   source-qualified identities. Prototype a mixed list and navigation.
2. Saved views, overlapping spaces/tags, and people/subject perspectives.
   Reuse the spaces proposal rather than build a competing grouping system.
3. Linked messages and notes, backlinks, reference collections and timeline
   views, so information organisation is useful in its own right.
4. Optional action metadata and GTD/kanban/agenda views, integrating with
   existing Org data rather than creating duplicate tasks.
5. Expand the view catalogue and add review/digest tools after trying the
   shared model with real information and communication patterns.

## Design questions to settle before implementation

- What should the combined destination be called, and where should it sit
  beside People, Organiser and Documents?
- Which views should be offered first, and how are favourites switched?
- Which organisation belongs to a person, and which can be shared?
- How should message-level links and knowledge notes be created and edited?
- Which metadata should write back to Org or Matrix, and which stays local
  to Sasonica's organisation layer?
- Is physically combining conversation histories also wanted, beyond the
  unified collection described here?
