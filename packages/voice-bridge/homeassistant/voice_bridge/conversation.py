"""The conversation entity: transcript in, bridge reply out."""

from __future__ import annotations

import logging
from typing import Literal

import aiohttp
import async_timeout

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CHAT_PATH, CONF_TIMEOUT, CONF_URL, DEFAULT_TIMEOUT, DEFAULT_URL, DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the conversation entity."""
    async_add_entities([VoiceBridgeConversationEntity(entry)])


class VoiceBridgeConversationEntity(conversation.ConversationEntity):
    """Forwards what you said to tmux-voice-bridge and speaks its answer back."""

    # No device to hang a name off, so name the entity itself — otherwise it
    # shows up in the pipeline picker as a bare entry id with no label.
    _attr_has_entity_name = False

    def __init__(self, entry: ConfigEntry) -> None:
        self.entry = entry
        self._attr_unique_id = entry.entry_id
        self._attr_name = entry.title or "Voice Bridge"

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        """Everything. The bridge parses a small command grammar and otherwise
        passes text through verbatim; it never interprets language."""
        return conversation.MATCH_ALL

    def _option(self, key: str, default):
        return self.entry.options.get(key, self.entry.data.get(key, default))

    async def _async_handle_message(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        """POST the transcript to the bridge; speak whatever it says back.

        The bridge's reply is a status line ("Sent to local session scratch",
        "Sent to the agent"), not a conversational answer — with a TTS stage on
        the pipeline it is read aloud, which is the confirmation that your words
        landed somewhere. So an error here must surface rather than be swallowed:
        silence sounds exactly like success.
        """
        url = self._option(CONF_URL, DEFAULT_URL).rstrip("/") + CHAT_PATH
        timeout = float(self._option(CONF_TIMEOUT, DEFAULT_TIMEOUT))
        response = intent.IntentResponse(language=user_input.language)

        try:
            session = async_get_clientsession(self.hass)
            async with async_timeout.timeout(timeout):
                resp = await session.post(url, json={
                    "model": "tmux-voice-bridge",
                    "messages": [{"role": "user", "content": user_input.text}],
                    "stream": False,
                })
                resp.raise_for_status()
                data = await resp.json()
            reply = data["choices"][0]["message"]["content"]
        except (TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.error("voice_bridge: %s unreachable: %s", url, err)
            response.async_set_error(
                intent.IntentResponseErrorCode.FAILED_TO_HANDLE,
                "The voice bridge is not responding.")
            return conversation.ConversationResult(
                response=response, conversation_id=chat_log.conversation_id)
        except (KeyError, IndexError, ValueError) as err:
            _LOGGER.error("voice_bridge: bad reply from %s: %s", url, err)
            response.async_set_error(
                intent.IntentResponseErrorCode.UNKNOWN,
                "The voice bridge sent something I couldn't read.")
            return conversation.ConversationResult(
                response=response, conversation_id=chat_log.conversation_id)

        response.async_set_speech(str(reply).strip() or "Sent.")
        return conversation.ConversationResult(
            response=response, conversation_id=chat_log.conversation_id)
