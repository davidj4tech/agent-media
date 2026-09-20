"""The `pattern` engine: generative SVG art with no model and no network.

Every other engine in this package costs something — an image API key
(venice) or a gateway LLM (svg). This one costs nothing at all: it composes
the artwork itself, in-process, from the reply text. That makes it the
engine a *distributable* build can default to, where the installer has no
key, no account and no local model. The paid engines stay available; they
are now the upgrade, not the floor.

How a reply becomes a picture:

1. **Seed.** A blake2b digest of the text seeds a ``random.Random``. Same
   reply, same artwork, forever — which also makes golden-file tests possible.
2. **Motif.** The text is scored against a keyword table (speech and audio
   words pull `waves`, connection words pull `network`, and so on). The
   winning family decides what is drawn; the seed decides the particulars.
3. **Palette.** One of a set of hand-tuned schemes, chosen by the seed and
   filtered by mode: dark (default), light, or `mono` for e-ink.
4. **Compose.** Background, setting, motif, and a few gentle SMIL loops.

**Continuity and beats.** The contract is ``generate(prompt)`` — one string,
config from the environment — so the scene identity travels in the
environment too, as every engine's config does:

  MEDIA_VISUAL_PATTERN_SEED     scene key (the CLI passes the session id):
                                consecutive replies in one session share a
                                palette, so the canvas reads as one body of
                                work rather than a slideshow.
  MEDIA_VISUAL_PATTERN_SUBJECT  the WHOLE reply, when the prompt is only a
                                part of it: the motif is chosen from this, so
                                every beat of a storyboard draws the same
                                subject.
  MEDIA_VISUAL_BEAT             "i/n" — this beat's place in the sequence.
                                Drives `progress`, which grows the subject
                                across the sequence, so a storyboard is one
                                scene developing rather than N unrelated ones.
                                (The CLI renders beats sequentially for this
                                engine — it is fast enough not to need
                                threads, and the env would race if it did.)

Other config:

  MEDIA_VISUAL_DARK     dark palettes, default ON (shared with the other
                        engines — see generate.dark_mode)
  MEDIA_VISUAL_MONO     1 = four flat greys, no gradients, no partial
                        opacity: what a DU4 e-ink panel can actually show
  MEDIA_VISUAL_FIGURE   1 = purposeful mode: label the subject with the
                        reply's own key words (vector text stays crisp)
"""

from __future__ import annotations

import hashlib
import math
import os
import random
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Tuple

from .generate import dark_mode

W, H = 1600, 900


# --- palettes -----------------------------------------------------------------

@dataclass
class Palette:
    """bg is (top, bottom) of the backdrop gradient; ink draws quiet
    structure; accents[0] is the subject's principal colour."""
    name: str
    bg: Tuple[str, str]
    ink: str
    accents: List[str]
    flat: bool = False  # no gradients, no partial opacity (e-ink)
    glow: bool = True

    def accent(self, i: int) -> str:
        return self.accents[i % len(self.accents)]


DARK_PALETTES = [
    Palette("deep-sea", ("#04101f", "#0d2137"), "#8fb8d6",
            ["#2ec4b6", "#ff9f1c", "#e0f7fa", "#5390d9"]),
    Palette("ember", ("#140a08", "#2e1511"), "#d7a99a",
            ["#ff6b35", "#ffd166", "#f7c59f", "#c1121f"]),
    Palette("violet", ("#0a0516", "#1f1038"), "#a898d6",
            ["#b388ff", "#ffd166", "#7c4dff", "#f7a4c0"]),
    Palette("forest", ("#03120c", "#0b2a1b"), "#8fc7aa",
            ["#4ade80", "#fbbf24", "#a7f3d0", "#2dd4bf"]),
    Palette("slate", ("#080c12", "#1b2430"), "#93a7bd",
            ["#60a5fa", "#f472b6", "#e2e8f0", "#fbbf24"]),
    Palette("rust", ("#120b06", "#2b1a0d"), "#c9a37a",
            ["#e07a5f", "#81b29a", "#f2cc8f", "#3d5a80"]),
]

