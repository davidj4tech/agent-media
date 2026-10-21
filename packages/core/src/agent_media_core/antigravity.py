"""Managed Antigravity transcripts, independent of Gemini CLI's store.

The native conversation ID belongs to agy; the app ID stays stable across
process restarts. The session host exports messages in our common file schema.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
from datetime import datetime, timezone


def root() -> Path:
    return Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state') / 'agent-media/antigravity'


def path(session: str) -> Path:
    import re
    if not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', session):
        raise ValueError('invalid Antigravity session ID')
    return root() / f'{session}.jsonl'


def append(session: str, record: dict) -> None:
    import uuid
    p = path(session)
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    record = {'uuid': str(uuid.uuid4()), 'timestamp': datetime.now(timezone.utc).isoformat(), **record}
    fd = os.open(p, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'a') as f:
        f.write(json.dumps(record, separators=(',', ':')) + '\n')
