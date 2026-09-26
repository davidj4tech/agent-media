"""Watch the Microsoft voice the phone renders Natasha with.

Sasonica asks Edge's Read Aloud service for Natasha itself (the app's
`speech/EdgeVoice.java`), a service nobody offers: it is spoken the way the
edge-tts project found it spoken, and when Microsoft changes the handshake
every sentence quietly falls back to Google's voice. The fallback keeps the
phone talking, which is also why nobody would notice. So, every run:

1. The app's own request, made from here: the token and browser version are
   read out of EdgeVoice.java in the app's checkout, so this tests what the
   phone sends, not what it ought to. No audio back is `needs`.
2. edge-tts upstream: the same two values in the project's constants.py.
   When they move, Microsoft has usually just changed something — `warn`,
   while the app still works, is the early warning.

The phone raises its own alarm the moment a sentence falls back (the app's
VoiceAlerts, alert `edge-voice-phone`, David: "shouldn't the fallback be the
trigger"). This daily check is the early warning that can't: edge-tts moving
on before anything has broken. It reports every run through agent-alert (the
alert store keeps the edges) and keeps no state of its own.

    media-edge-voice-watch [--app PATH] [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

ALERT_ID = "edge-voice"
APP_REPO = Path.home() / "projects/sasonica-app"
APP_PATH = "android/app/src/main/java/com/sasonica/app/speech/EdgeVoice.java"
UPSTREAM = "https://raw.githubusercontent.com/rany2/edge-tts/master/src/edge_tts/constants.py"
VOICE = "en-AU-NatashaNeural"
FIX = ("Natasha's handshake with Microsoft has moved: update TOKEN/CHROMIUM in "
       "sasonica-app android/app/src/main/java/com/sasonica/app/speech/EdgeVoice.java "
       "to match edge-tts's src/edge_tts/constants.py (and its drm.py if the "
       "Sec-MS-GEC recipe changed), push, and install the CI build on p8a.")
WIN_EPOCH = 11644473600


def app_source(repo: Path) -> str:
    """EdgeVoice.java as pushed — what CI builds for the phone, not whatever
    a session has half-edited in the shared checkout."""
    subprocess.run(["git", "-C", str(repo), "fetch", "-q", "origin", "main"],
                   capture_output=True, timeout=60)
    r = subprocess.run(["git", "-C", str(repo), "show", f"origin/main:{APP_PATH}"],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise ValueError(f"no {APP_PATH} on origin/main: {r.stderr.strip()}")
    return r.stdout


def app_constants(text: str) -> dict:
    """TOKEN and CHROMIUM as the app has them."""
    out = {}
    for key in ("TOKEN", "CHROMIUM"):
        m = re.search(rf'static final String {key} = "([^"]+)"', text)
        if not m:
            raise ValueError(f"{key} not found in EdgeVoice.java")
        out[key] = m.group(1)
    return out


def upstream_constants(text: str) -> dict:
    """The same two values in edge-tts's constants.py."""
    out = {}
    m = re.search(r'TRUSTED_CLIENT_TOKEN\s*=\s*"([^"]+)"', text)
    if m:
        out["TOKEN"] = m.group(1)
    m = re.search(r'CHROMIUM_FULL_VERSION\s*=\s*"([^"]+)"', text)
    if m:
        out["CHROMIUM"] = m.group(1)
    return out


def sec_ms_gec(token: str, unix_s: float) -> str:
    s = int(unix_s) + WIN_EPOCH
    s -= s % 300
    return hashlib.sha256(f"{s}0000000{token}".encode("ascii")).hexdigest().upper()


