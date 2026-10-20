"""Named app players in the shared output pickers, over outbound streams."""

from __future__ import annotations

from . import devices, speech_frames

PREFIX = "device:"


def options(channel: str) -> list[dict]:
    hub = speech_frames._HUBS[channel]
    with hub.lock:
        connected = {v[0] for v in hub.listeners.values()}
    selected = speech_frames.output_device(channel)
    names = {d["id"]: d.get("name") or d["id"] for d in devices.list_devices()}
    return [{"name": PREFIX + device, "label": names.get(device, device),
             "available": device in connected,
             "why": None if device in connected else "Device is not connected"}
            for device in sorted(connected | ({selected} if selected else set()),
                                 key=lambda d: names.get(d, d).lower())]


def block(channel: str, base: dict) -> dict:
    """Overlay the actual app destination without changing the sink name."""
    from agent_media_core import audio_targets

    ready = (audio_targets.speech_configured("sasonica")[0] if channel == "speech"
             else audio_targets.music_available("sasonica")[0])
    if not ready:
        return base
    offered = options(channel)
    if not offered:
        return base
    selected = speech_frames.output_device(channel)
    current = speech_frames._HUBS[channel].device()
    generic = [{**o, "label": "Automatic app player", "why": "The most recently connected app"}
               if o.get("name") == "sasonica" else o for o in base.get("options", [])]
    result = {**base, "options": [*generic, *offered]}
    if base.get("current") == "sasonica" and current:
        result["current"] = PREFIX + current
    if selected:
        if channel == "speech" and base.get("current") == "sasonica":
            result.update(current=PREFIX + selected, overridden=True)
        if channel == "music" and base.get("next") == "sasonica":
            result.update(next=PREFIX + selected, overridden=True)
    return result


def select(channel: str, target: str | None) -> str | None:
    """Select the named frame player; the underlying sink stays sasonica."""
    if target and target.startswith(PREFIX):
        device = target[len(PREFIX):]
        if device not in {d["id"] for d in devices.list_devices()}:
            raise ValueError("unknown paired device")
        speech_frames.select_output(channel, device)
        return "sasonica"
    if target in (None, "", "sasonica"):
        speech_frames.select_output(channel, None)
    return target
