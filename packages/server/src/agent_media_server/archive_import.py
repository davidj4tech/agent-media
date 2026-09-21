"""One-off: carry Audiobookshelf's `archived` tags over to the archive flag.

Archiving used to be a tag on the conversation's ABS item (`book_tracks`
still sets it after a week of quiet, and the app used to set it by hand). The
thread list now reads the flag this server keeps (`archive.py`), so threads
archived the old way would all come back unarchived. This reads every item
tagged `archived` in the conversation libraries, maps each to its session the
way the reply code does — the item folder's `<project>/<title>` tail against
the book-tracks manifests — and sets the flag for it.

`media session-archive-import` prints what it would mark; `--apply` marks.
**Read-only toward ABS**: two GETs per server (libraries, then each library's
items), never a write. Every configured server is read (`_abs_ready_all`), and
a session tagged on more than one is marked once.

A library counts as a conversation library when at least one of its items maps
to a session; an `archived` tag on an audiobook elsewhere is not reported.
"""

from __future__ import annotations

import json
import sys

from . import archive, sessions


def _manifest_tails() -> dict[str, str]:
    """`{"<project>/<title>": session}` from the book-tracks shelf."""
    out: dict[str, str] = {}
    for f in sorted(sessions._manifest_dir().glob("*.json")):
        try:
            data = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        tail = sessions._tail(data.get("folder") or "")
        if tail:
            out[tail] = str(data.get("session") or f.stem)
    return out


def plan(servers: list | None = None) -> tuple[list[dict], list[str]]:
    """`(rows, problems)`. One row per archived item: `{"session", "title",
    "tail", "item", "library", "server", "state"}`, where state is `mark`
    (would be / was archived), `already` (flag set) or `unmapped` (no session
    behind the folder). `problems` names servers or libraries that did not
    answer."""
    from agent_media_core import book_tracks

    if servers is None:
        servers = book_tracks._abs_ready_all()
    tails = _manifest_tails()
    flags = archive.archived()
    rows: list[dict] = []
    problems: list[str] = []
    seen: set[str] = set()
    for url, token, libs in servers:
        for lib in libs:
            try:
                items = book_tracks._abs_items(url, token, lib["id"])
            except Exception as e:  # noqa: BLE001
                problems.append(f"{url} library {lib.get('name') or lib.get('id')}: {e}")
                continue
            mapped = [(i, tails.get(sessions._tail(i.get("path") or ""))) for i in items]
            if not any(sid for _i, sid in mapped):
                continue          # not a conversation library
            for item, sid in mapped:
                media = item.get("media") or {}
                if book_tracks.ARCHIVED_TAG not in (media.get("tags") or []):
                    continue
                tail = sessions._tail(item.get("path") or "")
                title = str((media.get("metadata") or {}).get("title") or "") \
                    or tail.rsplit("/", 1)[-1]
                row = {"session": sid or None, "title": title, "tail": tail,
                       "item": item.get("id"), "library": lib.get("name") or lib.get("id"),
                       "server": url}
                once = sid or f"tail:{tail}"
                if once in seen:
                    continue
                seen.add(once)
                if not sid or not sessions._SESSION.fullmatch(sid):
                    row["state"] = "unmapped"
                else:
                    row["state"] = "already" if sid in flags else "mark"
                rows.append(row)
    return rows, problems


def run(apply: bool = False, *, as_json: bool = False, servers: list | None = None,
        out=None) -> int:
    out = out or sys.stdout
    if servers is None:
        from agent_media_core import book_tracks

        servers = book_tracks._abs_ready_all()
        if not servers:
            print("no Audiobookshelf answered (or none is configured on this host)",
                  file=sys.stderr)
            return 1
    rows, problems = plan(servers)
    if apply:
        for r in rows:
            if r["state"] == "mark":
                archive.set_archived(r["session"], True)
                r["state"] = "marked"
    if as_json:
        print(json.dumps({"apply": apply, "rows": rows, "problems": problems}, indent=1),
              file=out)
    else:
        verb = {"mark": "would mark", "marked": "marked", "already": "already archived",
                "unmapped": "no session"}
        for r in rows:
            sid = (r["session"] or "-")[:8]
            print(f"{verb[r['state']]:<16} {sid:<8} {r['title']}  ({r['tail']})", file=out)
        n = sum(1 for r in rows if r["state"] in ("mark", "marked"))
        print(f"{len(rows)} archived item(s); {n} session(s) "
              f"{'marked' if apply else 'to mark'}"
              f"{'' if apply or not n else ' — run with --apply'}", file=out)
        for p in problems:
            print(f"problem: {p}", file=out)
    return 1 if problems else 0
