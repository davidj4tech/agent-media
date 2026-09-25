"""A thread as the terminal sees it: messages built from the agent's transcript.

`/conversation/log` used to be built from speech history, so a reply reached
the phone only once it had been queued and rendered for speech — behind any
other speech waiting its turn — and only as the words that were spoken. The
terminal has the reply the moment it is written, with the steps and the
narration around it. This reads the same file the terminal is drawn from and
hands the phone *messages*:

    {"id": "<uuid of the message's first record>",
     "role": "user" | "assistant",
     "at": <epoch, 3 dp>,
     "parts": [{"type": "text", "text"},
               {"type": "reasoning", "text", "redacted": bool},
               {"type": "tool", "name", "title", "input_summary", "status",
                "result_summary", "tool_use_id"},
               {"type": "ask", "ask": [...], "status", "answer", "tool_use_id"}],
     "spoken": null | {"id", "key", "at", ...},     # joined in `join_speech`
     "turn": {"running": bool},
     "command": {"name", "args", "text"}}           # user slash commands only

Speech is joined on afterwards (`join_speech`): the spoken row's id (for
replay), its dedup key (for its pictures) and, while it plays, the
follow-along clock.

**Claude Code, Codex and pi**, each with a reader in `READERS`: a fold from
that harness's records into the messages above, plus the two cheap tests the
backwards scan makes on a raw line. Hermes keeps its conversations in a
database rather than a file, so its threads are still made from their spoken
lines (`messages_from_lines`), text only. A Builder is fed one record at a
time — which is also the shape a headless session's stream-json events have
(the spike, `docs/notes/2026-09-22-headless-spike.md`: one `assistant` event
per content block, `user` events for tool results), so the same fold serves
both.

**What is left out.** The transcript holds a great deal the terminal never
draws, and some it draws that is not the conversation:

- `isMeta` records (a skill's body, a caveat), sidechain records (a
  subagent's own turns — the subagent is one tool part, `Agent`), compact
  summaries, attachments (hook output, reminders, file snapshots) except a
  message typed while a turn ran (`queued_command`), and every bookkeeping
  record type (`mode`, `ai-title`, `file-history-*`, `cost-state`, …).
- The harness's own asides in the prompt stream: task notifications, system
  reminders, a local command's output. `strip_system_blocks` is the same
  rule the speech path uses. What is left of such a prompt is a message;
  nothing left means none, but the turn it starts is still a new message.
- Settings slash commands (`/model`, …), by `slash.is_settings` — the same
  rule the spoken lines follow. Other commands are a user message carrying
  the `command` chip.
- Tool inputs and results are **summaries**, never contents: a Bash command
  line, a path and line counts for Read/Edit/Write, the first few hundred
  characters of a result. `toolUseResult` (which carries whole files, before
  and after) is never read.

**Thinking.** In the transcripts on red5 (Sep 2026, Claude Code 2.1.26x–
2.1.278) almost every `thinking` block is signature-only: `"thinking": ""`
with the reasoning held server-side. Those become one
`{"type": "reasoning", "text": "", "redacted": true}` per run (consecutive
ones fold into one). The blocks that do carry text are the model's short
running narration between tool calls ("Found the route table; next I'll…"),
and they are shipped as written. `redacted_thinking` blocks are redacted
parts too.

**Cost.** A transcript is up to ~11 MB. Per file, the answer is cached with
the inode and the byte offset it was read to; transcripts are append-only,
so a growing file costs only its new bytes, and a file that shrank, was
replaced or was rewritten in place (`_seam`) is read again. A file seen for
the first time is read **from the end**, far enough back for `TAIL_PROMPTS`
prompts, and the rest only if a caller asks for older messages. Lines that
cannot be a message (most of a transcript by bytes: attachments, file
snapshots) are skipped without being parsed.

Read-only: nothing here ever writes to a transcript.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import threading
from datetime import datetime, timezone

#: How many characters of a tool's input or result a part carries.
SUMMARY_MAX = 300
#: A text part longer than this is cut (a reply is rarely a tenth of it).
TEXT_MAX = 32 * 1024
#: Narration longer than this is cut.
REASONING_MAX = 8 * 1024
#: A file seen for the first time is read back this many prompts from its end.
TAIL_PROMPTS = 60
#: Read size when walking backwards.
_CHUNK = 256 * 1024
#: Bytes kept before the read offset to tell an append from a rewrite.
_SEAM = 256
#: Files whose parse is kept. A canvas watches a handful of threads at once;
#: the bound only keeps a long-running process from growing without limit.
_CACHE_MAX = 32

#: A line that has none of these cannot be a message, and is not parsed.
#: Claude Code writes compact JSON, so the top-level `type` is spelled exactly
#: this way; a nested match only costs a parse.
_WANT = (b'"type":"user"', b'"type":"assistant"', b'"type":"system"', b'"queued_command"')
#: The system records that end a turn.
_TURN_END = {"turn_duration", "stop_hook_summary"}
#: What the harness writes into the prompt stream when a turn is cut short.
_INTERRUPTED = re.compile(r"^\[Request interrupted by user[^\]]*\]$")


def epoch(ts) -> float | None:
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return round(dt.timestamp(), 3)


def _cut(text: str, n: int) -> str:
    text = str(text or "")
    return text if len(text) <= n else text[: n - 1] + "…"


def _one_line(text: str, n: int = SUMMARY_MAX) -> str:
    return _cut(" ".join(str(text or "").split()), n)


def _nlines(text: str) -> int:
    text = str(text or "")
    return text.count("\n") + 1 if text else 0


# --- summaries --------------------------------------------------------------------


def input_summary(name: str, inp: dict) -> str:
    """What a tool was asked to do, in a line or two. Never a file's contents."""
    inp = inp if isinstance(inp, dict) else {}
    base = lambda k: str(inp.get(k) or "")  # noqa: E731
    if name == "Bash":
        cmd = inp.get("command")
        if isinstance(cmd, list):
            cmd = " ".join(str(c) for c in cmd)
        return _cut(str(cmd or "").strip(), SUMMARY_MAX)
    if name == "Read":
        path = base("file_path") or base("path")
        if inp.get("offset") or inp.get("limit"):
            start = int(inp.get("offset") or 1)
            end = f"{start + int(inp['limit']) - 1}" if inp.get("limit") else "end"
            return _one_line(f"{path} (lines {start}–{end})")
        return _one_line(path)
    if name in ("Edit", "MultiEdit"):
        path = base("file_path") or base("path")
        edits = inp.get("edits") if isinstance(inp.get("edits"), list) else [inp]
        old = sum(_nlines(e.get("old_string")) for e in edits if isinstance(e, dict))
        new = sum(_nlines(e.get("new_string")) for e in edits if isinstance(e, dict))
        return _one_line(f"{path} (−{old} +{new} lines)")
    if name == "Write":
        return _one_line(f"{base('file_path') or base('path')} "
                         f"({_nlines(inp.get('content'))} lines)")
    if name == "NotebookEdit":
        return _one_line(base("notebook_path"))
    if name in ("Grep", "Glob"):
        where = base("path")
        return _one_line(base("pattern") + (f" in {where}" if where else ""))
    if name == "WebFetch":
        return _one_line(base("url"))
    if name == "WebSearch":
        return _one_line(base("query"))
    if name in ("Agent", "Task"):
        kind = base("subagent_type")
        return _one_line(base("description") + (f" ({kind})" if kind else ""))
    if name == "Skill":
        return _one_line(" ".join(x for x in (base("skill"), base("args")) if x))
    if name == "TodoWrite":
        todos = inp.get("todos") if isinstance(inp.get("todos"), list) else []
        return f"{len(todos)} todos"
    try:
        return _cut(json.dumps(inp, ensure_ascii=False, separators=(",", ":")), SUMMARY_MAX)
    except (TypeError, ValueError):
        return ""


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                out.append(str(b.get("text") or ""))
            elif b.get("type") == "image":
                out.append("[image]")
        return "\n".join(out)
    return ""