LIGHT_PALETTES = [
    Palette("paper", ("#fdf6e3", "#f0e6cc"), "#6b6455",
            ["#bc4749", "#386641", "#f2a541", "#2a4494"]),
    Palette("sky", ("#eef4fa", "#cfe0f0"), "#5b7185",
            ["#1d4e89", "#e8871e", "#3fa7d6", "#ef476f"]),
    Palette("bloom", ("#fff4f2", "#ffe0e2"), "#8a6b6d",
            ["#9f1239", "#4c1d95", "#f59e0b", "#0f766e"]),
]

# DU4: black, dark grey, light grey, white — and nothing between them. Any
# gradient or alpha would be dithered into noise, so `flat` turns both off.
MONO_PALETTE = Palette("mono", ("#ffffff", "#ffffff"), "#555555",
                       ["#000000", "#555555", "#aaaaaa", "#000000"],
                       flat=True, glow=False)


def mono_mode() -> bool:
    return (os.environ.get("MEDIA_VISUAL_MONO") or "").strip().lower() \
        in ("1", "on", "yes", "true")


def _palette(rnd: random.Random) -> Palette:
    if mono_mode():
        return MONO_PALETTE
    pool = DARK_PALETTES if dark_mode() else LIGHT_PALETTES
    return rnd.choice(pool)


# --- text → motif -------------------------------------------------------------

_FENCE = re.compile(r"```.*?```", re.S)
_WORD = re.compile(r"[a-z][a-z'\-]{2,}")

STOPWORDS = frozenset("""
the and for that with this you your are was were have has had not but can
its it's from they them their there here what when where which who whom how
why all any both each few more most other some such than too very just now
will would should could into over under again then once about against
between during before after above below out off down only own same our
we're i'm don't doesn't isn't aren't one two three like get got make made
use used using need needs want wants say says said way ways thing things
""".split())

# Keyword → motif. Stems, matched as substrings of each word, so "connects",
# "connection" and "connected" all land on `network` from one entry.
MOTIF_KEYWORDS: Dict[str, Tuple[str, ...]] = {
    "network": ("network", "graph", "connect", "link", "relat", "mesh",
                "peer", "route", "messag", "mail", "protocol", "server",
                "host", "node", "cluster", "bridge", "relay", "socket"),
    "flow": ("pipeline", "flow", "stream", "through", "step", "chain",
             "sequen", "migrat", "transfer", "sync", "pass", "queue",
             "channel", "pipe", "hand"),
    "stack": ("layer", "stack", "build", "structur", "packag", "modul",
              "version", "deploy", "level", "architect", "compose", "block",
              "tier", "frame"),
    "growth": ("grow", "branch", "tree", "fork", "split", "expand", "root",
               "seed", "evolv", "deriv", "spread", "sprout", "leaf"),
    "waves": ("sound", "audio", "voice", "speech", "speak", "music", "wave",
              "signal", "frequen", "listen", "hear", "song", "tone", "echo",
              "spoken", "said"),
    "orbit": ("cycle", "loop", "repeat", "schedul", "clock", "session",
              "turn", "return", "orbit", "rotat", "period", "daily",
              "interval", "round", "circl"),
    "strata": ("histor", "memor", "record", "log", "store", "archive",
               "past", "note", "land", "ground", "deposit", "sediment",
               "remember", "persist", "database"),
    "burst": ("error", "fail", "crash", "alert", "spike", "burn", "cost",
              "launch", "fire", "break", "urgent", "warn", "explod",
              "sudden", "wrong"),
    "grid": ("table", "list", "file", "data", "index", "cell", "count",
             "config", "option", "matrix", "field", "column", "row",
             "spreadsheet", "catalog"),
    "spiral": ("search", "find", "deep", "recurs", "detail", "focus",
               "explor", "dive", "question", "inward", "centre", "center",
               "narrow", "trace"),
}

MOTIF_NAMES = tuple(MOTIF_KEYWORDS)


def keywords(text: str, limit: int = 4) -> List[str]:
    """The reply's most distinctive words, longest-first among the frequent
    ones. Used for figure labels — no model, just frequency and stopwords."""
    clean = _FENCE.sub(" ", text.lower())
    counts: Dict[str, int] = {}
    for w in _WORD.findall(clean):
        if w in STOPWORDS:
            continue
        counts[w] = counts.get(w, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], -len(kv[0]), kv[0]))
    return [w for w, _ in ranked[:limit]]


