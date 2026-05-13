from __future__ import annotations


def queue(track: dict) -> None:
    print(f"{track.get('artist', '?')} — {track.get('title', '?')}")
    if 'url' in track:
        print(f"  {track['url']}")
    if 'why' in track:
        print(f"  why: {track['why']}")