def result_summary(name: str, content) -> str:
    """What came back, cut short. A Read answers with how much it read, not
    what: the file is the one thing never to ship."""
    text = _result_text(content)
    if name == "Read" and text and not text.startswith(("Error", "<tool_use_error>")):
        return f"{_nlines(text.rstrip())} lines"
    return _cut(text.strip(), SUMMARY_MAX)


def _title(name: str, inp: dict) -> str:
    """The step as the phone already words it ("Read canvas.py")."""
    try:
        from agent_media_core import activity

        return activity.describe(name, inp) or name
    except Exception:  # noqa: BLE001 — a title is a nicety
        return name


def _ask_of(inp: dict) -> list:
    """AskUserQuestion's questions in the shape spoken lines carry (§6.2)."""
    out = []
    for q in (inp or {}).get("questions") or []:
        if not isinstance(q, dict):
            continue
        out.append({"question": str(q.get("question") or ""),
                    "options": [{"label": str(o.get("label") or ""),
                                 "description": str(o.get("description") or "")}
                                for o in q.get("options") or [] if isinstance(o, dict)],
                    "multiSelect": bool(q.get("multiSelect"))})
    return out


def _answer_of(content) -> str:
    try:
        from agent_media_core.intake.hook_claude_code import answers_from_response
    except Exception:  # noqa: BLE001
        return _one_line(_result_text(content))
    return answers_from_response(_result_text(content)) or _one_line(_result_text(content))


# --- the fold ---------------------------------------------------------------------


#: `<cross-session-message from="…" from-name="agent-media-71" …>text</…>`,
#: possibly followed by the harness's own notes about it.
_PEER = re.compile(r'^<cross-session-message\b(?P<attrs>[^>]*)>\s*(?P<body>.*?)\s*</cross-session-message>', re.S)
_PEER_NAME = re.compile(r'\bfrom-name="([^"]*)"')


def _user_text(content) -> tuple[str, bool]:
    """(the prompt's words, whether it carries tool results)."""
    if isinstance(content, str):
        return content, False
    texts, results = [], False
    for b in content or []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "tool_result":
            results = True
        elif b.get("type") == "text":
            texts.append(str(b.get("text") or ""))
    return "\n".join(texts), results


def is_boundary(rec: dict, sidechain: bool = False) -> bool:
    """A record that starts a new turn: a prompt, not a tool result. Reading
    from one of these on, every tool result has its tool call in view.
    `sidechain`: the file is a subagent's own (agents.py), whose every record
    is a sidechain one."""
    if rec.get("type") != "user" or rec.get("isMeta") \
            or (rec.get("isSidechain") and not sidechain):
        return False
    text, results = _user_text((rec.get("message") or {}).get("content"))
    return bool(text.strip()) and not results


