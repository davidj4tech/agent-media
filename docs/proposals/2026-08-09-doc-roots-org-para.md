# Proposal: where document playback meets GTD, PARA, org-roam and Denote

Status: **built** (org support, roots and `media doc agenda` landed 2026-08-09)
Date: 2026-08-09

Now that documents can be listened to, the question is which documents — and
the honest answer is that agent-media should not have an opinion about how
they are organised.

## What already exists

`~/org/productivity-system.org` documents a four-model system, and it is
explicit that the models do different jobs:

- **GTD** — what requires attention or action?
- **PARA** — where does this belong?
- **org-roam** — what ideas, people and references connect here?
- **tickler** — when should this reappear?

In practice: GTD files at the top of `~/org` (inbox, next-actions,
waiting-for, someday, tickler, areas, projects, journal), **882 org-roam
notes** under `~/org/roam/{journal,notes,people,projects,refs}`, and
`~/org/denote/` — which is **empty**. Denote is planned, not adopted.

## The repo's docs/ layout is not a rival to any of this

`docs/` organises by lifecycle — reference, decisions, proposals, notes,
handover — and that is scoped to *this repository*. In PARA terms the whole
tree is a Resource, with `proposals/` behaving like Projects and `decisions/`
like an Archive that stays authoritative. It maps cleanly precisely because it
is small and local.

It should stay that way. A repo's documents belong with the repo; merging them
into the personal system would put agent-media's design notes somewhere they
only make sense to one reader.

## Where the two should meet: the player, not the filesystem

`MEDIA_DOC_ROOTS` is a colon-separated list, deliberately. The connection is
one line of config —

```
MEDIA_DOC_ROOTS=<repo>/docs:~/org:~/org/roam
```

— and then agent-media is a *reader* for the system that already exists,
rather than a second place to put things. What it needs to earn that:

1. **Org markup in the projection.** Today it speaks markdown. Org needs
   `*`-headings as chapter marks, `#+begin_src`/`#+begin_example` announced
   the way fenced code is, org tables announced, `[[link][text]]` reduced to
   its text, and drawers/properties dropped entirely.
2. **`#+title:` and `#+filetags:` as metadata**, the way `Status:`/`Date:` are
   read now.
3. **Selection by tag, not by folder.** This is the important one. PARA
   membership is expressed as filetags and roam links, and a note routinely
   belongs to several places at once — which is exactly what a directory
   cannot express. So `media doc list --tag para-resource` rather than a
   directory-shaped picker. The current `kind` column is a folder name; for
   the org roots it should be the tags.

## On Denote

The empty `~/org/denote/` is worth a decision rather than a drift. Denote's
filename convention —

```
20260809T075902--speech-state-convergence__decision_agent-media.org
```

— carries a stable ID, a title slug and keywords *in the filename*, which
means a picker can list and filter without opening 882 files. That is
precisely what `list_docs()` wants, and it is a stronger version of the
`YYYY-MM-DD-slug.md` convention the repo docs just adopted. (agent-media
already names its speech clips `remote-20260809T074323-21510.mp3` — the same
timestamp-as-identity idea, arrived at separately.)

If Denote is adopted, agent-media should read that convention directly and
skip content parsing for those roots. If it is not, the directory should go,
because an empty directory in a documented system reads as a fifth model
nobody is using.

## The higher-value target: GTD's daily surfaces, not the reference shelf

Reading arbitrary notes aloud is the obvious feature and the least useful one.
A reference note is consulted when you are already at a screen, looking for
one specific thing — the worst possible case for audio, which is linear.

The GTD surfaces are the opposite: **short, time-bound, and wanted while your
hands are busy.** "What's on today", "what am I waiting for", "what does the
tickler have for me" — each is a minute of speech, has a natural moment, and
answers a question you actually have away from the desk.

That argues the next thing to build is not a bigger projection but a different
adapter: `media doc agenda`, speaking org-agenda's own output (TODO states,
scheduled/deadline dates) rather than the text of the files. Same player, same
book channel, but the content comes from Emacs' agenda machinery, which
already knows what today means.

## agent-sessions is a different problem

Session transcripts are documents in the sense that they are text, and not at
all in the sense that matters here: they are long, repetitive, and full of
tool output. Projecting one to speech would produce an hour of unlistenable
audio. What is wanted from a session is a *summary* — "what did we decide,
what changed, what is left" — which is a generation problem, not a formatting
one, and should not be folded into the doc projection.

The natural shape is the other direction: agent-sessions writes a summary into
`~/org/roam/journal/`, and doc playback picks it up for free as an org note,
like anything else in the system.

## Open questions

- Adopt Denote, or delete the empty directory?
- Does the picker want one flat list across all roots, or a root-first
  chooser? 882 notes plus a repo tree is a lot for one fzf list, though tag
  filtering may make it moot.
- ~~Should `media doc agenda` speak the agenda, or a summary?~~ **Summary.**
  The raw agenda is 322 entries, 153 past their date and 234 of them transits
  — twenty minutes of noise. It is a briefing with spoken caps, and
  `astro.org` gets its own skippable chapter so generated entries stop
  crowding real commitments out of every capped list.