def choose_motif(text: str, rnd: random.Random) -> str:
    """The best-scoring motif family for this text; a seeded pick when the
    text says nothing in particular (which is most short replies)."""
    clean = _FENCE.sub(" ", text.lower())
    words = _WORD.findall(clean)
    scores = {name: 0 for name in MOTIF_NAMES}
    for w in words:
        for name, stems in MOTIF_KEYWORDS.items():
            for stem in stems:
                if stem in w:
                    scores[name] += 1
                    break
    best = max(scores.values())
    if best == 0:
        return rnd.choice(MOTIF_NAMES)
    # Ties are broken by the seed, not by dict order, so two texts with the
    # same score profile still get different pictures.
    top = sorted(n for n, s in scores.items() if s == best)
    return rnd.choice(top)


# --- seeding ------------------------------------------------------------------

def _seed_int(*parts: str) -> int:
    h = hashlib.blake2b("\x1f".join(parts).encode("utf-8"), digest_size=8)
    return int.from_bytes(h.digest(), "big")


def _beat() -> Tuple[int, int]:
    """(index, count) from MEDIA_VISUAL_BEAT ("i/n", 1-based); (1, 1) when
    this is a single image."""
    raw = (os.environ.get("MEDIA_VISUAL_BEAT") or "").strip()
    m = re.match(r"^(\d+)\s*/\s*(\d+)$", raw)
    if not m:
        return 1, 1
    i, n = int(m.group(1)), int(m.group(2))
    n = max(1, n)
    return max(1, min(i, n)), n


# --- svg helpers --------------------------------------------------------------

def _op(pal: Palette, value: float) -> str:
    """Opacity, snapped to 1 on flat (e-ink) palettes."""
    return "1" if pal.flat else f"{value:.2f}"


def _animate(attr: str, values: str, dur: float, *, begin: float = 0.0) -> str:
    return (f'<animate attributeName="{attr}" values="{values}" '
            f'dur="{dur:.1f}s" begin="{begin:.1f}s" repeatCount="indefinite"/>')


def _rotate(cx: float, cy: float, dur: float, *, reverse: bool = False) -> str:
    a, b = (360, 0) if reverse else (0, 360)
    return (f'<animateTransform attributeName="transform" type="rotate" '
            f'from="{a} {cx:.0f} {cy:.0f}" to="{b} {cx:.0f} {cy:.0f}" '
            f'dur="{dur:.1f}s" repeatCount="indefinite"/>')


def _drift(dx: float, dy: float, dur: float, *, begin: float = 0.0) -> str:
    """A slow there-and-back translate — the whole package's idea of motion:
    present, never distracting."""
    return (f'<animateTransform attributeName="transform" type="translate" '
            f'values="0 0; {dx:.0f} {dy:.0f}; 0 0" dur="{dur:.1f}s" '
            f'begin="{begin:.1f}s" repeatCount="indefinite" additive="sum"/>')


def _wave_path(y: float, amp: float, periods: float, phase: float,
               *, close_to: float = H + 40) -> str:
    """A sine ridge across a bit more than the full width (so a drifting band
    never shows its end), closed into a fillable band."""
    pts = []
    span = W + 400
    for i in range(0, 49):
        x = -200 + span * i / 48
        yy = y + amp * math.sin(phase + periods * 2 * math.pi * i / 48)
        pts.append(f"{x:.0f},{yy:.1f}")
    return (f'M{pts[0]} L' + " L".join(pts[1:]) +
            f' L{W + 200},{close_to:.0f} L-200,{close_to:.0f} Z')


# --- motifs -------------------------------------------------------------------
# Each takes (rnd, pal, prog) and returns SVG fragments. `prog` runs 0→1
# across a beat sequence (1.0 for a single image): the subject is fuller,
# brighter or further along at the end than at the start.

