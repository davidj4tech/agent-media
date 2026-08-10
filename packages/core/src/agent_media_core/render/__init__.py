"""Text → audio rendering. Core ships `edge`; other engines are plugins."""

from .engines import (
    EDGE_DEFAULT_VOICE,
    KNOWN_ENGINES,
    render_text,
)
from .subtitles import sentence_offsets
from ..extensions import all_engine_names, discover_render_engines

__all__ = [
    "render_text",
    "sentence_offsets",
    "all_engine_names",
    "discover_render_engines",
    "KNOWN_ENGINES",
    "EDGE_DEFAULT_VOICE",
]
