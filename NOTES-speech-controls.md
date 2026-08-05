# Speech breadcrumb + control channel — OpenWebUI STT integration idea

The new speech-state breadcrumb (GET /speech peek, non-queueing) and control channel
(interrupt/supersede barge-in, flush, timed hold) are a natural fit for the OpenWebUI STT work:

- Breadcrumb -> STT knows when the agent is SPEAKING vs SILENT, so it can avoid transcribing
  the agent's own output and avoid talking over itself.
- Barge-in (--urgent --supersede) -> the moment STT detects the user starting to speak, fire
  an interrupt that both stops current speech AND drops/replaces the queued reply. Real
  full-duplex barge-in, not push-to-talk.

TODO: wire the OpenWebUI STT side directly into these endpoints rather than reinventing a
speaking-state signal.

## RESOLVED 2026-08-05 (was: version the ducker script)
Premise was wrong — speech-state-server.py and its systemd unit are ALREADY
versioned, in ~/dotfiles/packages/voice/, stow-symlinked into ~/.local/bin and
~/.config/systemd/user/. Nothing to move. See DECISION-speech-state-convergence.md.

Still open (separate idea): add a "local_audio": bool field to canvas /speech
(cheap core-idle unix-socket read) for new consumers; leave :8675 untouched.

## Operating pattern (agreed 2026-08-05): manage the speech channel, don't talk over it
David's ask: when TTS is playing, the voice agent should not barrel ahead in
parallel. Use the breadcrumb (canvas /speech `speaking`) to know when speech is
active, and before speaking either (a) wait for it to finish, or (b) actively
pause/flush it (media pause / speech-flush, or --urgent/--supersede) so the two
voices never fight. This is the intended payoff of the breadcrumb + control
channel work — treat speech as something to manage, not a parallel stream.

## Refinement (2026-08-05): the user talking to CeCe IS the barge-in signal
No STT needed for the common case. When David speaks to CeCe in the voice chat,
that inbound turn is itself the "user is speaking" signal. Reflex: at the START
of CeCe's turn, if canvas /speech shows speaking=true (e.g. Claude Code TTS on
the phone), pause it (media pause) BEFORE replying, so the phone speech doesn't
compete with the live conversation. STT-triggered barge-in is only needed for
detecting speech that never routes through CeCe.