def _motif_orbit(rnd: random.Random, pal: Palette, prog: float) -> List[str]:
    cx, cy = 800, 470
    out = [f'<circle cx="{cx}" cy="{cy}" r="{68 + 14 * prog:.0f}" '
           f'fill="{pal.accent(0)}" opacity="{_op(pal, 0.95)}">'
           f'{_animate("r", f"{62 + 12 * prog:.0f};{78 + 14 * prog:.0f};{62 + 12 * prog:.0f}", rnd.uniform(6, 10))}'
           f'</circle>']
    rings = 3 + int(round(prog * 2))
    for k in range(rings):
        rx = 150 + k * 105 + rnd.uniform(-14, 14)
        ry = rx * rnd.uniform(0.34, 0.52)
        tilt = rnd.uniform(-22, 22)
        out.append(f'<g transform="rotate({tilt:.0f} {cx} {cy})">'
                   f'<ellipse cx="{cx}" cy="{cy}" rx="{rx:.0f}" ry="{ry:.0f}" '
                   f'fill="none" stroke="{pal.ink}" stroke-width="2" '
                   f'opacity="{_op(pal, 0.4)}"/>'
                   f'<g>{_rotate(cx, cy, rnd.uniform(18, 46), reverse=bool(k % 2))}'
                   f'<circle cx="{cx + rx:.0f}" cy="{cy}" r="{10 + rnd.uniform(0, 12):.0f}" '
                   f'fill="{pal.accent(k + 1)}"/></g></g>')
    return out


def _motif_network(rnd: random.Random, pal: Palette, prog: float) -> List[str]:
    n = 8 + rnd.randrange(5)
    nodes = []
    cols = 4
    for i in range(n):
        col, row = i % cols, i // cols
        nodes.append((260 + col * 360 + rnd.uniform(-70, 70),
                      230 + row * 210 + rnd.uniform(-60, 60)))
    out = []
    for i, (x1, y1) in enumerate(nodes):
        for j, (x2, y2) in enumerate(nodes[i + 1:], i + 1):
            if math.hypot(x2 - x1, y2 - y1) < 400:
                out.append(f'<line x1="{x1:.0f}" y1="{y1:.0f}" x2="{x2:.0f}" '
                           f'y2="{y2:.0f}" stroke="{pal.ink}" stroke-width="2" '
                           f'opacity="{_op(pal, 0.35)}"/>')
    lit = max(1, int(round(prog * n)))
    order = list(range(n))
    rnd.shuffle(order)
    for rank, idx in enumerate(order):
        x, y = nodes[idx]
        on = rank < lit
        r = 26 if on else 15
        fill = pal.accent(0) if on else pal.ink
        out.append(f'<circle cx="{x:.0f}" cy="{y:.0f}" r="{r}" fill="{fill}" '
                   f'opacity="{_op(pal, 0.95 if on else 0.55)}">'
                   f'{_animate("r", f"{r};{r + 6};{r}", rnd.uniform(5, 11), begin=rnd.uniform(0, 4))}'
                   f'</circle>')
    return out


def _motif_waves(rnd: random.Random, pal: Palette, prog: float) -> List[str]:
    out = []
    layers = 4 + rnd.randrange(3)
    for k in range(layers):
        y = 360 + k * (460 / layers)
        amp = (26 + k * 11) * (0.55 + 0.6 * prog)
        periods = rnd.uniform(1.4, 3.2)
        col = pal.accent(k) if k % 2 else pal.ink
        out.append(f'<g opacity="{_op(pal, 0.28 + 0.12 * k)}">'
                   f'{_drift(rnd.choice([-110, 110]), 0, rnd.uniform(14, 30), begin=k * 0.7)}'
                   f'<path d="{_wave_path(y, amp, periods, rnd.uniform(0, 6.28))}" '
                   f'fill="{col}"/></g>')
    # A single bright crest rides on top: the subject, not the texture.
    out.append(f'<path d="{_wave_path(330, 40 * (0.6 + prog), 2.1, 1.1, close_to=340)}" '
               f'fill="none" stroke="{pal.accent(0)}" stroke-width="5" '
               f'opacity="{_op(pal, 0.9)}"/>')
    return out


def _motif_strata(rnd: random.Random, pal: Palette, prog: float) -> List[str]:
    disc_y = 420 - 150 * prog
    out = [f'<circle cx="{rnd.uniform(520, 1080):.0f}" cy="{disc_y:.0f}" '
           f'r="{110 + rnd.uniform(0, 40):.0f}" fill="{pal.accent(0)}" '
           f'opacity="{_op(pal, 0.9)}">'
           f'{_animate("opacity", f"{_op(pal, 0.75)};{_op(pal, 1)};{_op(pal, 0.75)}", rnd.uniform(8, 14))}'
           f'</circle>']
    bands = 4 + rnd.randrange(3)
    for k in range(bands):
        y = 470 + k * (430 / bands)
        col = pal.accent(k + 1) if k % 2 else pal.ink
        out.append(f'<g opacity="{_op(pal, 0.35 + 0.1 * k)}">'
                   f'{_drift(rnd.choice([-40, 40]), 0, rnd.uniform(20, 40), begin=k * 1.1)}'
                   f'<path d="{_wave_path(y, 18 + k * 6, rnd.uniform(0.8, 1.8), rnd.uniform(0, 6.28))}" '
                   f'fill="{col}"/></g>')
    return out