class Builder:
    """Folds transcript records, in file order, into messages.

    Feed it every record (`feed`); read `messages` at any point. A message is
    changed in place as later records arrive (a tool's result, the next block
    of the same turn), which is what an incremental reader wants: the caller
    diffs by `id`.
    """

    def __init__(self, sidechain: bool = False) -> None:
        #: Read sidechain records instead of skipping them: a subagent's own
        #: transcript (`subagents/agent-<id>.jsonl`) is nothing but.
        self.sidechain = sidechain
        self.messages: list[dict] = []
        self._cur: dict | None = None          # the assistant message still open
        self._stop: str | None = None          # its last stop_reason
        self._tools: dict[str, tuple[str, dict]] = {}
        self._seen: set[str] = set()

    # -- helpers --

    def _close(self, interrupted: bool = False) -> None:
        cur = self._cur
        if cur is None:
            return
        cur["turn"]["running"] = False
        for p in cur["parts"]:
            if p.get("status") == "running":
                p["status"] = "error" if interrupted else "done"
                if interrupted and not p.get("result_summary"):
                    p["result_summary"] = "interrupted"
        self._cur = None
        self._stop = None

    def _user(self, rec: dict, text: str, command: dict | None = None) -> None:
        self._close()
        # A queued prompt and its echo, or a record repeated by a resume:
        # the same words twice in a row are one message.
        last = self.messages[-1] if self.messages else None
        if last and last["role"] == "user" and last["parts"] \
                and last["parts"][0].get("text") == text:
            return
        msg = {"id": str(rec.get("uuid") or ""), "role": "user",
               "at": epoch(rec.get("timestamp")) or 0.0,
               "parts": [{"type": "text", "text": _cut(text, TEXT_MAX)}],
               "spoken": None, "turn": {"running": False}}
        if command:
            msg["command"] = command
        self.messages.append(msg)

    def _assistant_msg(self, rec: dict) -> dict:
        if self._cur is None:
            self._cur = {"id": str(rec.get("uuid") or ""), "role": "assistant",
                         "at": epoch(rec.get("timestamp")) or 0.0, "parts": [],
                         "spoken": None, "turn": {"running": True}}
            self.messages.append(self._cur)
        return self._cur

    # -- records --

    def feed(self, rec: dict) -> None:
        if not isinstance(rec, dict) or (rec.get("isSidechain") and not self.sidechain):
            return
        uid = rec.get("uuid")
        if uid:
            if uid in self._seen:
                return
            self._seen.add(uid)
        kind = rec.get("type")
        if kind == "assistant":
            self._feed_assistant(rec)
        elif kind == "user":
            self._feed_user(rec)
        elif kind == "system":
            sub = rec.get("subtype")
            if sub in _TURN_END or sub == "compact_boundary":
                self._close()
        elif kind == "attachment":
            att = rec.get("attachment") or {}
            # A message typed while a turn was running. Claude Code wrote the
            # prompt as a string and now writes it as content blocks; either
            # is the same message, and dropping the second kind lost every
            # mid-turn message from the thread — they survived only as spoken
            # lines, which a reader then saw placed by when they were read
            # aloud rather than by when they were sent.
            if att.get("type") == "queued_command":
                text = att.get("prompt")
                if isinstance(text, list):
                    text = _user_text(text)[0]
                if isinstance(text, str) and text.strip():
                    self._prompt(rec, text)

    def _feed_assistant(self, rec: dict) -> None:
        m = rec.get("message") or {}
        blocks = m.get("content")
        if isinstance(blocks, str):
            blocks = [{"type": "text", "text": blocks}]
        msg = self._assistant_msg(rec)
        # Running until `settle` sees the turn end (or a later record closes it).
        msg["turn"]["running"] = True
        parts = msg["parts"]
        for b in blocks or []:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text":
                text = str(b.get("text") or "")
                if text.strip():
                    parts.append({"type": "text", "text": _cut(text, TEXT_MAX)})
            elif t in ("thinking", "redacted_thinking"):
                text = str(b.get("thinking") or "") if t == "thinking" else ""
                if text.strip():
                    parts.append({"type": "reasoning", "text": _cut(text.strip(), REASONING_MAX),
                                  "redacted": False})
                elif not (parts and parts[-1].get("type") == "reasoning"
                          and parts[-1].get("redacted")):
                    # A run of signature-only blocks is one "thought".
                    parts.append({"type": "reasoning", "text": "", "redacted": True})
            elif t == "tool_use":
                name = str(b.get("name") or "")
                inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                tid = str(b.get("id") or "")
                if name == "AskUserQuestion":
                    part = {"type": "ask", "ask": _ask_of(inp), "status": "running",
                            "answer": "", "tool_use_id": tid}
                else:
                    part = {"type": "tool", "name": name, "title": _title(name, inp),
                            "input_summary": input_summary(name, inp),
                            "status": "running", "result_summary": "",
                            "tool_use_id": tid}
                parts.append(part)
                if tid:
                    self._tools[tid] = (name, part)
        self._stop = m.get("stop_reason") or self._stop

    def _feed_user(self, rec: dict) -> None:
        content = (rec.get("message") or {}).get("content")
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    got = self._tools.pop(str(b.get("tool_use_id") or ""), None)
                    if not got:
                        continue
                    name, part = got
                    part["status"] = "error" if b.get("is_error") else "done"
                    if part["type"] == "ask":
                        part["answer"] = _answer_of(b.get("content"))
                    else:
                        part["result_summary"] = result_summary(name, b.get("content"))
        if rec.get("isMeta") or rec.get("isCompactSummary"):
            return
        text, results = _user_text(content)
        if results or not text.strip():
            return
        self._prompt(rec, text)

    def _prompt(self, rec: dict, text: str) -> None:
        from agent_media_core import slash
        from agent_media_core.intake._text import strip_system_blocks

        stripped = text.strip()
        if _INTERRUPTED.match(stripped):
            self._close(interrupted=True)
            return
        cmd = slash.parse(stripped) if ("<command-name>" in stripped
                                        or stripped.startswith("/")) else None
        if cmd:
            if slash.is_settings(cmd["name"]):
                self._close()
                return
            self._user(rec, cmd["text"], cmd)
            return
        peer = _PEER.match(stripped)
        if peer:
            # Another Claude session's message, delivered into this one: it
            # reads as the listener's own bubble with the raw tags in it
            # otherwise. Kept as a user-side message, marked with who sent it.
            body = " ".join(peer.group("body").split())
            self._user(rec, body)
            if self.messages and self.messages[-1]["parts"][0].get("text") == _cut(body, TEXT_MAX):
                name = _PEER_NAME.search(peer.group("attrs"))
                self.messages[-1]["peer"] = {"name": (name.group(1) if name else "") or "another session"}
            return
        words = strip_system_blocks(stripped)
        if not words:
            # A task notification, a reminder, a local command's output:
            # nobody said it, but what follows is a new turn.
            self._close()
            return
        self._user(rec, words)

    # -- the answer --

    def settle(self) -> None:
        """Mark the open message's turn as finished if its last block ended
        the turn (`end_turn`) and it is waiting on no tool."""
        cur = self._cur
        if cur is not None and self._stop not in (None, "tool_use") \
                and not any(p.get("status") == "running" for p in cur["parts"]):
            cur["turn"]["running"] = False

    def close_open(self) -> None:
        """What a reader that knows the next record is a prompt does."""
        self._close()



# --- Codex and pi: the same messages, from their own transcripts -----------------
#
# Each of these folds one harness's records into the message shape above, so a
# client reads one format whatever wrote the conversation. They are fed the
# same way the Claude builder is (`_read` → `feed`), and they answer the same
# three questions: what is a message, what ends a turn, and which raw line
# starts one (`Reader.wants`/`prompt_hint`, the cheap tests the backwards scan
# makes before parsing anything).


def _norm_tool(name: str) -> str:
    """A harness's name for a tool, as Claude Code spells it, so the summaries
    above ("Read canvas.py (lines 1–40)") serve every harness. Unknown names
    are left alone."""
    from agent_media_core import activity

    return activity.canonical_tool(name)


def _tool_part(name: str, inp, tid: str) -> dict:
    inp = inp if isinstance(inp, dict) else {}
    canon = _norm_tool(name)
    return {"type": "tool", "name": canon, "title": _title(name, inp),
            "input_summary": input_summary(canon, inp), "status": "running",
            "result_summary": "", "tool_use_id": tid}


class _HarnessBuilder(Builder):
    """What Codex's and pi's folds share: the Claude builder's message
    keeping (`_close`, `_user`, `_assistant_msg`, `settle`), with the record
    reading replaced. A turn here is running only while a tool call is
    waiting on its result — neither harness writes a `stop_reason`, and
    "is it working now" is answered by the session's own state (§6.2), not
    by its transcript."""

    def feed(self, rec: dict) -> None:
        raise NotImplementedError

    def settle(self) -> None:
        cur = self._cur
        if cur is not None and not any(p.get("status") == "running" for p in cur["parts"]):
            cur["turn"]["running"] = False

    def _finish_tool(self, tid: str, content, *, error: bool = False) -> None:
        got = self._tools.pop(str(tid or ""), None)
        if not got:
            return
        name, part = got
        part["status"] = "error" if error else "done"
        # A failed Read answers with what went wrong, not with how much it
        # read: the "N lines" summary is for a Read that worked.
        part["result_summary"] = (_cut(_result_text(content).strip(), SUMMARY_MAX)
                                  if error else result_summary(name, content))


