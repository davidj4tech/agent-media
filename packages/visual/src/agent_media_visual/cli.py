"""`media-visual` — generate an image for a reply and push it to the canvas.

    media-visual "the spoken reply text"            image only
    media-visual --say "the spoken reply text"      speak AND show

--say fires `media say` first, detached, so speech starts immediately;
the image is generated while the reply is being spoken and cross-fades in
when ready (the "album art" pattern — speech never waits on pixels).

--session <id> keys the scene-continuity memory: consecutive replies from
one session evolve a single artwork (the Stop hook passes its session id).

Config (env):
  MEDIA_VISUAL_URL   canvas base URL(s) to push to — space- or comma-
                     separated for multiple canvases (default
                     http://127.0.0.1:8781). With multiple targets the
                     image is referenced by the FIRST target's absolute
                     /img/ URL, so the first canvas must be reachable
                     from every screen (use the tailnet URL, not
                     127.0.0.1).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

from agent_media_core.intake._env import load_env_file

from .canvas import DEFAULT_PORT
from .engines import generate_image
from .generate import shape_prompt
from .state import gc_spool, spool_dir


def _canvas_urls() -> list[str]:
    raw = os.environ.get("MEDIA_VISUAL_URL") or f"http://127.0.0.1:{DEFAULT_PORT}"
    return [u.rstrip("/") for u in raw.replace(",", " ").split() if u.strip()]


def _push_one(base: str, payload: dict) -> str:
    req = urllib.request.Request(
        base + "/show", data=json.dumps(payload).encode(),
        method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
        return ""
    except (urllib.error.URLError, OSError) as e:
        return str(e)


def _push(name: str, caption: str | None, prompt: str) -> list[str]:
    """Push to every configured canvas; returns per-target errors ("" = ok).

    Single target keeps the canvas-relative bare-name reference (robust — no
    hostname assumptions). Multiple targets get the first target's absolute
    /img/ URL, since only the pushing host holds the spool.
    """
    targets = _canvas_urls()
    image = name if len(targets) == 1 else f"{targets[0]}/img/{name}"
    payload = {"image": image, "caption": caption, "prompt": prompt}
    return [_push_one(t, payload) for t in targets]


def main() -> None:
    load_env_file("visual")
    ap = argparse.ArgumentParser(
        description="generate an image for a reply and push it to the canvas")
    ap.add_argument("text", nargs="?", help="reply text (or stdin)")
    ap.add_argument("--say", action="store_true",
                    help="also speak the text via `media say` (detached)")
    ap.add_argument("--caption", help="caption shown on the canvas")
    ap.add_argument("--session", default="",
                    help="scene-continuity key (e.g. the Claude session id)")
    ap.add_argument("--engine", help="visual engine override (default: "
                    "MEDIA_VISUAL_ENGINE, else venice)")
    ap.add_argument("--model", help="image model override for the engine")
    ap.add_argument("--no-shape", action="store_true",
                    help="skip LLM prompt shaping, use the text directly")
    args = ap.parse_args()

    text = (args.text if args.text is not None else sys.stdin.read()).strip()
    if not text:
        ap.error("no text")
    if args.model:
        os.environ["MEDIA_VISUAL_MODEL"] = args.model

    if args.say:
        subprocess.Popen(["media", "say", text], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    t0 = time.perf_counter()
    if args.no_shape:
        prompt, used_llm = text[:300], False
    else:
        prompt, used_llm = shape_prompt(text, session=args.session)
    t_shape = time.perf_counter() - t0

    t0 = time.perf_counter()
    img, err = generate_image(prompt, engine=args.engine)
    t_gen = time.perf_counter() - t0
    if img is None:
        print(f"image generation failed: {err}", file=sys.stderr)
        sys.exit(1)

    name = f"img-{int(time.time())}-{os.getpid()}.webp"
    (spool_dir() / name).write_bytes(img)
    gc_spool()

    errors = _push(name, args.caption, prompt)
    targets = _canvas_urls()
    for target, e in zip(targets, errors):
        if e:
            print(f"push to {target} failed: {e}", file=sys.stderr)
    if all(errors):
        print(f"generated {name} but every push failed", file=sys.stderr)
        sys.exit(1)

    shown = sum(1 for e in errors if not e)
    print(f"shown: {name}  ({len(img)//1024} KiB, {shown}/{len(targets)} canvases)\n"
          f"prompt ({'llm' if used_llm else 'fallback'}, {t_shape:.1f}s): {prompt}\n"
          f"image: {t_gen:.1f}s")


if __name__ == "__main__":
    main()