def _motif_stack(rnd: random.Random, pal: Palette, prog: float) -> List[str]:
    total = 5 + rnd.randrange(3)
    # One new block per beat: round(prog*total) repeats a frame at 4 beats.
    shown = max(1, int(round(1 + prog * (total - 1))))
    out = []
    bw, bh = 620, 84
    for k in range(total):
        y = 700 - k * (bh + 22)
        x = 490 + rnd.uniform(-60, 60)
        on = k < shown
        col = pal.accent(0) if k == shown - 1 else (pal.accent(k) if on else pal.ink)
        out.append(f'<g opacity="{_op(pal, 0.95 if on else 0.25)}">'
                   f'{_drift(0, rnd.choice([-8, 8]), rnd.uniform(6, 12), begin=k * 0.6)}'
                   f'<rect x="{x:.0f}" y="{y:.0f}" width="{bw}" height="{bh}" rx="14" '
                   f'fill="{col}"/></g>')
    return out


def _motif_growth(rnd: random.Random, pal: Palette, prog: float) -> List[str]:
    out: List[str] = []
    max_depth = 3 + int(round(prog * 2))

    def branch(x: float, y: float, angle: float, length: float, depth: int) -> None:
        if depth > max_depth or length < 18:
            return
        x2 = x + length * math.cos(angle)
        y2 = y + length * math.sin(angle)
        col = pal.accent(0) if depth >= max_depth - 1 else pal.ink
        out.append(f'<line x1="{x:.0f}" y1="{y:.0f}" x2="{x2:.0f}" y2="{y2:.0f}" '
                   f'stroke="{col}" stroke-width="{max(3, 20 - depth * 4)}" '
                   f'stroke-linecap="round" opacity="{_op(pal, 0.45 + 0.12 * depth)}"/>')
        if depth >= max_depth:
            out.append(f'<circle cx="{x2:.0f}" cy="{y2:.0f}" r="{13 + rnd.uniform(0, 10):.0f}" '
                       f'fill="{pal.accent(depth)}"/>')
            return
        spread = rnd.uniform(0.34, 0.62)
        for turn in (-spread, spread):
            branch(x2, y2, angle + turn, length * rnd.uniform(0.62, 0.8), depth + 1)

    out.append(f'<g>{_drift(14, 0, rnd.uniform(12, 20))}')
    branch(800, 910, -math.pi / 2, 265, 1)
    out.append("</g>")
    return out


def _motif_flow(rnd: random.Random, pal: Palette, prog: float) -> List[str]:
    out = []
    ribbons = 3 + rnd.randrange(2)
    for k in range(ribbons):
        y = 200 + k * (520 / max(1, ribbons - 1))  # spread across the frame
        d = (f'M-100,{y:.0f} C{W * 0.3:.0f},{y + rnd.uniform(-160, 160):.0f} '
             f'{W * 0.7:.0f},{y + rnd.uniform(-160, 160):.0f} {W + 100},{y:.0f}')
        # The ribbon itself has to carry the picture: a hairline guide with a
        # couple of travellers reads as an empty frame in a still, and the
        # canvas is looked at in stills as often as in motion.
        out.append(f'<path d="{d}" fill="none" stroke="{pal.accent(k)}" '
                   f'stroke-width="{26 + 10 * prog:.0f}" stroke-linecap="round" '
                   f'opacity="{_op(pal, 0.3 + 0.08 * k)}"/>')
        out.append(f'<path d="{d}" fill="none" stroke="{pal.accent(k)}" '
                   f'stroke-width="6" stroke-linecap="round" '
                   f'stroke-dasharray="{28 + k * 9} {46 + k * 7}" '
                   f'opacity="{_op(pal, 0.85)}">'
                   f'<animate attributeName="stroke-dashoffset" '
                   f'values="0;{-(74 + k * 16) * 6}" dur="{rnd.uniform(9, 16):.1f}s" '
                   f'repeatCount="indefinite"/></path>')
        for t in range(1 + int(round(prog * 3))):
            out.append(
                f'<circle r="{16 + rnd.uniform(0, 10):.0f}" fill="{pal.accent(k + 2)}">'
                f'<animateMotion path="{d}" dur="{rnd.uniform(9, 16):.1f}s" '
                f'begin="{t * 2.3 + k * 0.7:.1f}s" repeatCount="indefinite"/>'
                f'</circle>')
    return out