#: Codex staples its instructions and environment in as messages of their own:
#: a `developer` role, or a user message that is one big tag.
_CODEX_ASIDE = re.compile(r"^\s*(?:<|#\s*AGENTS\.md)")


class CodexBuilder(_HarnessBuilder):
    """`~/.codex/sessions/.../rollout-*.jsonl`.

    The conversation is in `response_item` records: `message` (developer,
    user, assistant), `reasoning` (a summary when there is one, else
    encrypted — the same "there was thinking here" the Claude side shows for
    a signature-only block), and two shapes of tool call, `custom_tool_call`
    (the sandboxed `exec`) and `function_call`, each answered later by an
    `*_output` record naming the same `call_id`. `event_msg` records repeat
    what the response items already said; only `task_complete` is read, to
    end the turn.
    """

    def feed(self, rec: dict) -> None:
        if not isinstance(rec, dict):
            return
        kind = rec.get("type")
        p = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        if kind == "event_msg":
            if p.get("type") == "task_complete":
                self._close()
            return
        if kind != "response_item":
            return
        t = p.get("type")
        if t == "message":
            self._message(rec, p)
        elif t == "reasoning":
            self._reasoning(rec, p)
        elif t in ("custom_tool_call", "function_call"):
            self._call(rec, p)
        elif t in ("custom_tool_call_output", "function_call_output"):
            self._finish_tool(p.get("call_id"), _codex_output(p.get("output")))

    def _message(self, rec: dict, p: dict) -> None:
        role = p.get("role")
        text = _codex_text(p.get("content")).strip()
        if not text:
            return
        if role == "assistant":
            msg = self._assistant_msg(_codex_rec(rec, p))
            msg["turn"]["running"] = True
            msg["parts"].append({"type": "text", "text": _cut(text, TEXT_MAX)})
        elif role == "user" and not _CODEX_ASIDE.match(text):
            self._user(_codex_rec(rec, p), _cut(text, TEXT_MAX))

    def _reasoning(self, rec: dict, p: dict) -> None:
        said = " ".join(str(s.get("text") or "") for s in p.get("summary") or []
                        if isinstance(s, dict)).strip()
        msg = self._assistant_msg(_codex_rec(rec, p))
        msg["turn"]["running"] = True
        parts = msg["parts"]
        if said:
            parts.append({"type": "reasoning", "text": _cut(said, REASONING_MAX),
                          "redacted": False})
        elif not (parts and parts[-1].get("type") == "reasoning" and parts[-1].get("redacted")):
            parts.append({"type": "reasoning", "text": "", "redacted": True})

    def _call(self, rec: dict, p: dict) -> None:
        msg = self._assistant_msg(_codex_rec(rec, p))
        msg["turn"]["running"] = True
        name = str(p.get("name") or "")
        tid = str(p.get("call_id") or p.get("id") or "")
        part = _tool_part(name, _codex_args(p), tid)
        msg["parts"].append(part)
        if tid:
            self._tools[tid] = (part["name"], part)


def _codex_rec(rec: dict, p: dict) -> dict:
    """A record in the shape the shared message keeping expects: an id and a
    time. Codex numbers its items (`msg_…`, `ctc_…`) and stamps the line."""
    return {"uuid": str(p.get("id") or rec.get("timestamp") or ""),
            "timestamp": rec.get("timestamp")}


def _codex_text(content) -> str:
    if isinstance(content, str):
        return content
    out = []
    for b in content or []:
        if isinstance(b, dict) and b.get("type") in ("input_text", "output_text", "text"):
            out.append(str(b.get("text") or ""))
    return "\n".join(out)


def _codex_output(output) -> str:
    return _codex_text(output) if not isinstance(output, str) else output


#: `exec` hands its command over as a little script: `tools.exec_command({cmd:"…"})`.
_EXEC_CMD = re.compile(r'cmd\s*:\s*"((?:[^"\\]|\\.)*)"')


def _codex_args(p: dict) -> dict:
    """A call's arguments as a dict, whichever way Codex wrote them: JSON in
    `arguments`, or the `exec` script in `input` (whose command is pulled out
    when it is there, so the step reads as the command it ran)."""
    raw = p.get("arguments")
    if isinstance(raw, str) and raw.strip().startswith("{"):
        try:
            got = json.loads(raw)
            if isinstance(got, dict):
                return got
        except ValueError:
            pass
    if isinstance(raw, dict):
        return raw
    text = p.get("input")
    if isinstance(text, str):
        m = _EXEC_CMD.search(text)
        if m:
            try:
                return {"command": json.loads(f'"{m.group(1)}"')}
            except ValueError:
                return {"command": m.group(1)}
        return {"command": text}
    return {}


def _pi_rec(rec: dict) -> dict:
    """pi's records in the shape the shared message keeping expects: it
    numbers every line (`id`, and `parentId` before it) and stamps it."""
    return {"uuid": str(rec.get("id") or rec.get("timestamp") or ""),
            "timestamp": rec.get("timestamp")}


class PiBuilder(_HarnessBuilder):
    """`~/.pi/agent/sessions/--<cwd>--/<stamp>_<id>.jsonl`.

    One `message` record per turn part, told apart by `message.role`: `user`,
    `assistant` (text, `thinking` — with its words, unlike Codex's — and
    `toolCall` blocks), and `toolResult`, which names the call it answers
    (`toolCallId`) and says whether it failed. `compaction` ends a turn, as
    Claude's compact boundary does.
    """

    def feed(self, rec: dict) -> None:
        if not isinstance(rec, dict):
            return
        if rec.get("type") == "compaction":
            self._close()
            return
        if rec.get("type") != "message":
            return
        m = rec.get("message") if isinstance(rec.get("message"), dict) else {}
        role = m.get("role")
        blocks = m.get("content")
        if isinstance(blocks, str):
            blocks = [{"type": "text", "text": blocks}]
        if role == "toolResult":
            self._finish_tool(m.get("toolCallId"), blocks, error=bool(m.get("isError")))
            return
        if role == "user":
            text = "\n".join(str(b.get("text") or "") for b in blocks or []
                              if isinstance(b, dict) and b.get("type") == "text").strip()
            if text:
                self._user(_pi_rec(rec), _cut(text, TEXT_MAX))
            return
        if role != "assistant":
            return
        msg = self._assistant_msg(_pi_rec(rec))
        msg["turn"]["running"] = True
        for b in blocks or []:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text" and str(b.get("text") or "").strip():
                msg["parts"].append({"type": "text", "text": _cut(str(b["text"]), TEXT_MAX)})
            elif t == "thinking":
                said = str(b.get("thinking") or "").strip()
                msg["parts"].append({"type": "reasoning",
                                     "text": _cut(said, REASONING_MAX) if said else "",
                                     "redacted": not said})
            elif t == "toolCall":
                tid = str(b.get("id") or "")
                part = _tool_part(str(b.get("name") or ""), b.get("arguments"), tid)
                msg["parts"].append(part)
                if tid:
                    self._tools[tid] = (part["name"], part)


