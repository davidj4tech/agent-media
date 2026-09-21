#!/usr/bin/env python3
"""Print a compact timeline of a spike log: `show.py logs/<name>.jsonl [--full]`.

Stream-event deltas are skipped unless --full; long payloads are trimmed.
"""
import json
import sys

full = "--full" in sys.argv
for line in open(sys.argv[1]):
    r = json.loads(line)
    o = r.get("obj")
    if o is None:
        print(r["t"], r["dir"], r.get("line", "")[:300])
        continue
    ty = o.get("type")
    if ty == "stream_event" and not full:
        ev = o["event"]
        if ev["type"] in ("message_start", "message_stop"):
            print(r["t"], r["dir"], "stream_event", ev["type"])
        continue
    if ty == "rate_limit_event":
        continue
    s = json.dumps(o)
    if ty == "system" and o.get("subtype") == "init":
        s = "system/init"
    elif ty == "control_response" and '"commands"' in s[:400]:
        s = "control_response(initialize) ..."
    elif ty in ("assistant", "user") and r["dir"] == "out":
        c = o["message"]["content"]
        c = c if isinstance(c, list) else [{"type": "text", "text": c}]
        s = ty + " " + json.dumps([{k: (v[:160] if isinstance(v, str) else v)
                                     for k, v in b.items() if k != "signature"} for b in c])[:700]
        extra = {k: o[k] for k in o if k not in ("message", "session_id", "uuid", "type",
                                                  "parent_tool_use_id", "timestamp", "request_id")}
        if extra:
            s += " " + json.dumps(extra)[:300]
    elif ty == "result":
        s = "result " + json.dumps({k: o.get(k) for k in (
            "subtype", "is_error", "result", "stop_reason", "terminal_reason", "num_turns",
            "queued_turn_count", "result_index", "ttft_ms", "duration_ms", "total_cost_usd",
            "permission_denials")})
    print(r["t"], r["dir"], s[:1200])
