"""`media-visual` — generate an image for a reply and push it to the canvas.

    media-visual "the spoken reply text"            image only
    media-visual --say "the spoken reply text"      speak AND show

--say fires `media say` first, detached, so speech starts immediately;
the image is generated while the reply is being spoken and cross-fades in
when ready (the "album art" pattern — speech never waits on pixels).

Config (env):
  MEDIA_VISUAL_URL   canvas base URL to push to (default http://127.0.0.1:8781)
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

from .canvas import DEFAULT_PORT, spool_dir
from .generate import generate_image, shape_prompt


def _canvas_url() -> str:
    return (os.environ.get("MEDIA_VISUAL_URL")
            or f"http://127.0.0.1:{DEFAULT_PORT}").rstrip("/")


def _push(payload: dict) -> str:
    req = urllib.request.Request(
        _canvas_url() + "/show", data=json.dumps(payload).encode(),
        method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
        return ""
    except (urllib.error.URLError, OSError) as e:
        return str(e)


def main() -> None:
    load_env_file("visual")
    ap = argparse.ArgumentParser(
        description="generate an image for a reply and push it to the canvas")
    ap.add_argument("text", nargs="?", help="reply text (or stdin)")
    ap.add_argument("--say", action="store_true",
                    help="also speak the text via `media say` (detached)")
    ap.add_argument("--caption", help="caption shown on the canvas")
    ap.add_argument("--model", help="Venice image model override")
    ap.add_argument("--no-shape", action="store_true",
                    help="skip LLM prompt shaping, use the text directly")
    args = ap.parse_args()

    text = (args.text if args.text is not None else sys.stdin.read()).strip()
    if not text:
        ap.error("no text")

    if args.say:
        subprocess.Popen(["media", "say", text], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    t0 = time.perf_counter()
    if args.no_shape:
        prompt, used_llm = text[:300], False
    else:
        prompt, used_llm = shape_prompt(text)
    t_shape = time.perf_counter() - t0

    t0 = time.perf_counter()
    img, err = generate_image(prompt, model=args.model)
    t_gen = time.perf_counter() - t0
    if img is None:
        print(f"image generation failed: {err}", file=sys.stderr)
        sys.exit(1)

    name = f"img-{int(time.time())}-{os.getpid()}.webp"
    (spool_dir() / name).write_bytes(img)

    err = _push({"image": name, "caption": args.caption, "prompt": prompt})
    if err:
        print(f"generated {name} but push failed: {err}", file=sys.stderr)
        sys.exit(1)

    print(f"shown: {name}  ({len(img)//1024} KiB)\n"
          f"prompt ({'llm' if used_llm else 'fallback'}, {t_shape:.1f}s): {prompt}\n"
          f"image: {t_gen:.1f}s")


if __name__ == "__main__":
    main()