#: opencode's argument names, as the summaries above spell them.
_OPENCODE_ARGS = {"filePath": "file_path", "oldString": "old_string",
                  "newString": "new_string"}
_OPENCODE_TOTAL = re.compile(r"\(End of file - total (\d+) lines?\)")


class OpencodeBuilder(_HarnessBuilder):
    """opencode's database (`agent_media_core.harnesses.opencode_db`).

    Not a file: `message` rows carry the role, `part` rows the content, each a
    JSON `data` column. Fed one message at a time (`{"id", "at", "role",
    "parts", "finish", "error"}`, from `_opencode_rows`). An assistant turn is
    several messages — one per model step — so they fold into one until the
    next prompt. A tool part holds its own result (`state`: pending, running,
    completed, error), so it is settled where it stands.
    """

    def feed(self, rec: dict) -> None:
        r = {"uuid": rec.get("id"), "timestamp": rec.get("at")}
        parts = [p for p in rec.get("parts") or [] if isinstance(p, dict)]
        if rec.get("role") == "user":
            text = "\n".join(str(p.get("text") or "") for p in parts
                             if p.get("type") == "text" and not p.get("synthetic")).strip()
            if text:
                self._user(r, _cut(text, TEXT_MAX))
            return
        if rec.get("role") != "assistant":
            return
        msg = self._assistant_msg(r)
        msg["turn"]["running"] = True
        for p in parts:
            t = p.get("type")
            if t == "text" and str(p.get("text") or "").strip():
                msg["parts"].append({"type": "text", "text": _cut(str(p["text"]), TEXT_MAX)})
            elif t == "reasoning":
                said = str(p.get("text") or "").strip()
                msg["parts"].append({"type": "reasoning",
                                     "text": _cut(said, REASONING_MAX) if said else "",
                                     "redacted": not said})
            elif t == "tool":
                st = p.get("state") if isinstance(p.get("state"), dict) else {}
                inp = st.get("input") if isinstance(st.get("input"), dict) else {}
                inp = {_OPENCODE_ARGS.get(k, k): v for k, v in inp.items()}
                tid = str(p.get("callID") or p.get("id") or "")
                part = _tool_part(str(p.get("tool") or ""), inp, tid)
                msg["parts"].append(part)
                self._tools[tid] = (part["name"], part)
                if st.get("status") == "completed":
                    out = str(st.get("output") or "")
                    self._finish_tool(tid, out)
                    # Its Read wraps the file in <path>/<content> tags and
                    # says the count itself, which is the count to show.
                    total = _OPENCODE_TOTAL.search(out) if part["name"] == "Read" else None
                    if total:
                        part["result_summary"] = f"{total.group(1)} lines"
                elif st.get("status") == "error":
                    self._finish_tool(tid, str(st.get("error") or ""), error=True)
        if rec.get("finish") == "stop" or rec.get("error"):
            self._close(interrupted=bool(rec.get("error")))


def _opencode_rows(session: str) -> list[dict]:
    """A session's messages with their parts, oldest first, as
    `OpencodeBuilder.feed` takes them."""
    from agent_media_core import harnesses

    msgs: dict[str, dict] = {}
    for mid, at, mdata, pdata in harnesses.opencode_rows(
            "select m.id, m.time_created, m.data, p.data from message m "
            "left join part p on p.message_id = m.id "
            "where m.session_id = ? order by m.time_created, m.id, p.id", (session,)):
        rec = msgs.get(mid)
        if rec is None:
            try:
                m = json.loads(mdata)
            except ValueError:
                m = {}
            m = m if isinstance(m, dict) else {}
            rec = msgs[mid] = {
                "id": mid, "role": m.get("role"), "parts": [],
                "at": datetime.fromtimestamp(at / 1000.0, timezone.utc).isoformat(),
                "finish": m.get("finish"), "error": m.get("error")}
        if pdata:
            try:
                rec["parts"].append(json.loads(pdata))
            except ValueError:
                pass
    return list(msgs.values())


def _opencode_messages(session: str) -> list[dict] | None:
    """Every message of an opencode session, or None when it has none."""
    rows = _opencode_rows(session)
    if not rows:
        return None
    b = OpencodeBuilder()
    for rec in rows:
        b.feed(rec)
    b.settle()
    return b.messages


def copy_messages(messages: list[dict]) -> list[dict]:
    """Copies the caller may change (the speech join does) without touching
    the cache."""
    out = []
    for m in messages:
        c = dict(m)
        c["parts"] = [dict(p) for p in m["parts"]]
        c["turn"] = dict(m["turn"])
        out.append(c)
    return out


# --- reading a file ---------------------------------------------------------------


def _parse_line(raw: bytes, wants: tuple = _WANT) -> dict | None:
    if not any(w in raw for w in wants):
        return None
    try:
        rec = json.loads(raw)
    except ValueError:
        return None
    return rec if isinstance(rec, dict) else None


def _complete_end(fh, size: int) -> int:
    pos = size
    while pos > 0:
        n = min(4096, pos)
        fh.seek(pos - n)
        k = fh.read(n).rfind(b"\n")
        if k >= 0:
            return pos - n + k + 1
        pos -= n
    return 0


def _seam(fh, offset: int) -> bytes:
    lo = max(0, offset - _SEAM)
    fh.seek(lo)
    return fh.read(offset - lo)


def _feed_range(b: Builder, fh, lo: int, hi: int, wants: tuple = _WANT) -> None:
    """Feed every line in bytes [lo, hi) (line boundaries), in order."""
    fh.seek(lo)
    left = hi - lo
    carry = b""
    while left > 0:
        buf = carry + fh.read(min(_CHUNK * 4, left))
        left = hi - fh.tell()
        lines = buf.split(b"\n")
        carry = lines.pop()
        for raw in lines:
            rec = _parse_line(raw, wants)
            if rec is not None:
                b.feed(rec)
    if carry.strip():
        rec = _parse_line(carry, wants)
        if rec is not None:
            b.feed(rec)


