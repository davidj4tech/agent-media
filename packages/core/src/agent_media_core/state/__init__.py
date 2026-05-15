"""State store: SQLite-backed now-playing, history, errors."""

from .store import StateStore, default_db_path

__all__ = ["StateStore", "default_db_path"]
