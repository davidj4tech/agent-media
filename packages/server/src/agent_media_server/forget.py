"""Delete a thread: one session, gone from everywhere this host keeps it.

`media session-delete <session>…` (dry run unless `--apply`). In order:

1. The session is recorded as deleted (`agent_media_core.deleted`), first, so
   nothing rebuilds what the next steps take away: the shelf is re-exported
   from speech history every minute, and the thread list and the search index
   read the agent's own store.
2. Its speech-history rows (`extras.source_session` = the session): the lines
   spoken in it and the listener's own turns.
3. Its shelf folder and its book-tracks manifest. A folder is taken only when
   every session whose manifest names it is being deleted too (two
   conversations that asked the same question share one).
4. Its item on each Audiobookshelf the host publishes to
   (`book_tracks._abs_ready_all`), found by folder the way export finds it.
5. Its search rows (`search.forget`), and its flags: archived, pinned,
   rested, moved, draft.

Nothing is thrown away blind. Before any of it, the history rows and the
manifest are written, and the folder is moved, under
`~/backups/agent-media-deleted/<stamp>/<session>/`; restoring is putting the
folder back and re-inserting the rows. The agent's own transcript (a Claude
jsonl, a Codex rollout) is left alone: it is the agent's, and the deleted
mark is what keeps it off the list.

`abs_items` names library items to delete as well, by id: ones whose folder
was already moved away, which ABS shows as missing and nothing here can find
by path any more.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path

from agent_media_core import book_tracks, deleted, harnesses
from agent_media_core._paths import state_dir
from agent_media_core.state.store import default_db_path


def backup_root() -> Path:
    return Path.home() / "backups" / "agent-media-deleted"


def _history(conn: sqlite3.Connection, session: str) -> list[dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM history WHERE json_extract(extras, '$.source_session') = ?",
        (session,)).fetchall()
    return [dict(r) for r in rows]


def _manifest(session: str) -> tuple[Path, dict]:
    path = book_tracks._manifest_path(session)
    try:
        return path, json.loads(path.read_text())
    except (OSError, ValueError):
        return path, {}


def _folder_users(folder: str) -> set[str]:
    """Every session whose manifest names `folder`."""
    out = set()
    for p in (state_dir() / "book-tracks").glob("*.json"):
        try:
            if json.loads(p.read_text()).get("folder") == folder:
                out.add(p.stem)
        except (OSError, ValueError):
            continue
    return out


def _abs_delete(url: str, token: str, item_id: str) -> int:
    """DELETE the library item (not its files: those were moved to the
    backup already). The HTTP status, 0 when ABS did not answer."""
    req = urllib.request.Request(f"{url}/api/items/{item_id}", method="DELETE",
                                 headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, OSError):
        return 0


def plan(sessions: list[str], abs_items: tuple[str, ...] = ()) -> dict:
    """What deleting `sessions` would take, without touching anything."""
    bad = [s for s in sessions if not harnesses.SESSION_ID.fullmatch(s)]
    if bad:
        raise ValueError(f"not a session id: {', '.join(bad)}")
    conn = sqlite3.connect(default_db_path())
    try:
        per = {}
        for s in sessions:
            path, man = _manifest(s)
            folder = str(man.get("folder") or "")
            users = _folder_users(folder) if folder else set()
            per[s] = {
                "history": len(_history(conn, s)),
                "manifest": str(path) if path.exists() else "",
                "folder": folder if folder and Path(folder).exists() else "",
                # Shared with a thread that is staying: the folder stays.
                "folder_kept_for": sorted(users - set(sessions)),
            }
    finally:
        conn.close()
    items = []
    folders = {v["folder"] for v in per.values() if v["folder"] and not v["folder_kept_for"]}
    for url, token, libs in book_tracks._abs_ready_all():
        for folder in sorted(folders):
            item = book_tracks._find_item(url, token, libs, Path(folder))
            if item and item.get("id"):
                items.append({"url": url, "id": item["id"], "path": item.get("path", "")})
        for item_id in abs_items:
            items.append({"url": url, "id": item_id, "path": "(named)"})
    return {"sessions": per, "abs_items": items}


def apply(sessions: list[str], abs_items: tuple[str, ...] = ()) -> dict:
    """Delete `sessions` (see the module docstring). The plan, with outcomes."""
    from . import archive, drafts, moves, pins, rest, search

    report = plan(sessions, abs_items)
    deleted.mark(sessions)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    conn = sqlite3.connect(default_db_path(), timeout=10.0)
    tokens = {url: token for url, token, _libs in book_tracks._abs_ready_all()}
    try:
        for s, got in report["sessions"].items():
            keep = backup_root() / stamp / s
            keep.mkdir(parents=True, exist_ok=True)
            rows = _history(conn, s)
            (keep / "history.json").write_text(json.dumps(rows, indent=1))
            path, man = _manifest(s)
            if path.exists():
                shutil.copy2(path, keep / "manifest.json")
            if got["folder"] and not got["folder_kept_for"] and Path(got["folder"]).exists():
                shutil.move(got["folder"], keep / "folder")
                got["folder_moved_to"] = str(keep / "folder")
            with conn:
                conn.execute(
                    "DELETE FROM history WHERE json_extract(extras, '$.source_session') = ?", (s,))
            path.unlink(missing_ok=True)
            got["search_rows"] = search.forget(s)
            archive.set_archived(s, False)
            pins.set_pinned(s, False)
            rest.clear_rested(s)
            moves.forget(s)
            drafts._draft_path(s).unlink(missing_ok=True)
            got["backup"] = str(keep)
    finally:
        conn.close()
    for item in report["abs_items"]:
        token = tokens.get(item["url"], "")
        item["status"] = _abs_delete(item["url"], token, item["id"]) if token else 0
    return report


def run(sessions: list[str], *, apply_: bool = False, abs_items: tuple[str, ...] = (),
        as_json: bool = False) -> int:
    try:
        report = apply(sessions, abs_items) if apply_ else plan(sessions, abs_items)
    except ValueError as e:
        print(f"media session-delete: {e}")
        return 2
    if as_json:
        print(json.dumps(report, indent=1))
        return 0
    verb = "deleted" if apply_ else "would delete"
    for s, got in report["sessions"].items():
        where = got["folder"] or "no folder"
        if got["folder_kept_for"]:
            where += f" (kept: also {', '.join(x[:8] for x in got['folder_kept_for'])})"
        print(f"{s[:8]}: {verb} {got['history']} history rows, "
              f"{'the manifest, ' if got['manifest'] else ''}{where}"
              + (f", {got['search_rows']} search rows" if "search_rows" in got else ""))
    for item in report["abs_items"]:
        status = f" -> {item['status']}" if "status" in item else ""
        print(f"abs {item['id']} {item['path']}{status}")
    if apply_ and report["sessions"]:
        print(f"backup: {backup_root()}")
    elif not apply_:
        print("dry run: nothing changed (--apply to delete)")
    return 0
