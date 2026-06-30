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
 * Engine/voice/routing come from ~/.config/agent-media.env (MEDIA_RENDER_* /
 * MEDIA_SPEECH_*), loaded by the hook. No per-extension TTS config here.
 *
 * Install once:
 *   pi install /home/mel/agent-media/packages/core/pi
 *
 * Env:
 *   MEDIA_HOOK_ENABLED        "0" disables all agent-media hooks
 *   PI_TTS_STREAM_ENABLED     "0" disables just this extension
 *   MEDIA_HOOK_STREAM_BIN     override the hook binary (default: media-hook-pi-stream)
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { spawn, type ChildProcess } from "node:child_process";

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
	pi.on("message_start", (event: any, _ctx) => {
		try {
			if (process.env.MEDIA_HOOK_ENABLED === "0") return;
			if (process.env.PI_TTS_STREAM_ENABLED === "0") return;
			// Only stream the assistant's own messages — skip user echoes,
			// system inserts, and tool-result messages.
			if (event?.message?.role !== "assistant") return;

			// Close any still-open stream from a previous (concurrent) turn so
			// it finalises before this one starts.
			endActive();

			const bin = process.env.MEDIA_HOOK_STREAM_BIN || "media-hook-pi-stream";
			active = spawn(bin, [], { stdio: ["pipe", "ignore", "ignore"] });
			active.on("error", () => {
				// Hook missing / not on PATH: never break the agent loop.
				active = null;
			});
		} catch {
			active = null;
		}
	});

	pi.on("message_update", (event: any, _ctx) => {
		if (!active || !active.stdin || active.stdin.destroyed) return;
		const e = event?.assistantMessageEvent;
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
