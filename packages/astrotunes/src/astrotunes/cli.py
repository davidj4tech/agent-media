from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from . import __version__
from .natal import load_natal, NatalData
from .natal.sacred import to_dict as natal_to_dict
from .weather import fetch_melbourne_weather


def _time_of_day(now: datetime) -> str:
    h = now.hour
    if h < 5: return "late night"
    if h < 8: return "early morning"
    if h < 12: return "morning"
    if h < 14: return "midday"
    if h < 17: return "afternoon"
    if h < 20: return "evening"
    if h < 23: return "night"
    return "late night"


def cmd_context(args: argparse.Namespace) -> int:
    natal = load_natal()
    now = datetime.now(ZoneInfo(natal.timezone))

    ctx: dict = {
        "now": now.isoformat(),
        "time_of_day": _time_of_day(now),
        "natal": natal_to_dict(natal),
    }

    # Human Design bodygraph — static chart, plus today's transit overlay.
    bg = None
    try:
        from .humandesign import compute_bodygraph
        bg = compute_bodygraph(natal)
        ctx["natal"]["human_design"] = bg.to_dict()
    except RuntimeError as e:
        ctx["natal"]["human_design_error"] = str(e)

    try:
        from .transits import compute
        ctx["transits"] = compute(natal, now).to_dict()
    except RuntimeError as e:
        ctx["transits_error"] = str(e)

    if bg is not None:
        try:
            ctx.setdefault("transits", {})
            ctx["transits"]["human_design"] = bg.transit_overlay(now)
        except Exception as e:
            ctx["transits"].setdefault("human_design_error", str(e))

    if not args.no_weather:
        try:
            ctx["weather"] = fetch_melbourne_weather()
        except Exception as e:
            ctx["weather_error"] = str(e)

    # Information sources (raga, gene keys, sabian + config-gated features /
    # last.fm / home-assistant). Run before qualities so their nudges fold in.
    try:
        from .sources import gather
        ctx["sources"] = gather(ctx)
    except Exception as e:
        ctx["sources_error"] = str(e)

    # Translation layer: turn the computed facts (+ source nudges) into an
    # explicit, tweakable musical brief. Track selection stays downstream.
    try:
        from .qualities import derive
        ctx["qualities"] = derive(ctx)
    except Exception as e:
        ctx["qualities_error"] = str(e)

    json.dump(ctx, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


def cmd_recommend(args: argparse.Namespace) -> int:
    sys.stderr.write(
        "astrotunes recommend is a stub. The recommendation logic lives in "
        "the Claude Code skill at ~/.claude/skills/music-transit/SKILL.md, "
        "which calls `astrotunes context` and synthesizes picks from there.\n"
        "If you want a no-Claude pick, this CLI doesn't do it yet.\n"
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="astrotunes")
    parser.add_argument("--version", action="version", version=f"astrotunes {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_ctx = sub.add_parser("context", help="Print current transit + weather + time context as JSON")
    p_ctx.add_argument("--no-weather", action="store_true", help="Skip weather fetch")
    p_ctx.set_defaults(func=cmd_context)

    p_rec = sub.add_parser("recommend", help="(stub) Recommend tracks based on context")
    p_rec.add_argument("--queue", choices=["stdout", "mpd", "media-mcp"], default="stdout")
    p_rec.set_defaults(func=cmd_recommend)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
