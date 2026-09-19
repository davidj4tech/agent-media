# agent-media-engine-piper

Offline [Piper](https://github.com/OHF-Voice/piper1-gpl) TTS render engine for
agent-media. It POSTs text to Piper's own HTTP server and writes back WAV. Piper
needs no network once a voice is on disk, no API key and no GPU, so a server on
the same host is the engine that still speaks when the tailnet and the cloud
engines are down. On any failure it returns `(False, err)` so core falls back.

## Use

```sh
pip install -e packages/engine-piper
export MEDIA_RENDER_ENGINE=piper                 # or MEDIA_RENDER_FALLBACK_ENGINE=piper
export MEDIA_PIPER_BASE_URL=http://127.0.0.1:5000
```

Config (env): `MEDIA_PIPER_BASE_URL`, `MEDIA_RENDER_VOICE_PIPER` (default: the
server's own voice), `MEDIA_PIPER_DOWNLOAD` (`0` = never ask the server to fetch
a missing voice), `MEDIA_PIPER_SPEED` (default `1.0`, higher is faster),
`MEDIA_PIPER_SPEAKER` (speaker id for multi-speaker voices),
`MEDIA_PIPER_TIMEOUT_S`.

## Server

Piper runs as its own process, installed on its own, apart from agent-media:

```sh
python3 -m venv ~/.local/share/piper/venv
py=~/.local/share/piper/venv/bin/python
$py -m pip install 'piper-tts[http]'
piper_voices=~/.local/share/piper/voices
$py -m piper.download_voices en_GB-alba-medium --data-dir "$piper_voices"
$py -m piper.http_server -m en_GB-alba-medium --data-dir "$piper_voices" \
    --download-dir "$piper_voices" --host 127.0.0.1 --port 5000
```

Pass `--download-dir` as well: `--data-dir` adds to a list that starts with
the current directory, and downloads go to the first entry. The server's `-m`
voice is the default. The engine asks the server to download
any other named voice the first time it's requested.

## Licence

Piper is GPL-3.0. This package is MIT and stdlib-only: it talks to Piper over
HTTP and never imports or ships it, so Piper stays a separate program. Keep it
that way. An `import piper` here would bring the package under the GPL.

Voice models carry their own licences (see each voice's `MODEL_CARD` on
[rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices)), and some
don't allow commercial use. Check a voice before making it a default anyone pays for.
