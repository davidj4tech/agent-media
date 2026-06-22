# agent-media-intake-matrix

Matrix intake adapter for [agent-media](https://github.com/davidj4tech/agent-media):
bridges a Matrix room to the speech / music / book channels. Extracted from core
as an optional, separately-installable intake source.

```bash
pip install agent-media-intake-matrix   # pulls in agent-media-core
media-intake-matrix                      # run the daemon
```

Config (environment): `MATRIX_HOMESERVER`, `MATRIX_ACCESS_TOKEN`,
`MATRIX_SAM_ID`, `MATRIX_CONTROL_IDS`, `MATRIX_ROOM_ALLOW`. Uses only the
standard library for the Matrix protocol. See the core repo's
`docs/EXTENSIONS.md` (§2 Intake adapters) for the convention.
