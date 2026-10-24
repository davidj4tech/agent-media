"""Read the phone's listen log and say why listens came back empty.

David, 25 Oct 2026: "the mic doesn't kick in sometimes after your message in
hands-free". The phone reports each mic listen to `POST /device/listen-log`
(listen_log.py), and a third of them were `empty` with no error and no echo.
Each line now says how the recogniser went (`recError`, `openMs`, `speechMs`,
`ranMs`, `chime`, `rmsPeak`, `playingAtStart`, `interlude`), so the cause can
be read at once:

    python -m agent_media_server.listen_summary [--hours 24] [--at handsfree]
                                               [--file PATH] [--json]

For the empties it prints how many were:

- never opened      `openMs` -1: the mic never became ready (audio focus, the
                    earbud route, a recogniser that did not start)
- slow to open      `openMs` over SLOW_OPEN_MS: the words had begun before it
                    listened
- no speech heard   opened, but `speechMs` -1: nothing the recogniser took for
                    speech began
- speech, no words  `speechMs` set, still empty: heard something, understood
                    nothing (no match)
- quiet             `rmsPeak` under QUIET_DB: the mic barely heard anything
- a voice playing   `playingAtStart`: the phone was talking as it opened
- an interlude      hands-free's opening between spoken updates

(The counts overlap; each empty is also put under one main cause, in that
order.) Then the recogniser's error codes, and, for lines from a phone that
does not send them yet, how many carried no diagnostics. Lines older than the
fields just count as `no diagnostics`.
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
import time
from pathlib import Path

from agent_media_core._paths import state_dir

#: Over this many ms to open the mic is slow (the phone takes about 1.5 s).
SLOW_OPEN_MS = 2500
#: A peak under this many dB means the mic heard next to nothing.
QUIET_DB = 3.0

#: (key, label, test) — in the order a listen's main cause is chosen.
CAUSES = (
    ("never_opened", "mic never opened (openMs -1)", lambda l: l["openMs"] == -1),
    ("slow_open", f"slow to open (openMs over {SLOW_OPEN_MS})", lambda l: l["openMs"] > SLOW_OPEN_MS),
    ("no_speech", "opened, no speech began (speechMs -1)", lambda l: l.get("speechMs") == -1),
    ("speech_no_words", "speech began, no words understood", lambda l: l.get("speechMs", -1) >= 0),
)
#: Independent flags, counted whatever the main cause.
FLAGS = (
    ("quiet", f"quiet (rmsPeak under {QUIET_DB} dB)", lambda l: isinstance(l.get("rmsPeak"), (int, float)) and l["rmsPeak"] < QUIET_DB),
    ("voice_playing", "a voice was playing as it opened", lambda l: l.get("playingAtStart") is True),
    ("interlude", "a hands-free opening between spoken updates", lambda l: l.get("interlude") is True),
    ("no_chime", "no chime", lambda l: l.get("chime") is False),
)


def default_path() -> Path:
    return state_dir() / "listen-log.jsonl"


def read(path: Path, since: float | None = None) -> list[dict]:
    """The log's lines, oldest first; those received before `since` left out."""
    out: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for raw in text.splitlines():
        try:
            line = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(line, dict):
            continue
        if since is not None and float(line.get("received") or 0) < since:
            continue
        out.append(line)
    return out


def _has_diagnostics(line: dict) -> bool:
    return isinstance(line.get("openMs"), (int, float)) and not isinstance(line.get("openMs"), bool)


def summarise(lines: list[dict], at: str | None = None) -> dict:
    """Counts for the listens (of `at`, when given): by outcome, and why the
    empty ones were empty."""
    if at:
        lines = [l for l in lines if l.get("at") == at]
    outcomes = collections.Counter(l.get("outcome") or "?" for l in lines)
    empties = [l for l in lines if l.get("outcome") == "empty"]
    diag = [l for l in empties if _has_diagnostics(l)]
    main = collections.Counter()
    for l in diag:
        for key, _label, test in CAUSES:
            if test(l):
                main[key] += 1
                break
        else:
            main["other"] += 1
    flags = {key: sum(1 for l in diag if test(l)) for key, _label, test in FLAGS}
    return {
        "listens": len(lines),
        "outcomes": dict(outcomes),
        "empty": len(empties),
        "empty_with_diagnostics": len(diag),
        "empty_without_diagnostics": len(empties) - len(diag),
        "main_cause": dict(main),
        "also": flags,
        "rec_errors": dict(collections.Counter(str(l["recError"]) for l in diag if "recError" in l)),
        "open_ms": _spread([l["openMs"] for l in diag if l["openMs"] >= 0]),
        "rms_peak": _spread([l["rmsPeak"] for l in diag if isinstance(l.get("rmsPeak"), (int, float))]),
    }


def _spread(values: list) -> dict | None:
    if not values:
        return None
    v = sorted(values)
    return {"n": len(v), "min": v[0], "median": v[len(v) // 2], "max": v[-1]}


def render(s: dict) -> str:
    out = []
    oc = ", ".join(f"{k} {n}" for k, n in sorted(s["outcomes"].items(), key=lambda kv: -kv[1]))
    out.append(f"{s['listens']} listens: {oc or 'none'}")
    if not s["empty"]:
        return "\n".join(out + ["no empty listens"])
    out.append(f"{s['empty']} empty, {s['empty_with_diagnostics']} with diagnostics"
               f" ({s['empty_without_diagnostics']} from a build that did not send them)")
    if s["empty_with_diagnostics"]:
        out.append("main cause of the empties:")
        labels = {k: label for k, label, _t in CAUSES}
        labels["other"] = "opened in time, heard speech, no code for it"
        for key, n in sorted(s["main_cause"].items(), key=lambda kv: -kv[1]):
            out.append(f"  {n:4d}  {labels[key]}")
        out.append("also (overlapping):")
        for key, label, _t in FLAGS:
            out.append(f"  {s['also'][key]:4d}  {label}")
        if s["rec_errors"]:
            out.append("recogniser error codes (0 results, 6 speech timeout, 7 no match, 11 server disconnected…): "
                       + ", ".join(f"{k}: {n}" for k, n in sorted(s["rec_errors"].items())))
        for name, key in (("ms to open", "open_ms"), ("rmsPeak dB", "rms_peak")):
            sp = s[key]
            if sp:
                out.append(f"{name}: n {sp['n']}, min {sp['min']}, median {sp['median']}, max {sp['max']}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="listen_summary", description=__doc__.split("\n\n")[0])
    ap.add_argument("--hours", type=float, default=0, help="only listens received in the last N hours (default all)")
    ap.add_argument("--at", default="", help="only this kind of listen: handsfree, countdown, confirm, dictation")
    ap.add_argument("--file", default="", help="the log (default <state>/listen-log.jsonl)")
    ap.add_argument("--json", action="store_true", help="the counts as JSON")
    a = ap.parse_args(argv)
    path = Path(a.file) if a.file else default_path()
    since = time.time() - a.hours * 3600 if a.hours > 0 else None
    lines = read(path, since)
    if not lines:
        print(f"no listens in {path}", file=sys.stderr)
        return 1
    s = summarise(lines, a.at or None)
    print(json.dumps(s, indent=2) if a.json else render(s))
    return 0


if __name__ == "__main__":
    sys.exit(main())
