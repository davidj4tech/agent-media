/**
 * agent-media plugin for opencode.
 *
 * Tells agent-media what Claude Code's hooks tell it, in the same shape
 * (see agent_media_core/intake/agent_events.py), through
 * `media-hook-opencode`:
 *   chat.message        → UserPromptSubmit: your words, as a "You:" turn
 *   tool.execute.before → PreToolUse: a step in the phone's list
 *   session.status      → a retry (a model busy or out of its limit; opencode
 *                         tries five times over ~70 s) as a step, so the
 *                         phone says why it is waiting
 *   session.idle        → Stop, then the reply is spoken. opencode says the
 *                         session went idle, not what it said, so the hook
 *                         reads the reply back from opencode's database.
 *
 * A subagent's session goes idle too; the hook leaves it unspoken, since its
 * words are its parent's tool result.
 *
 * Install once (media-setup does this):
 *   ln -s ~/projects/agent-media/packages/core/opencode/agent-media.js \
 *         ~/.config/opencode/plugins/agent-media.js
 */

import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

// opencode's PATH need not include the venv agent-media is installed in.
function hookBin() {
  if (process.env.MEDIA_HOOK_BIN_OPENCODE) return process.env.MEDIA_HOOK_BIN_OPENCODE;
  const venv = join(homedir(), "projects", "agent-media", ".venv", "bin", "media-hook-opencode");
  return existsSync(venv) ? venv : "media-hook-opencode";
}

function run(args, input = "") {
  if (process.env.MEDIA_HOOK_ENABLED === "0") return;
  try {
    const child = spawn(hookBin(), args, { stdio: ["pipe", "ignore", "ignore"], detached: true });
    child.on("error", () => {});
    child.stdin.on("error", () => {});
    child.stdin.end(input);
    child.unref();
  } catch {
    // Non-fatal: agent-media is best-effort.
  }
}

function event(name, session_id, extra = {}) {
  if (!session_id) return;
  run(["event"], JSON.stringify({ hook_event_name: name, session_id, ...extra }));
}

export const AgentMedia = async () => ({
  "chat.message": async (input, output) => {
    const text = (output?.parts || [])
      .filter((p) => p?.type === "text" && !p.synthetic && p.text)
      .map((p) => p.text)
      .join("\n")
      .trim();
    if (text) event("UserPromptSubmit", input?.sessionID, { prompt: text, cwd: process.cwd() });
  },

  "tool.execute.before": async (input, output) => {
    event("PreToolUse", input?.sessionID, { tool_name: input?.tool || "", tool_input: output?.args || {} });
  },

  event: async ({ event: e }) => {
    const status = e?.type === "session.status" ? e.properties?.status : null;
    if (status?.type === "retry") {
      const why = /limit|rate|429|quota/i.test(status.message || "") ? "Model limit reached" : "Model busy";
      event("PreToolUse", e.properties?.sessionID, {
        tool_name: "Retry",
        tool_input: { description: `${why}, retrying (try ${(status.attempt || 0) + 1})` },
      });
      return;
    }
    if (e?.type !== "session.idle") return;
    const session = e.properties?.sessionID;
    if (!session) return;
    event("Stop", session);
    run(["--session", session]);
  },
});
