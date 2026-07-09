"""`media-visual` — generate image(s) for a reply and push them to the canvas.

    media-visual "the spoken reply text"            image only
    media-visual --say "the spoken reply text"      speak AND show

--say fires `media say` first, detached, so speech starts immediately;
the image is generated while the reply is being spoken and cross-fades in
when ready (the "album art" pattern — speech never waits on pixels).

--session <id> keys the scene-continuity memory: consecutive replies from
one session evolve a single artwork (the Stop hook passes its session id).

**Beats** (default on for multi-part replies, `--no-beats` /
MEDIA_VISUAL_BEATS=0 off): the reply is split into up to
MEDIA_VISUAL_BEATS_MAX (4) parts, one storyboard call turns the scene into
a prompt per part, the images generate concurrently, and the canvas is sent
a *sequence* with per-part time fractions plus an estimated spoken duration
(chars ÷ MEDIA_VISUAL_CHARS_PER_SEC). The page then flips beats in step
with the voice and parks on the final beat when speech ends. Any failure
along the way falls back to the single-image path.

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
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from agent_media_core.intake._env import load_env_file

from .canvas import DEFAULT_PORT
from .engines import generate_image
from .generate import shape_prompt, shape_story
from .state import gc_spool, save_push, spool_dir
from .state import save_scene as state_save_scene

DEFAULT_BEATS_MAX = 4
DEFAULT_CHARS_PER_SEC = 14  # rough TTS pace for the spoken-duration estimate


def _canvas_urls() -> list[str]:
    raw = os.environ.get("MEDIA_VISUAL_URL") or f"http://127.0.0.1:{DEFAULT_PORT}"
    return [u.rstrip("/") for u in raw.replace(",", " ").split() if u.strip()]


def _image_ref(name: str, targets: list[str]) -> str:
    """Single target keeps the canvas-relative bare-name reference (robust —
    no hostname assumptions). Multiple targets get the first target's absolute
    /img/ URL, since only the pushing host holds the spool."""
    return name if len(targets) == 1 else f"{targets[0]}/img/{name}"


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


def _push_all(payload_for: "callable") -> tuple[list[str], dict]:
    """payload_for(targets) → payload; push it to every canvas. Returns
    (per-target errors ["" = ok], the payload as pushed)."""
    targets = _canvas_urls()
    payload = payload_for(targets)
    return [_push_one(t, payload) for t in targets], payload


# --- beat splitting ------------------------------------------------------------

_FENCE = re.compile(r"```.*?```", re.S)
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _beats_engine(cli_engine: str | None) -> str | None:
    """The engine for beat images: an explicit --engine wins, then
    MEDIA_VISUAL_BEATS_ENGINE, then the normal resolution (None). Exists
    because beats live or die by latency — e.g. svg (slow, ~90s for a
    sequence) as the single-image engine with venice (~6s) for beats keeps
    the sequences actually synced to the voice."""
    return cli_engine or os.environ.get("MEDIA_VISUAL_BEATS_ENGINE") or None


def _beats_max() -> int:
    try:
        v = int(os.environ.get("MEDIA_VISUAL_BEATS_MAX", "") or DEFAULT_BEATS_MAX)
        return max(2, min(8, v))
    except ValueError:
        return DEFAULT_BEATS_MAX


def _merge_even(chunks: list[str], n: int) -> list[str]:
    """Merge contiguous chunks into `n` groups of roughly equal character
    mass, preserving order."""
    total = sum(len(c) for c in chunks) or 1
    groups: list[list[str]] = [[]]
    acc = 0
    for c in chunks:
        # Start a new group once the current one has its share — unless this
        # is the last allowed group, which takes everything remaining.
        if groups[-1] and len(groups) < n and acc >= total * len(groups) / n:
            groups.append([])
        groups[-1].append(c)
        acc += len(c)
    return ["\n\n".join(g) for g in groups]


def split_beats(text: str, max_n: int) -> list[tuple[float, str]] | None:
    """Split a reply into ≤ max_n ordered parts with their start fractions
    (cumulative character offset / total — a proxy for spoken timing).
    None when the reply doesn't warrant beats (short / single-thought).
    Fenced code blocks are dropped first: they aren't spoken verbatim, so
    they'd skew the pacing."""
    clean = _FENCE.sub(" ", text).strip()
    paras = [p.strip() for p in re.split(r"\n\s*\n", clean) if p.strip()]
    if len(paras) < 2:
        sents = [s.strip() for s in _SENT_SPLIT.split(clean) if s.strip()]
        if len(sents) < 4:
            return None
        paras = _merge_even(sents, min(max_n, len(sents) // 2))
    if len(paras) > max_n:
        paras = _merge_even(paras, max_n)
    if len(paras) < 2:
        return None
    total = sum(len(p) for p in paras) or 1
    out: list[tuple[float, str]] = []
    off = 0
    for p in paras:
        out.append((off / total, p))
        off += len(p)
    return out


def _est_duration(text: str) -> int:
    try:
        cps = float(os.environ.get("MEDIA_VISUAL_CHARS_PER_SEC", "")
                    or DEFAULT_CHARS_PER_SEC)
    except ValueError:
        cps = DEFAULT_CHARS_PER_SEC
    clean = _FENCE.sub(" ", text)
    return max(3, int(len(clean) / max(cps, 1.0)))


def _spool(img: bytes) -> str:
    # The svg engine returns markup, raster engines return webp — pick the
    # extension by sniffing so the canvas serves the right content type.
    ext = "svg" if img.lstrip()[:4] == b"<svg" else "webp"
    name = f"img-{int(time.time())}-{os.getpid()}-{_spool.n}.{ext}"
    _spool.n += 1
    (spool_dir() / name).write_bytes(img)
    return name


_spool.n = 0


def main() -> None:
    load_env_file("visual")
    ap = argparse.ArgumentParser(
        description="generate image(s) for a reply and push them to the canvas")
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
    ap.add_argument("--no-beats", action="store_true",
                    help="always a single image, even for multi-part replies")
    ap.add_argument("--hint",
                    help="author-supplied illustration description: used as "
                         "the scene directly (no shaping call, no beats) — "
                         "maximal relevance, minimal latency")
    ap.add_argument("--fast", action="store_true",
                    help="use the beats/fast engine (a reveal can't wait "
                         "a minute for clip art)")
    ap.add_argument("--key", default="",
                    help="reply identity (the intake dedup key): the pushed "
                         "payload is remembered under it so a speech replay "
                         "re-shows this reply's visual")
    args = ap.parse_args()

    text = (args.text if args.text is not None else sys.stdin.read()).strip()
    if not text:
        ap.error("no text")
    if args.model:
        os.environ["MEDIA_VISUAL_MODEL"] = args.model

    t_start = time.perf_counter()
    if args.say:
        subprocess.Popen(["media", "say", text], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    if args.fast:
        args.engine = _beats_engine(args.engine)

    # Scene + storyboard in ONE gateway call when the reply splits into
    # beats; plain scene shaping otherwise. Beats need the LLM — a raw-text
    # fallback has no scene to storyboard. An author-supplied --hint
    # short-circuits everything: it IS the scene (saved to the continuity
    # memory so later replies evolve from it), single decisive image.
    beats_on = (not args.no_beats and not args.no_shape and not args.hint
                and (os.environ.get("MEDIA_VISUAL_BEATS", "1") or "1") != "0")
    parts = split_beats(text, _beats_max()) if beats_on else None
    t0 = time.perf_counter()
    prompts = None
    if args.hint:
        scene, used_llm = args.hint.strip(), True
        # Purposeful mode for engines that can honour it (the svg engine
        # switches to its labeled-figure prompt).
        os.environ["MEDIA_VISUAL_FIGURE"] = "1"
        state_save_scene(args.session, scene)
    elif args.no_shape:
        scene, used_llm = text[:300], False
    elif parts:
        scene, prompts, used_llm = shape_story(
            text, [p for _, p in parts], session=args.session)
    else:
        scene, used_llm = shape_prompt(text, session=args.session)
    t_shape = time.perf_counter() - t0

    # --- beats path: concurrent generation + sequence push -------------------
    # Any failure (few surviving images, dead canvases) falls through to the
    # single-image path below.
    if prompts:
        beat_engine = _beats_engine(args.engine)
        with ThreadPoolExecutor(max_workers=len(prompts)) as pool:
            results = list(pool.map(
                lambda p: generate_image(p, engine=beat_engine), prompts))
        beats = [(frac, img) for (frac, _), (img, _err)
                 in zip(parts, results) if img is not None]
        if len(beats) >= 2:
            named = [(_spool(img), frac) for frac, img in beats]
            gc_spool()
            gen_secs = time.perf_counter() - t_start
            errors, payload = _push_all(lambda targets: {
                "sequence": [{"image": _image_ref(n, targets),
                              "at": round(frac, 3)} for n, frac in named],
                "caption": args.caption,
                "prompt": scene,
                "estdur": _est_duration(text),
                "gen_secs": round(gen_secs, 1),
            })
            _report_pushes(errors)
            if any(not e for e in errors):
                save_push(args.key, payload)
            kib = sum((spool_dir() / n).stat().st_size for n, _ in named) // 1024
            print(f"shown: {len(named)} beats  ({kib} KiB)\n"
                  f"scene (llm, {t_shape:.1f}s): {scene}\n"
                  f"beats: {gen_secs - t_shape:.1f}s")
            return

    t0 = time.perf_counter()
    img, err = generate_image(scene, engine=args.engine)
    t_gen = time.perf_counter() - t0
    if img is None:
        print(f"image generation failed: {err}", file=sys.stderr)
        sys.exit(1)

    name = _spool(img)
    gc_spool()
    errors, payload = _push_all(lambda targets: {
        "image": _image_ref(name, targets),
        "caption": args.caption,
        "prompt": scene,
        # Purposeful figure (author-hinted) vs ambient art — the canvas
        # badges figures and gives them their own arrival sound.
        "purpose": "figure" if args.hint else None,
    })
    _report_pushes(errors)
    if any(not e for e in errors):
        save_push(args.key, payload)
    print(f"shown: {name}  ({len(img)//1024} KiB, "
          f"{sum(1 for e in errors if not e)}/{len(errors)} canvases)\n"
          f"prompt ({'llm' if used_llm else 'fallback'}, {t_shape:.1f}s): {scene}\n"
          f"image: {t_gen:.1f}s")


def _report_pushes(errors: list[str]) -> None:
    targets = _canvas_urls()
    for target, e in zip(targets, errors):
        if e:
            print(f"push to {target} failed: {e}", file=sys.stderr)
    if all(errors):
        print("every push failed", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