def _motif_burst(rnd: random.Random, pal: Palette, prog: float) -> List[str]:
    cx, cy = 800, 450
    rays = 14 + rnd.randrange(12)
    out = [f'<g>{_rotate(cx, cy, rnd.uniform(40, 90))}']
    for k in range(rays):
        a = 2 * math.pi * k / rays
        r0, r1 = 120, 300 + rnd.uniform(0, 260) * (0.5 + 0.6 * prog)
        out.append(f'<line x1="{cx + r0 * math.cos(a):.0f}" y1="{cy + r0 * math.sin(a):.0f}" '
                   f'x2="{cx + r1 * math.cos(a):.0f}" y2="{cy + r1 * math.sin(a):.0f}" '
                   f'stroke="{pal.accent(k)}" stroke-width="{3 + rnd.uniform(0, 5):.0f}" '
                   f'opacity="{_op(pal, 0.5)}"/>')
    out.append("</g>")
    core = 80 + 30 * prog
    out.append(f'<circle cx="{cx}" cy="{cy}" r="{core:.0f}" fill="{pal.accent(0)}">'
               f'{_animate("r", f"{core:.0f};{core + 26:.0f};{core:.0f}", rnd.uniform(3.5, 6))}'
               f'</circle>')
    return out


def _motif_grid(rnd: random.Random, pal: Palette, prog: float) -> List[str]:
    cols, rows = 7, 4
    cw, ch = 170, 150
    x0 = (W - cols * cw) / 2
    y0 = (H - rows * ch) / 2 + 20
    cells = [(c, r) for r in range(rows) for c in range(cols)]
    rnd.shuffle(cells)
    lit = max(1, int(round(prog * len(cells) * 0.35)))
    out = []
    for i, (c, r) in enumerate(cells):
        x, y = x0 + c * cw, y0 + r * ch
        on = i < lit
        col = pal.accent(i) if on else pal.ink
        out.append(f'<rect x="{x + 8:.0f}" y="{y + 8:.0f}" width="{cw - 16}" '
                   f'height="{ch - 16}" rx="10" fill="{col}" '
                   f'opacity="{_op(pal, 0.9 if on else 0.16)}">'
                   f'{_animate("opacity", f"{_op(pal, 0.7)};{_op(pal, 1)};{_op(pal, 0.7)}", rnd.uniform(6, 12), begin=rnd.uniform(0, 5)) if on else ""}'
                   f'</rect>')
    return out


def _motif_spiral(rnd: random.Random, pal: Palette, prog: float) -> List[str]:
    cx, cy = 800, 460
    turns = rnd.uniform(2.6, 4.2)
    dots = 60 + rnd.randrange(40)
    out = [f'<g>{_rotate(cx, cy, rnd.uniform(60, 140), reverse=rnd.random() < 0.5)}']
    for k in range(dots):
        t = k / dots
        a = t * turns * 2 * math.pi
        r = 40 + (380 * (0.55 + 0.5 * prog)) * t
        out.append(f'<circle cx="{cx + r * math.cos(a):.0f}" '
                   f'cy="{cy + r * math.sin(a) * 0.82:.0f}" '
                   f'r="{4 + 14 * (1 - t):.0f}" fill="{pal.accent(k // 9)}" '
                   f'opacity="{_op(pal, 0.35 + 0.55 * (1 - t))}"/>')
    out.append("</g>")
    return out


MOTIFS: Dict[str, Callable[[random.Random, Palette, float], List[str]]] = {
    "orbit": _motif_orbit,
    "network": _motif_network,
    "waves": _motif_waves,
    "strata": _motif_strata,
    "stack": _motif_stack,
    "growth": _motif_growth,
    "flow": _motif_flow,
    "burst": _motif_burst,
    "grid": _motif_grid,
    "spiral": _motif_spiral,
}


