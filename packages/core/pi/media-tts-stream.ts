/**
 * agent-media STREAMING TTS extension for pi.
 *
 * Streaming sibling of media-tts.ts. Instead of waiting for `agent_end` and
 * speaking the whole reply at once, this subscribes to per-token deltas
 * (`message_update` with `assistantMessageEvent.type === "text_delta"`) and
 * pipes them into a `media-hook-pi-stream` subprocess as they arrive. That
 * process segments on sentence boundaries and renders + speaks each sentence
 * through agent-media's sink-speech broker (→ Snapcast) while the model is
 * still generating — so audio starts a few seconds into the first sentence
 * instead of after the entire response.
 *
 * Use this OR media-tts.ts, not both — they'd both react to the same assistant
 * message and you'd get duplicated audio.
 *
 * It tells agent-media the same Claude-shaped events media-tts.ts does
 * (session_start → SessionStart, input → UserPromptSubmit, tool_call →
 * PreToolUse, agent_end → Stop) so the phone can find this pi and follow it,
 * and passes the session id to the stream hook so each reply is filed under
 * its conversation.
 *
 * Engine/voice/routing come from ~/.config/agent-media.env (MEDIA_RENDER_* /
 * MEDIA_SPEECH_*), loaded by the hook. No per-extension TTS config here.
 *
 * Install once:
 *   pi install /home/mel/projects/agent-media/packages/core/pi
 *
 * Env:
 *   MEDIA_HOOK_ENABLED        "0" disables all agent-media hooks
 *   PI_TTS_STREAM_ENABLED     "0" disables just this extension
 *   MEDIA_HOOK_STREAM_BIN     override the stream hook binary (default: media-hook-pi-stream)
 *   MEDIA_HOOK_BIN            override the event hook binary (default: media-hook-pi)
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { spawn, type ChildProcess } from "node:child_process";
import { existsSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

// pi's PATH need not include the venv agent-media is installed in.
function bin(envVar: string, name: string): string {
	if (process.env[envVar]) return process.env[envVar] as string;
	const venv = join(homedir(), "projects", "agent-media", ".venv", "bin", name);
	return existsSync(venv) ? venv : name;
}

function sessionId(ctx: any): string {
	try {
		return ctx?.sessionManager?.getSessionId?.() || "";
	} catch {
		return "";
	}
}

function event(name: string, ctx: any, extra: Record<string, unknown> = {}): void {
	if (process.env.MEDIA_HOOK_ENABLED === "0") return;
	const session_id = sessionId(ctx);
	if (!session_id) return;
	try {
		const child = spawn(bin("MEDIA_HOOK_BIN", "media-hook-pi"), ["event"], {
			stdio: ["pipe", "ignore", "ignore"],
			detached: true,
		});
		child.on("error", () => {});
		child.stdin.on("error", () => {});
		child.stdin.write(JSON.stringify({ hook_event_name: name, session_id, ...extra }));
		child.stdin.end();
		child.unref();
	} catch {
		// Non-fatal: agent-media is best-effort.
	}
}

// One active hook subprocess per pi turn. pi emits a single assistant message
// per user turn, but if a new message_start arrives before the previous ended
// we gracefully close the old stream's stdin so it finalises first.
let active: ChildProcess | null = null;

function endActive(): void {
	const p = active;
	active = null;
	if (!p) return;
	try {
		if (p.stdin && !p.stdin.destroyed) p.stdin.end();
	} catch {
		/* swallow */
	}
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

	pi.on("tool_call", async (e: any, ctx) => {
		event("PreToolUse", ctx, { tool_name: e?.toolName || "", tool_input: e?.input || {} });
	});

	pi.on("agent_end", async (_e, ctx) => {
		event("Stop", ctx);
	});

	pi.on("message_start", (msg: any, ctx) => {
		try {
			if (process.env.MEDIA_HOOK_ENABLED === "0") return;
			if (process.env.PI_TTS_STREAM_ENABLED === "0") return;
			// Only stream the assistant's own messages — skip user echoes,
			// system inserts, and tool-result messages.
			if (msg?.message?.role !== "assistant") return;

			// Close any still-open stream from a previous (concurrent) turn so
			// it finalises before this one starts.
			endActive();

			const session = sessionId(ctx);
			active = spawn(bin("MEDIA_HOOK_STREAM_BIN", "media-hook-pi-stream"),
				session ? ["--session", session] : [],
				{ stdio: ["pipe", "ignore", "ignore"] });
			active.on("error", () => {
				// Hook missing / not on PATH: never break the agent loop.
				active = null;
			});
		} catch {
			active = null;
		}
	});

	pi.on("message_update", (update: any, _ctx) => {
		if (!active || !active.stdin || active.stdin.destroyed) return;
		const e = update?.assistantMessageEvent;
		// Only spoken text content. Skip thinking, tool-call argument deltas,
		// and start/end markers — those don't belong in audio. The hook's
		// segmenter handles markdown stripping, so no pre-filtering here.
		if (!e || e.type !== "text_delta" || typeof e.delta !== "string") return;
		try {
			active.stdin.write(e.delta);
		} catch {
			/* producer closed early — drop the delta silently */
		}
	});

	pi.on("message_end", (_event: any, _ctx) => {
		endActive();
	});
}
