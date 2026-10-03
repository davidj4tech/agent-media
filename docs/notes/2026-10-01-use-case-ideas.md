# Use-case ideas David liked

Status: **ideas, nothing built**
Date: 2026-10-01

From a brainstorm of what Sasonica could be used for. These two got a
reaction; everything else from that session was left out on purpose.

## Where you go decides what your agents do

The house knows where you are and roughly how long you will be away (rough
location from the phone; "speaks when you're free"). Arriving at the beach,
it starts the long jobs it has been saving; heading home, it has them done
and summarises them in the car.

Builds on: docs/proposals/2026-10-01-the-phone-as-eyes-and-hands.md
(location), docs/proposals/2026-10-01-speaks-when-youre-free.md.

## Your energy decides what gets asked of you

Agents time their asks to the state you are in: hard design questions when
you are fresh, only yes/no taps when you are tired. The signal: time of day,
sleep, and your voice — pace, pauses, flatness — compared only with your own
usual self. Runs on red5, never in the cloud; opt-in; it only decides *when*
to ask, never *what* to do.

Voice chat already hears you continuously, so the voice signal comes nearly
free (docs/proposals/2026-09-22-voice-chat.md).

## Weather and season shape the work

Not just the music: the first sunny weekend, agents put off screen-heavy
jobs and suggest something outdoors (the garden gadget); a rainy week, they
line up the deep refactors you'd want to sit with. astrotunes already reads
Melbourne's weather and the season.
