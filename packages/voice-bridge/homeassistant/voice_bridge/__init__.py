"""Voice Bridge — send Assist transcripts to tmux-voice-bridge.

A deliberately small conversation agent. Home Assistant has no built-in way to
point a conversation agent at your own HTTP endpoint (`openai_conversation` has
no base-URL field), which is the only reason a third-party integration was in
this path at all. This does that one thing.

It registers a real `conversation.*` entity, so it appears in the Assist
pipeline picker like any other agent — unlike a legacy `async_set_agent` agent,
which is addressable only by config-entry id and invisible to the UI.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

PLATFORMS = [Platform.CONVERSATION]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Voice Bridge from a config entry."""
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when options change (URL or timeout edited in the UI)."""
    await hass.config_entries.async_reload(entry.entry_id)