def _tail_start(fh, end: int, prompts: int, sidechain: bool = False,
                reader: "Reader | None" = None) -> int:
    """The offset of the line `prompts` prompts back from `end` (0 when the
    file has fewer). Reads backwards; only lines that could be a prompt are
    parsed."""
    carry = b""
    pos = end
    found = 0
    while pos > 0:
        n = min(_CHUNK, pos)
        pos -= n
        fh.seek(pos)
        buf = fh.read(n) + carry
        base = pos
        if pos > 0:
            cut = buf.find(b"\n")
            if cut < 0:
                carry = buf
                continue
            carry, buf, base = buf[:cut], buf[cut + 1:], pos + cut + 1
        else:
            carry = b""
        lines = buf.split(b"\n")
        starts, o = [], base
        for raw in lines:
            starts.append(o)
            o += len(raw) + 1
        rd = reader or READERS["claude"]
        for raw, at in zip(reversed(lines), reversed(starts)):
            if not rd.prompt_hint(raw):
                continue
            rec = _parse_line(raw, rd.wants)
            if rec is not None and rd.boundary(rec, sidechain):
                found += 1
                if found >= prompts:
                    return at
    return 0


class Reader:
    """How one harness's transcript is read: the fold (`build`), the words a
    line must hold to be worth parsing (`wants`), and how a prompt is
    recognised — cheaply from the raw line (`prompt_hint`), then for certain
    from the parsed record (`boundary`). The backwards scan that finds where
    to start reading uses the last two; nothing else here is harness-aware.
    """

    __slots__ = ("build", "wants", "prompt_hint", "boundary")

    def __init__(self, build, wants, prompt_hint, boundary) -> None:
        self.build, self.wants = build, wants
        self.prompt_hint, self.boundary = prompt_hint, boundary


def _claude_hint(raw: bytes) -> bool:
    # A tool result is never a prompt, and it is the line that carries whole
    # files: not worth parsing to find that out.
    return b'"type":"user"' in raw and b'"tool_result"' not in raw


def _codex_boundary(rec: dict, sidechain: bool = False) -> bool:
    p = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
    if rec.get("type") != "response_item" or p.get("type") != "message" \
            or p.get("role") != "user":
        return False
    text = _codex_text(p.get("content")).strip()
    return bool(text) and not _CODEX_ASIDE.match(text)


def _pi_boundary(rec: dict, sidechain: bool = False) -> bool:
    m = rec.get("message") if isinstance(rec.get("message"), dict) else {}
    if rec.get("type") != "message" or m.get("role") != "user":
        return False
    return any(isinstance(b, dict) and b.get("type") == "text" and str(b.get("text") or "").strip()
               for b in (m.get("content") or []))


READERS: dict[str, Reader] = {
    "claude": Reader(lambda sidechain=False: Builder(sidechain), _WANT,
                     _claude_hint, is_boundary),
    "codex": Reader(lambda sidechain=False: CodexBuilder(),
                    (b'"response_item"', b'"task_complete"'),
                    lambda raw: b'"role": "user"' in raw or b'"role":"user"' in raw,
                    _codex_boundary),
    "pi": Reader(lambda sidechain=False: PiBuilder(),
                 (b'"type": "message"', b'"type":"message"', b'"compaction"'),
                 lambda raw: b'"role": "user"' in raw or b'"role":"user"' in raw,
                 _pi_boundary),
}


class _File:
    __slots__ = ("ino", "size", "mtime", "lo", "offset", "seam", "builder", "lock",
                 "sidechain", "reader")

    def __init__(self, sidechain: bool = False, reader: Reader | None = None) -> None:
        self.ino = self.size = self.lo = self.offset = 0
        self.mtime = 0.0
        self.seam = b""
        self.sidechain = sidechain
        self.reader = reader or READERS["claude"]
        self.builder = self.reader.build(sidechain)
        self.lock = threading.Lock()


_CACHE: dict[str, _File] = {}
_LOCK = threading.Lock()


def _reset_for_tests() -> None:
    with _LOCK:
        _CACHE.clear()


def _read(path: str, full: bool, sidechain: bool = False,
          harness: str = "claude") -> tuple[list[dict], bool] | None:
    """`(messages, more)` for the transcript at `path`: the cached fold,
    brought up to date. `more` is whether older messages exist that have not
    been read (the file was read from its end); `full` reads them.
    `sidechain`: a subagent's transcript (see `Builder`)."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    with _LOCK:
        f = _CACHE.get(path)
        if f is None:
            f = _CACHE[path] = _File(sidechain, READERS.get(harness) or READERS["claude"])
            while len(_CACHE) > _CACHE_MAX:
                _CACHE.pop(next(iter(_CACHE)))
    with f.lock:
        same = (f.ino, f.size, f.mtime) == (st.st_ino, st.st_size, st.st_mtime) and f.offset
        if same and not (full and f.lo):
            return f.builder.messages, f.lo > 0
        try:
            with open(path, "rb") as fh:
                end = _complete_end(fh, st.st_size)
                appended = (f.offset and f.ino == st.st_ino and f.offset <= end
                            and _seam(fh, f.offset) == f.seam)
                if not appended:
                    # First sight, shrunk, replaced or rewritten: from scratch,
                    # from the end.
                    f.builder = f.reader.build(f.sidechain)
                    f.lo = 0 if full else _tail_start(fh, end, TAIL_PROMPTS, f.sidechain,
                                                      f.reader)
                    f.offset = f.lo
                elif full and f.lo:
                    # Older messages wanted: read what came before the tail.
                    # `lo` is a prompt, so everything before it is closed.
                    older = f.reader.build(f.sidechain)
                    _feed_range(older, fh, 0, f.lo, f.reader.wants)
                    older.close_open()
                    f.builder.messages[:0] = older.messages
                    f.builder._seen |= older._seen
                    f.lo = 0
                _feed_range(f.builder, fh, f.offset, end, f.reader.wants)
                f.builder.settle()
                f.offset = end
                f.seam = _seam(fh, end)
        except OSError:
            return None
        f.ino, f.size, f.mtime = st.st_ino, st.st_size, st.st_mtime
        return f.builder.messages, f.lo > 0


def transcript_path(session: str) -> str:
    """The Claude Code transcript for `session`, or ""."""
    from . import recaps

    return recaps.transcript_path(session)


def transcript_of(session: str) -> tuple[str, str]:
    """`(harness, path)` of a transcript this module can read, or `("", "")`.

    Claude Code first (its own lookup knows where a moved thread went), then
    the stores of the harnesses with a reader here. Hermes keeps
    conversations in a database, so it has no path and no parser: its
    threads are still built from their spoken lines."""
    path = transcript_path(session)
    if path:
        return "claude", path
    from agent_media_core import harnesses

    found = harnesses.transcript(session)
    if not found or found[0] not in READERS:
        return "", ""
    return found[0], str(found[1])


def messages(session: str, *, limit: int | None = None, before: str = "") \
        -> tuple[list[dict], bool] | None:
    """`(messages, more)` for a session, oldest first — copies, safe to
    change — or None when there is no transcript this can read (Hermes,
    which keeps conversations in a database, or nothing written yet).

    Claude Code, Codex and pi each have a reader (`READERS`); the harness is
    decided by which store holds the file.

    `limit` keeps the newest that many; `before` (a message id) keeps only
    those before it. `more` says older ones exist beyond what was returned.
    """
    harness, path = transcript_of(session)
    if not path:
        return _opencode_page(session, limit=limit, before=before)
    return messages_at(path, limit=limit, before=before, harness=harness)


def _opencode_page(session: str, *, limit: int | None, before: str) \
        -> tuple[list[dict], bool] | None:
    """`messages` for opencode, which keeps a database rather than a file: the
    whole session is read each time (a query, no file to walk backwards)."""
    from agent_media_core import harnesses

    msgs = _opencode_messages(session) if harnesses.is_opencode(session) else None
    if msgs is None:
        return None
    more = False
    if before:
        idx = next((i for i, m in enumerate(msgs) if m["id"] == before), None)
        more = bool(idx)
        msgs = msgs[:idx] if idx is not None else []
    if limit and len(msgs) > limit:
        more = True
        msgs = msgs[-limit:]
    return copy_messages(msgs), more


def messages_at(path: str, *, limit: int | None = None, before: str = "",
                sidechain: bool = False, harness: str = "claude") \
        -> tuple[list[dict], bool] | None:
    """`messages`, for the transcript at `path` — a session's, or with
    `sidechain` a subagent's own (agents.py). None when it cannot be read."""
    got = _read(path, full=False, sidechain=sidechain, harness=harness)
    if got is None:
        return None
    msgs, more = got
    want_older = bool(before) and not any(m["id"] == before for m in msgs[1:])
    if (limit and len(msgs) < limit and more) or (want_older and more):
        got = _read(path, full=True, sidechain=sidechain, harness=harness)
        if got is None:
            return None
        msgs, more = got
    if before:
        idx = next((i for i, m in enumerate(msgs) if m["id"] == before), None)
        msgs = msgs[:idx] if idx is not None else []
    if limit and len(msgs) > limit:
        more = True
        msgs = msgs[-limit:]
    return copy_messages(msgs), more


