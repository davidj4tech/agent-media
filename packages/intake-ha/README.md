# agent-media-intake-ha

Home Assistant SSE intake adapter for
[agent-media](https://github.com/davidj4tech/agent-media): subscribes to a Home
Assistant event stream and speaks matching events. Extracted from core as an
optional, separately-installable intake source.

```bash
pip install agent-media-intake-ha   # pulls in agent-media-core
media-intake-ha-sse                  # run the daemon
```

Config (environment): `HA_URL`, `HA_TOKEN`, event-type settings. See the core
repo's `docs/EXTENSIONS.md` (§2 Intake adapters).
