# Decision: the canvas carries the reply's words, not ambient art

Status: **accepted**
Date: 2026-09-21

## The rule

The canvas, and the pictures attached to a reply, show information: the
sentence being spoken, the reply's numbers paired with what they count, the
filenames it named, and a progress rule. A generated metaphor scene per
reply is decoration and is not the direction.

A new canvas feature should answer "what does this tell the listener that
the voice did not?".

## Why

David, 21 Sep 2026, judged the per-reply ambient artwork "pretty
unnecessary". A screen across the room is good for the part of a reply that
is hard to catch by ear, not for a picture of it. Each ambient image also
cost a model call to shape it.

## What it means in config

- `MEDIA_VISUAL_PATTERN_STYLE=card` (the default) sets the reply's own
  words, figures and filenames over the art, with the motif dimmed behind
  it. `art` brings back the motif alone.
- `MEDIA_VISUAL_AMBIENT=0` (22 Sep 2026) turns ambient pictures off at the
  source: a reply gets a picture only when it asks for one with a
  `[[visual:]]` or `[[reveal:]]` marker. The code default is still `1`.
- A marked figure is the exception: it was asked for on purpose, so it still
  earns a model (`MEDIA_VISUAL_FIGURE_ENGINE`).
- On e-ink (`MEDIA_VISUAL_MONO=1`) the card is type alone, with no motif.