#: A jump (`around`) shows this many messages before the one it names.
AROUND_BEFORE = 5


def window_around(msgs: list[dict], around: str, limit: int, most: int) \
        -> tuple[list[dict], bool, bool] | None:
    """`(window, older, newer)` of `msgs` (oldest first) holding the message
    `around`, or None when it is not there. The window runs from a few
    messages before it to the newest, so the live thread joins on below it —
    unless that is more than `most` messages; then it is `limit` long and
    `newer` says the thread goes on past it."""
    idx = next((i for i, m in enumerate(msgs) if m["id"] == around), None)
    if idx is None:
        return None
    start = max(0, idx - AROUND_BEFORE)
    if len(msgs) - start <= most:
        return msgs[start:], start > 0, False
    return msgs[start:start + limit], start > 0, True


def messages_around(session: str, around: str, *, limit: int, most: int) \
        -> tuple[list[dict], bool, bool] | None:
    """`window_around` over a session's whole transcript (copies). None when
    there is no transcript this can read, or no such message."""
    harness, path = transcript_of(session)
    if path:
        got = _read(path, full=True, harness=harness)
    else:
        got = _opencode_page(session, limit=None, before="")
    if got is None:
        return None
    win = window_around(got[0], around, limit, most)
    if win is None:
        return None
    msgs, older, newer = win
    return copy_messages(msgs), older, newer


def file_state(session: str) -> tuple[int, int, float] | None:
    """`(inode, size, mtime)` of the session's transcript — what a watcher
    polls to know there is something new to read. Whichever harness wrote
    it, so a Codex or pi thread streams like a Claude one. An opencode
    session has no file: its parts are counted and their newest change
    taken, which moves exactly when a file's size and mtime would."""
    path = transcript_of(session)[1]
    if not path:
        from agent_media_core import harnesses

        if not harnesses.is_opencode(session):
            return None
        rows = harnesses.opencode_rows(
            "select count(*), max(time_updated) from part where session_id = ?", (session,))
        if not rows or not rows[0][0]:
            return None
        return 0, int(rows[0][0]), float(rows[0][1] or 0) / 1000.0
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_ino, st.st_size, st.st_mtime


# --- joining speech ---------------------------------------------------------------

#: The follow-along fields of a live line, as they move onto the message.
LIVE_FIELDS = ("sentences", "sentence", "offsets", "elapsed", "server_time", "delay",
               "paused")
#: A spoken line is looked for from this long before its message began (the
#: clocks are the same host's, but `at` is rounded) …
_JOIN_EARLY_S = 5.0
#: … and the text fallback accepts a match at least this similar.
_JOIN_RATIO = 0.6


