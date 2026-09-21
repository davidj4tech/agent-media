#!/usr/bin/env python3
"""Make the recorded logs safe for a public repo, in place: `sanitize.py logs/*.jsonl`.

The envelopes are kept byte-for-byte in shape; only values are replaced:

* the account e-mail (initialize response `account`) -> "<email>"
* hook outputs (SessionStart's additionalContext carries David's memory
  excerpts) -> "<redacted: N chars>"
* thinking `signature` blobs -> "<sig>"
* the initialize response's `commands`/`agents`/`models` arrays -> names only
"""
import json
import re
import sys

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,}")


def scrub(x, key=""):
    if isinstance(x, dict):
        out = {}
        for k, v in x.items():
            if k == "signature" and isinstance(v, str) and v:
                out[k] = "<sig>"
            elif k in ("output", "stdout", "stderr") and isinstance(v, str) and len(v) > 120:
                out[k] = f"<redacted: {len(v)} chars>"
            elif k in ("commands", "agents", "models") and isinstance(v, list) and v \
                    and isinstance(v[0], dict):
                out[k] = [{"name": i.get("name") or i.get("value")} for i in v]
            else:
                out[k] = scrub(v, k)
        return out
    if isinstance(x, list):
        return [scrub(i, key) for i in x]
    if isinstance(x, str):
        return EMAIL.sub("<email>", x)
    return x


for path in sys.argv[1:]:
    lines = [json.dumps(scrub(json.loads(l))) for l in open(path)]
    open(path, "w").write("\n".join(lines) + "\n")
