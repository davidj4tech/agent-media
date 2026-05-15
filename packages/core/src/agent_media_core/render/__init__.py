"""Text → audio rendering. Engines: edge, openai, qwen, realtime."""

from .engines import (
    EDGE_DEFAULT_VOICE,
    KNOWN_ENGINES,
    OPENAI_DEFAULT_MODEL,
    OPENAI_DEFAULT_VOICE,
    QWEN_DEFAULT_BASE_URL,
    QWEN_DEFAULT_LANG,
    QWEN_DEFAULT_MODEL,
    QWEN_DEFAULT_VOICE,
    REALTIME_DEFAULT_MODEL,
    REALTIME_DEFAULT_VOICE,
    default_openai_python,
    render_text,
)

__all__ = [
    "render_text",
    "default_openai_python",
    "KNOWN_ENGINES",
    "EDGE_DEFAULT_VOICE",
    "OPENAI_DEFAULT_VOICE",
    "OPENAI_DEFAULT_MODEL",
    "QWEN_DEFAULT_VOICE",
    "QWEN_DEFAULT_MODEL",
    "QWEN_DEFAULT_LANG",
    "QWEN_DEFAULT_BASE_URL",
    "REALTIME_DEFAULT_VOICE",
    "REALTIME_DEFAULT_MODEL",
]
