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
