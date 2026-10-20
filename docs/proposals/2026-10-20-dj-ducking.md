# Ducking like a DJ

David, 20 Oct 2026: "I wonder how it would go if instead of just plain
ducking when speech is being read that it works more like a DJ would and
adjusts the music to match the situation." And then: "how we would work for
microphone dictation too … essentially turning the user into a DJ as well".

The plain duck is one `setvol 10` before the first word and one step back
after the last, the same for every reply, blind to what is playing. A DJ
rides the fader, dips for an ident, stops for a talk, waits for the outro,
and comes back on a fresh track.

## Built (steps 1 and 2)

`MEDIA_DJ_DUCK=1` switches it on; off is the plain duck, unchanged.
`route/dj.py` chooses a **cue** before the reply starts, from what is
playing (content type) and the reply (priority, whether it asks a question,
its length estimated from the text at the device renderer's rate):

| Reply over music        | Cue                                  |
|-------------------------|--------------------------------------|
| under 4 s               | dip to 25                            |
| ordinary                | duck to 10                           |
| a question, or over 45 s| fade out and stop; resume, fade in   |
| urgent                  | duck to 10 in 150 ms                 |
| over a DJ set           | never stopped, only ducked           |
| over ambient            | duck to 4, slow fades                |
| audiobook / podcast     | paused, as before                    |

Fades: down over 400 ms as the reply starts, a 300 ms beat after the last
word, up over 1.5 s; the rooms (Snapcast clients) and the player move
together. `MEDIA_DJ_FADE_DOWN_MS`, `MEDIA_DJ_FADE_UP_MS`, `MEDIA_DJ_BEAT_MS`,
`MEDIA_DJ_SHORT_S`, `MEDIA_DJ_LONG_S`, `MEDIA_DJ_DIP_LEVEL` tune it;
`MEDIA_DUCK_VOLUME` still overrides the duck level.

The duck debt (`state.set_music_duck`) is written before the first step and
cleared only once the volume is back, so a fade cut short is paid by the next
clip as before. A backend that ducks itself (Sasonica's player, by audio
focus) declares `fades = False` and keeps its one-step behaviour.

## Next

3. **Talk in the gap.** A low or normal reply that is not a question waits
   (up to ~20 s) for the track to end, then speaks between tracks, or over
   the last seconds of the outro. Never for urgent, high or a question. The
   card shows that it is waiting, with "speak now".
4. **Pick the next track.** After a long reply, come back on the next track
   from its start rather than mid-song; with an empty queue, ask the mood
   picker for one if it has a callable form.
5. **David on the mic (talkover).** An open mic today quiets only the
   phone's own players (call_guard's MicSource, the app's MicDuck); the
   rooms keep playing into the mic. `Coordinator.talkover(on)` fades the
   music and rooms to a talkover level fast and back slowly after a short
   hold, with its own marker so it never mixes with a reply's duck; a reply
   during talkover keeps the lower level. Triggered by `POST /talkover {on}`
   from the device when its mic opens and closes (outbound, per the
   off-tailnet rule), and by the call guard's hold on the phone.
