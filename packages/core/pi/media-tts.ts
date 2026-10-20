/**
 * agent-media extension for pi.
 *
 * At the end of each agent turn, pipes the assistant's text response to
 * `media-hook-pi --session <id>` (agent-media core intake), which speaks it
 * and files it under this conversation in the library.
 *
 * It also tells agent-media what Claude Code's hooks tell it, in the same
 * shape (see agent_media_core/intake/agent_events.py):
 *   session_start → SessionStart: which pi, in which tmux pane, is on which
 *                   session. pi retitles its process and keeps no file open,
 *                   so this is the only way the phone can find a live pi.
 *   input         → UserPromptSubmit: your words, as a "You:" turn
 *   tool_call     → PreToolUse: a step in the phone's list
 *   agent_end     → Stop
 *
 * And before each run it asks `media-hook-context` whether the context is
 * full enough to say so (50/70/85% of pi's own count, ctx.getContextUsage()),
 * and passes any note it prints to the model as a hidden message.
 *
 * Install once:
 *   pi install /home/ryer/projects/agent-media/packages/core/pi
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { execFileSync, spawn } from "child_process";
import { existsSync } from "fs";
import { homedir } from "os";
import { join } from "path";

// pi's PATH need not include the venv agent-media is installed in.
function hookBin(): string {
	if (process.env.MEDIA_HOOK_BIN) return process.env.MEDIA_HOOK_BIN;
	const venv = join(homedir(), "projects", "agent-media", ".venv", "bin", "media-hook-pi");
	return existsSync(venv) ? venv : "media-hook-pi";
}

function run(args: string[], input: string): void {
	if (process.env.MEDIA_HOOK_ENABLED === "0") return;
	try {
		const child = spawn(hookBin(), args, {
			stdio: ["pipe", "ignore", "ignore"],
			detached: true,
		});
		child.on("error", () => {});
		child.stdin.on("error", () => {});
		child.stdin.write(input);
		child.stdin.end();
		child.unref();
	} catch {
		// Non-fatal: agent-media is best-effort.
	}
}

// How full the context is, as a note for the model, or "" (intake/context.py).
function contextNote(session_id: string, usage: any): string {
	if (!usage || usage.tokens == null || !usage.contextWindow) return "";
	const bin = join(homedir(), "projects", "agent-media", ".venv", "bin", "media-hook-context");
	try {
		return execFileSync(existsSync(bin) ? bin : "media-hook-context", [], {
			input: JSON.stringify({
				session_id,
				context_tokens: usage.tokens,
				context_window: usage.contextWindow,
			}),
			timeout: 5000,
			stdio: ["pipe", "pipe", "ignore"],
		}).toString().trim();
	} catch {
		return "";
	}
}

function sessionId(ctx: any): string {
	try {
		return ctx?.sessionManager?.getSessionId?.() || "";
	} catch {
		return "";
	}
}

function event(name: string, ctx: any, extra: Record<string, unknown> = {}): void {
	const session_id = sessionId(ctx);
	if (!session_id) return;
	run(["event"], JSON.stringify({ hook_event_name: name, session_id, ...extra }));
}

export default function (pi: ExtensionAPI) {
	pi.on("session_start", async (_event, ctx) => {
		event("SessionStart", ctx, {
			pid: process.pid,
			pane: process.env.TMUX_PANE || "",
			cwd: process.cwd(),
		});
	});

	pi.on("input", async (e: any, ctx) => {
		// An extension's own injected input is not something anybody typed.
		if (e?.source === "extension") return;
		const text = typeof e?.text === "string" ? e.text : "";
		if (text.trim()) event("UserPromptSubmit", ctx, { prompt: text });
	});

	pi.on("before_agent_start", async (_e, ctx) => {
		const session_id = sessionId(ctx);
		const note = session_id ? contextNote(session_id, ctx.getContextUsage?.()) : "";
		if (!note) return;
		return { message: { customType: "agent-media-context", content: note, display: false } };
	});

	pi.on("tool_call", async (e: any, ctx) => {
		event("PreToolUse", ctx, { tool_name: e?.toolName || "", tool_input: e?.input || {} });
	});

	pi.on("agent_end", async (e, ctx) => {
		event("Stop", ctx);

		// Find the last assistant message and extract its text content.
		const messages = e.messages;
		let text = "";
		for (let i = messages.length - 1; i >= 0; i--) {
			const msg = messages[i] as any;
			if (msg.role === "assistant" && Array.isArray(msg.content)) {
				for (const part of msg.content) {
					if (part.type === "text" && part.text) {
						text += part.text;
					}
				}
				if (text) break;
			}
		}
		if (!text.trim()) return;
		const session = sessionId(ctx);
		run(session ? ["--session", session] : [], text);
	});
}
