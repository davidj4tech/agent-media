/**
 * agent-media TTS extension for pi.
 *
 * At the end of each agent turn, pipes the assistant's text response to
 * `media-hook-pi` (agent-media core intake), which renders it via Edge TTS
 * and delivers to the Snapcast feed.
 *
 * Install once:
 *   pi install /home/mel/projects/agent-media/packages/core/pi
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { spawn } from "child_process";

export default function (pi: ExtensionAPI) {
	pi.on("agent_end", async (event) => {
		if (process.env.MEDIA_HOOK_ENABLED === "0") return;

		// Find the last assistant message and extract its text content.
		const messages = event.messages;
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

		const hook = process.env.MEDIA_HOOK_BIN || "media-hook-pi";
		try {
			const child = spawn(hook, [], {
				stdio: ["pipe", "ignore", "ignore"],
				detached: true,
			});
			child.stdin.write(text);
			child.stdin.end();
			child.unref();
		} catch {
			// Non-fatal: TTS is best-effort.
		}
	});
}
