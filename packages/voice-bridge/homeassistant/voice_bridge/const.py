"""Constants for the Voice Bridge conversation agent."""

DOMAIN = "voice_bridge"

CONF_URL = "url"
CONF_TIMEOUT = "timeout"

DEFAULT_URL = "http://127.0.0.1:18790"
DEFAULT_TIMEOUT = 30

# tmux-voice-bridge answers the OpenAI chat-completions shape. We only ever
# send one user message and read one reply, so this is the whole protocol.
CHAT_PATH = "/v1/chat/completions"