def _key(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", str(text or "").lower()).split())


def spoken_keys(reply: str) -> list[str]:
    """The dedup keys the Stop hook would have given this reply.

    The hook keys a reply by `sha1(strip_markdown(reply without its
    [[visual:]] markers))` — before any spoken summary rewrites the words —
    and a `[[reveal:]]` reply is spoken as two halves, each keyed on its own.
    """
    from agent_media_core.intake._text import strip_markdown
    from agent_media_core.intake._visual import extract_visual_markers

    raw = (reply or "").strip()
    clean, _hint, pre, post = extract_visual_markers(raw)
    out = []
    text = strip_markdown(clean)
    if text:
        out.append(_key(text))
    for half in (pre, post):
        if half is not None:
            h = strip_markdown(half)
            if h:
                out.append(_key(h))
    return out


def _final_texts(msg: dict) -> list[str]:
    """What the hook may have been handed as the reply: the last text part,
    and every text part after the last tool call, joined."""
    texts = []
    tail: list[str] = []
    for p in msg["parts"]:
        if p["type"] in ("tool", "ask"):
            tail = []
        elif p["type"] == "text":
            tail.append(p["text"])
    if tail:
        texts.append(tail[-1])
        if len(tail) > 1:
            texts.extend(["\n\n".join(tail), "\n".join(tail)])
    return texts


def _spoken(line: dict) -> dict:
    out = {"id": line.get("id") or None, "key": line.get("key") or "", "at": line.get("at")}
    if line.get("images"):
        out["images"] = line["images"]
        out["figure"] = bool(line.get("figure"))
    if line.get("unheard") and not line.get("live"):
        # Held for the listener and never played (§6.2.2): the app makes its
        # play key the big one.
        out["unheard"] = True
    if line.get("resume") and not line.get("live"):
        # Interrupted part-way (§6.2.2): the ▶ resumes at `sentence`, and the
        # app says where — `at_s` into `dur_s` — and marks what was not heard.
        out["resume"] = line["resume"]
    if line.get("live"):
        out["live"] = {k: line.get(k) for k in LIVE_FIELDS}
    elif line.get("sentences"):
        # The turn's timeline with no claim that it is playing: the newest
        # turn carries it whether or not the live row survived (§6.2). A
        # client that can see the player's own position (`/speech/now`'s
        # `pos` and `turn`) bolds from that instead of an `elapsed` that
        # died with the row.
        out["timeline"] = {"sentences": line.get("sentences") or [],
                           "offsets": line.get("offsets") or [],
                           "measured": bool(line.get("measured"))}
    return out


def join_speech(msgs: list[dict], lines: list[dict]) -> None:
    """Give each message the spoken line that said it, in place.

    The heuristic, in order:

    1. **By key.** The Stop hook keys each reply by a hash of its stripped
       text (`spoken_keys`), and a line carries that key. The same hash of
       the assistant message's final text is an exact match — and it holds
       even when a spoken summary rewrote the words, because the key is taken
       before the rewrite.
    2. **By words**, for what the key misses (a line from before keys, a
       reply the hook got differently than we reconstruct it): the nearest
       unclaimed agent line *at or after* the message began, whose words are
       at least `_JOIN_RATIO` similar to the message's final text.
    3. A listener line joins a user message with the same words (normalised)
       said within two minutes of it.

    Each line joins one message. Failure mode: a message that was never
    spoken — or whose speech cannot be recognised — keeps `spoken: null`;
    it is never guessed onto the nearest line by time alone. A line that
    joins nothing (a notification, a question read out on the alert lane)
    stays in `lines` and is not a message.
    """
    agent = [l for l in lines if l.get("who") == "agent" and l.get("at") is not None]
    you = [l for l in lines if l.get("who") == "you" and l.get("at") is not None]
    by_key: dict[str, dict] = {}
    for l in agent:
        if l.get("key"):
            by_key.setdefault(l["key"], l)
    claimed: set[int] = set()

    replies = [m for m in msgs if m["role"] == "assistant"]
    for m in replies:
        for text in _final_texts(m):
            for k in spoken_keys(text):
                line = by_key.get(k)
                if line is not None and id(line) not in claimed:
                    claimed.add(id(line))
                    m["spoken"] = _spoken(line)
                    break
            if m["spoken"]:
                break
    for m in replies:
        if m["spoken"]:
            continue
        # Without the canvas markers: a `[[visual:]]` is an instruction, never
        # a word anybody said, so the spoken line does not have it. They are
        # stripped after the join (`strip_markers`), which left the raw text
        # here — and a marker is easily 400 characters, which is the whole
        # window this compares. A reply whose figure sat near the top scored
        # 0.45 against its own live line and joined nothing, so the thread had
        # no live line to follow for the length of that reply: no bold at all,
        # the whole way down (David, 23 Sep 2026). The key path above needs
        # the raw text and keeps it — `spoken_keys` does its own stripping.
        texts = [display_text(t) for t in _final_texts(m)]
        if not texts:
            continue
        want = _norm(texts[0])[:400]
        if not want:
            continue
        best, score = None, 0.0
        for l in agent:
            if id(l) in claimed or l["at"] < m["at"] - _JOIN_EARLY_S:
                continue
            got = _norm(l.get("text"))[:400]
            r = difflib.SequenceMatcher(None, want, got, autojunk=False).ratio() if got else 0.0
            if r > score:
                best, score = l, r
        if best is not None and score >= _JOIN_RATIO:
            claimed.add(id(best))
            m["spoken"] = _spoken(best)
    for m in msgs:
        if m["role"] != "user" or m["spoken"]:
            continue
        want = _norm(m["parts"][0]["text"] if m["parts"] else "")
        for l in you:
            if id(l) in claimed or abs(l["at"] - m["at"]) > 120:
                continue
            if _norm(l.get("text")) == want:
                claimed.add(id(l))
                m["spoken"] = _spoken(l)
                break


#: A `[[visual: …]]` / `[[reveal: …]]` marker, as the Stop hook reads it
#: (intake/_visual.py), with the spaces around it.
_MARKER = re.compile(r"[ \t]*\[\[\s*(?:visual|reveal)\s*:\s*.+?\s*\]\][ \t]*",
                     re.IGNORECASE | re.DOTALL)


def display_text(text: str) -> str:
    """`text` without its `[[visual:]]` / `[[reveal:]]` markers — they are
    instructions to the canvas, never words for anyone to read. The markdown
    stays (the client renders it). A marker mid-sentence leaves one space; one
    on a line of its own leaves no blank line behind."""
    if "[[" not in (text or ""):
        return text
    out = _MARKER.sub(" ", text)
    out = re.sub(r"(?m)^ +| +$", "", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def strip_markers(msgs: list[dict]) -> None:
    """Every assistant text part through `display_text`, in place. After the
    speech join, which keys on the raw words (a `[[reveal:]]` reply is keyed
    by its halves)."""
    for m in msgs:
        if m.get("role") != "assistant":
            continue
        for p in m.get("parts") or []:
            if p.get("type") == "text" and "[[" in (p.get("text") or ""):
                p["text"] = display_text(p["text"])


def live_of(msgs: list[dict]) -> dict | None:
    """`{"id", "at", ...follow-along}` for the message whose speech is
    playing now, or None."""
    for m in msgs:
        sp = m.get("spoken") or {}
        if sp.get("live"):
            return {"id": m["id"], "at": sp.get("at"), **sp["live"]}
    return None


# --- the other harnesses ----------------------------------------------------------


def messages_from_lines(lines: list[dict], working: bool = False) -> list[dict]:
    """Messages for a session with no transcript parser (Codex, pi, Hermes):
    one per spoken line, text only — what the lines already said, in the new
    shape, so a client reads one format whatever the harness."""
    out = []
    for l in lines:
        who = "user" if l.get("who") == "you" else "assistant"
        parts: list[dict] = []
        if l.get("ask"):
            parts.append({"type": "ask", "ask": l["ask"], "status": "done", "answer": "",
                          "tool_use_id": ""})
        elif l.get("text"):
            parts.append({"type": "text", "text": l["text"]})
        msg = {"id": f"line:{l.get('at')}", "role": who, "at": l.get("at") or 0.0,
               "parts": parts, "spoken": _spoken(l) if l.get("id") or l.get("live") else None,
               "turn": {"running": False}}
        if l.get("command"):
            msg["command"] = l["command"]
        out.append(msg)
    if working and out and out[-1]["role"] == "assistant":
        out[-1]["turn"]["running"] = True
    return out