async def probe(c: dict, text: str = "Checking the voice.", timeout: float = 15.0) -> tuple[int, str]:
    """Ask for `text` the way the app does. (audio bytes, "" or why not)."""
    import aiohttp

    major = c["CHROMIUM"].split(".", 1)[0]
    url = ("wss://speech.platform.bing.com/consumer/speech/synthesize/readaloud/edge/v1"
           f"?TrustedClientToken={c['TOKEN']}&ConnectionId={uuid.uuid4().hex}"
           f"&Sec-MS-GEC={sec_ms_gec(c['TOKEN'], time.time())}"
           f"&Sec-MS-GEC-Version=1-{c['CHROMIUM']}")
    headers = {
        "Pragma": "no-cache", "Cache-Control": "no-cache",
        "Origin": "chrome-extension://jdiccldimpdaibmpdkjnbmckianbfold",
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                       f" (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36 Edg/{major}.0.0.0"),
        "Accept-Language": "en-US,en;q=0.9",
        "Cookie": f"muid={uuid.uuid4().hex.upper()};",
    }
    stamp = time.strftime("%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)", time.gmtime())
    got = 0
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
            async with s.ws_connect(url, headers=headers) as ws:
                await ws.send_str(
                    f"X-Timestamp:{stamp}\r\nContent-Type:application/json; charset=utf-8\r\n"
                    "Path:speech.config\r\n\r\n"
                    '{"context":{"synthesis":{"audio":{"metadataoptions":{'
                    '"sentenceBoundaryEnabled":"false","wordBoundaryEnabled":"false"},'
                    '"outputFormat":"audio-24khz-48kbitrate-mono-mp3"}}}}\r\n')
                await ws.send_str(
                    f"X-RequestId:{uuid.uuid4().hex}\r\nContent-Type:application/ssml+xml\r\n"
                    f"X-Timestamp:{stamp}Z\r\nPath:ssml\r\n\r\n"
                    "<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xml:lang='en-US'>"
                    f"<voice name='{VOICE}'><prosody pitch='+0Hz' rate='+0%' volume='+0%'>{text}"
                    "</prosody></voice></speak>")
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.BINARY and len(msg.data) > 2:
                        hl = int.from_bytes(msg.data[:2], "big")
                        if b"Path:audio" in msg.data[2:2 + hl]:
                            got += len(msg.data) - 2 - hl
                    elif msg.type == aiohttp.WSMsgType.TEXT and "Path:turn.end" in msg.data:
                        break
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
    except aiohttp.WSServerHandshakeError as e:
        return got, f"Microsoft refused the handshake ({e.status})"
    except Exception as e:  # noqa: BLE001
        return got, f"{type(e).__name__}: {e}"
    return got, "" if got else "the service answered but sent no audio"


def verdict(app: dict, upstream: dict | None, probed: tuple[int, str]) -> tuple[str, str, str]:
    """(level, title, detail) for agent-alert."""
    audio, why = probed
    drift = [f"{k}: app {app[k]}, edge-tts {upstream[k]}"
             for k in ("TOKEN", "CHROMIUM") if upstream and upstream.get(k) and upstream[k] != app[k]]
    if why:
        detail = f"The app's request failed: {why}. Replies fall back to Google's voice."
        if drift:
            detail += " edge-tts has moved on — " + "; ".join(drift) + "."
        return "needs", "Natasha on the phone is failing", detail
    if drift:
        return ("warn", "edge-tts changed Microsoft's handshake",
                "The app still gets audio, but edge-tts has updated: " + "; ".join(drift)
                + ". Microsoft usually changed something; the app may start falling back soon.")
    return "ok", "Natasha on the phone works", f"{audio} bytes of audio for the app's own request."


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="media-edge-voice-watch", description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", type=Path, default=APP_REPO, help="the sasonica-app checkout")
    ap.add_argument("--dry-run", action="store_true", help="print the verdict, report nothing")
    a = ap.parse_args(argv)

    try:
        app = app_constants(app_source(a.repo))
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        level, title, detail = "warn", "Natasha watch can't read the app", str(e)
    else:
        try:
            with urllib.request.urlopen(UPSTREAM, timeout=20) as r:
                upstream = upstream_constants(r.read().decode("utf-8"))
        except Exception:  # noqa: BLE001 — GitHub being down is not the voice failing
            upstream = None
        level, title, detail = verdict(app, upstream, asyncio.run(probe(app)))

    print(f"{level}: {title} — {detail}")
    if a.dry_run:
        return 0
    cmd = [os.path.expanduser("~/.local/bin/agent-alert"), "report", ALERT_ID,
           "--level", level, "--title", title, "--detail", detail, "--every", str(86400)]
    if level != "ok":
        cmd += ["--fix", FIX]
    return subprocess.call(cmd)


if __name__ == "__main__":
    sys.exit(main())
