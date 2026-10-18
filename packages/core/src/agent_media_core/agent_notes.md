# How to write for this install

Told once to every agent session agent-media hooks (intake/context.py
`notes_for`; again after a compaction), so each one behaves for a reader who
mostly *listens*, on a phone. One rule per line: `- [<when>] <rule>`.
<when> is `all`, `claude` (Claude Code's own tools), or `canvas` (the
canvas draws figures: MEDIA_SPEECH_VISUAL=1). MEDIA_AGENT_NOTES=0 turns them
off; MEDIA_AGENT_NOTES_FILE points at a site's own copy of this file.

- [all] Your replies are read aloud in full. Lead with the answer, aim for under a minute (about 150 words), and put long tables, file lists or logs in a file and say where.
- [all] Put anything the user has to copy (a command, a URL to paste, a value) in its own fenced code block: they copy from a phone.
- [all] Give places to go as markdown links with full https URLs, not bare host or file names.
- [all] Before ending a turn on a background wait, give the watcher a deadline and make it report either way, so a turn never ends on a check that can fail silently.
- [claude] Give every Bash call a plain-words `description`: the phone shows it as the step being run.
- [claude] Ask questions as AskUserQuestion cards, recommended option first; they can be answered with a tap. Prose written just before the card is spoken with it: keep it to a couple of sentences and never speak it yourself.
- [canvas] A reply can carry a figure: `[[visual: <description>]]` is drawn beside it, `[[reveal: <description>]]` also pauses the speech until it is on screen. Use one only when the picture carries meaning, and describe it concretely (boxes, labels, arrows).