# --- composition --------------------------------------------------------------

def _backdrop(pal: Palette) -> List[str]:
    if pal.flat:
        return [f'<rect width="{W}" height="{H}" fill="{pal.bg[0]}"/>']
    return [
        '<defs><linearGradient id="bg" x1="0" y1="0" x2="0.3" y2="1">'
        f'<stop offset="0%" stop-color="{pal.bg[0]}"/>'
        f'<stop offset="100%" stop-color="{pal.bg[1]}"/></linearGradient>'
        '<radialGradient id="vig" cx="0.5" cy="0.45" r="0.75">'
        '<stop offset="55%" stop-color="#000000" stop-opacity="0"/>'
        '<stop offset="100%" stop-color="#000000" stop-opacity="0.45"/>'
        '</radialGradient></defs>',
        f'<rect width="{W}" height="{H}" fill="url(#bg)"/>',
    ]


def _grain(rnd: random.Random, pal: Palette) -> List[str]:
    """A scatter of faint dots. Cheap, and it stops large flat fills from
    looking like a rendering failure on a big screen."""
    if pal.flat:
        return []
    out = []
    for _ in range(90):
        out.append(f'<circle cx="{rnd.uniform(0, W):.0f}" cy="{rnd.uniform(0, H):.0f}" '
                   f'r="{rnd.uniform(1, 2.6):.1f}" fill="{pal.ink}" '
                   f'opacity="{rnd.uniform(0.06, 0.2):.2f}"/>')
    return out


def _labels(words: List[str], pal: Palette) -> List[str]:
    """Figure mode: the reply's own key words, set large along the bottom.
    No model can be consulted for a real diagram here, so the honest thing is
    to name the subject rather than mime it."""
    out = []
    y = H - 60 - 58 * (len(words) - 1)
    for i, w in enumerate(words):
        out.append(f'<text x="80" y="{y + i * 58:.0f}" font-family="Inter, '
                   f'Helvetica, Arial, sans-serif" font-size="44" '
                   f'font-weight="600" fill="{pal.accent(0) if i == 0 else pal.ink}" '
                   f'opacity="{_op(pal, 1 if i == 0 else 0.75)}">{_esc(w)}</text>')
    return out


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def compose(text: str, *, scene: str = "", subject: str = "",
            beat: Tuple[int, int] = (1, 1), figure: bool = False) -> str:
    """The whole pipeline, as one deterministic function — the engine below
    is just this plus environment reading, which keeps it testable.

    Three inputs, three jobs: `scene` (stable across a session) fixes the
    palette, `subject` (the whole reply) fixes the motif, `text` (this beat)
    varies the particulars. Any of them may be empty, in which case `text`
    stands in."""
    i, n = beat
    ident_rnd = random.Random(_seed_int("identity", scene or subject or text))
    pal = _palette(ident_rnd)
    motif = choose_motif(subject or text, ident_rnd)

    # Structure is seeded WITHOUT the beat: every frame of a sequence places
    # the same elements in the same spots, and `prog` alone carries the
    # development. Seed the beat in here and the scene twitches between
    # frames instead of advancing.
    detail_rnd = random.Random(_seed_int("detail", scene, subject or text))
    prog = 1.0 if n <= 1 else i / n

    parts = _backdrop(pal)
    parts += _grain(detail_rnd, pal)
    parts += MOTIFS[motif](detail_rnd, pal, prog)
    if figure:
        parts += _labels(keywords(text, 3), pal)

    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
            f'width="{W}" height="{H}">' + "".join(parts) + "</svg>")


def generate(prompt: str) -> Tuple[bytes | None, str]:
    """The engine contract: one artwork for `prompt`, never a failure that
    matters. Nothing here can time out, rate-limit or bill."""
    text = (prompt or "").strip()
    if not text:
        return None, "pattern: empty prompt"
    svg = compose(
        text,
        scene=(os.environ.get("MEDIA_VISUAL_PATTERN_SEED") or "").strip(),
        subject=(os.environ.get("MEDIA_VISUAL_PATTERN_SUBJECT") or "").strip(),
        beat=_beat(),
        figure=os.environ.get("MEDIA_VISUAL_FIGURE") == "1",
    )
    return svg.encode("utf-8"), ""
