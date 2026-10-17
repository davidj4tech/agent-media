"""Seal a thread: everything this host keeps of it, age-encrypted into one
bundle, and the plaintext taken away. Unsealing puts it all back.

    POST /session/seal {"session", "sealed": true|false}
    media session-seal <session> [--unseal]

`claude-session-seal` sealed the transcript alone, by hand, after the session
had exited. This is that for the whole trail, from the app (David, 15 Oct
2026). What goes into the bundle, `<state_dir>/sealed/<session>.tar.age`:

1. **The agent's transcript.** Claude Code's `<session>.jsonl` and the
   `<session>/` folder beside it (subagents, tool results); a Codex rollout;
   a pi session file; a Gemini CLI conversation file, with what Gemini
   itself deletes along with one (its subagents' `chats/<session>/`, and
   `tool-outputs/session-<session>/`, `logs/session-<session>.jsonl` and
   `<session>/` in its project folder). And the copies agent-sessions
   stages for fleet sync
   (`~/.cache/claude-incoming`, `~/.cache/agent-sessions-incoming`), which
   are the whole transcript again. Hermes and opencode keep conversations in
   a database shared with every other one, so they cannot be sealed: 409.
2. **Its spoken lines**: the speech-history rows (`extras.source_session`),
   which hold the text, and the clip files they name with every file beside
   each clip (`<stem>.*`: the text, the rendered audio).
3. **Its shelf folder and manifest**, and its Audiobookshelf item is deleted
   (the folder is gone). A folder another thread shares stays, as in
   `forget`.
4. **Its search rows** are dropped; unsealing brings the transcript back and
   the index reads it again.

The session is recorded as sealed (`agent_media_core.sealed`) first, so the
shelf export, the search index and the thread list leave it alone, and it is
listed as a row with no title (`sessions.seal_row`). A live session is ended
first: closed, and waited for until its process has gone and its hooks have
settled. The bundle is decrypted and listed before anything is removed, so a
bad key or a full disk leaves the thread as it was.

Not covered: the speech-events log and the canvas's figure history, which
roll over on their own; journald; whatever the phone has cached.

The recipient is `MEDIA_SEAL_RECIPIENT` or `SOPS_AGE_RECIPIENT`, else the
public half of the key file, `MEDIA_SEAL_KEY_FILE` or `SOPS_AGE_KEY_FILE`
(`~/.config/sops/age/keys.txt`) — the key `claude-session-seal` uses.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
import time
from pathlib import Path

from agent_media_core import book_tracks, harnesses, sealed
from agent_media_core._paths import state_dir
from agent_media_core.state.store import default_db_path

from . import auth, forget, sessions

#: How long a live session gets to exit after its pane is closed.
EXIT_WAIT_S = 20.0
#: And its hooks (SessionEnd, the fleet-sync copy) to finish after it has.
SETTLE_S = 3.0


class SealError(Exception):
    pass


def bundle_dir() -> Path:
    return state_dir() / "sealed"


def bundle_path(session: str) -> Path:
    return bundle_dir() / f"{session}.tar.age"


def _key_file() -> Path:
    return Path(os.environ.get("MEDIA_SEAL_KEY_FILE") or os.environ.get("SOPS_AGE_KEY_FILE")
                or Path.home() / ".config" / "sops" / "age" / "keys.txt").expanduser()


def _recipient() -> str:
    got = os.environ.get("MEDIA_SEAL_RECIPIENT") or os.environ.get("SOPS_AGE_RECIPIENT")
    if got:
        return got
    try:
        out = subprocess.run(["age-keygen", "-y", str(_key_file())], capture_output=True,
                             text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SealError(f"no age recipient ({e})") from None
    if out.returncode or not out.stdout.strip():
        raise SealError(f"no age recipient: {out.stderr.strip() or 'empty key file'}")
    return out.stdout.strip().splitlines()[0]


def staging_roots() -> list[Path]:
    home = Path.home()
    return [home / ".cache" / "claude-incoming", home / ".cache" / "agent-sessions-incoming"]


# --- what a thread has here ----------------------------------------------------


def _transcript_paths(session: str) -> tuple[str, list[Path]]:
    """`(harness, paths)`: the transcript, its folder, the staged copies."""
    harness = harnesses.harness_of(session)
    if harness in (harnesses.HERMES, harnesses.OPENCODE):
        raise SealError(f"a {harness} conversation lives in a shared database and "
                        "cannot be sealed")
    out: list[Path] = []
    found = harnesses.transcript(session)
    if found:
        path = found[1]
        out.append(path)
        side = path.with_suffix("")
        if harness == harnesses.CLAUDE and side.is_dir():
            out.append(side)
        if harness == harnesses.GEMINI:
            # What Gemini deletes with a session (deleteSessionArtifacts,
            # 0.64): its subagents' chats, its tool outputs, its log, and a
            # folder of its own, all in the project folder above `chats/`.
            project = path.parent.parent
            out += [p for p in (path.parent / session,
                                project / "tool-outputs" / f"session-{session}",
                                project / "logs" / f"session-{session}.jsonl",
                                project / session) if p.exists()]
    for root in staging_roots():
        out += [p for p in root.glob(f"*/*/*/{session}.jsonl") if p.is_file()]
    return harness, out


def _clips(rows: list[dict]) -> list[Path]:
    """Every file beside every clip the rows name."""
    out: dict[str, Path] = {}
    for r in rows:
        try:
            ex = json.loads(r.get("extras") or "{}")
        except ValueError:
            ex = {}
        uris = list(ex.get("clip_uris") or []) + [r.get("uri") or ""]
        for u in uris:
            p = Path(str(u))
            if not p.is_absolute() or not p.parent.is_dir():
                continue
            for f in p.parent.glob(f"{p.stem}.*"):
                if f.is_file():
                    out[str(f)] = f
    return list(out.values())


def plan(session: str) -> dict:
    """What sealing `session` would take, without touching anything."""
    if not harnesses.SESSION_ID.fullmatch(session or ""):
        raise SealError(f"not a session id: {session}")
    harness, files = _transcript_paths(session)
    conn = sqlite3.connect(default_db_path())
    try:
        rows = forget._history(conn, session)
    finally:
        conn.close()
    path, man = forget._manifest(session)
    folder = str(man.get("folder") or "")
    shared = sorted(forget._folder_users(folder) - {session}) if folder else []
    # Only the threads that are still readable keep a shared folder.
    shared = [s for s in shared if not sealed.is_sealed(s)]
    return {"session": session, "harness": harness, "transcript": [str(p) for p in files],
            "history": rows, "clips": [str(p) for p in _clips(rows)],
            "manifest": str(path) if path.exists() else "",
            "folder": folder if folder and Path(folder).exists() and not shared else "",
            "folder_kept_for": shared}


# --- the bundle ----------------------------------------------------------------


def _write_bundle(dest: Path, index: dict, paths: list[Path]) -> None:
    """Tar `paths` and `index.json` straight into age: no plaintext on disk."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(dest.parent, 0o700)
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.tmp")
    proc = subprocess.Popen(["age", "-r", _recipient(), "-o", str(tmp)],
                            stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        with tarfile.open(fileobj=proc.stdin, mode="w|") as tar:
            raw = json.dumps(index, indent=1).encode()
            info = tarfile.TarInfo("index.json")
            info.size, info.mtime, info.mode = len(raw), int(time.time()), 0o600
            tar.addfile(info, io.BytesIO(raw))
            for i, p in enumerate(paths):
                tar.add(str(p), arcname=f"item/{i}")
        proc.stdin.close()
        err = proc.stderr.read().decode(errors="replace").strip()
        if proc.wait(timeout=120):
            raise SealError(f"age failed: {err or proc.returncode}")
    except BaseException:
        proc.kill()
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(dest)


def _open_bundle(path: Path):
    """`(proc, tar)`: the bundle decrypted as a stream."""
    proc = subprocess.Popen(["age", "-d", "-i", str(_key_file()), str(path)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return proc, tarfile.open(fileobj=proc.stdout, mode="r|")


def _check_bundle(path: Path, n: int) -> None:
    """Decrypt and list it: `index.json` and `n` items, or SealError."""
    proc, tar = _open_bundle(path)
    names = set()
    try:
        for m in tar:
            names.add(m.name.split("/")[0] + "/" + m.name.split("/")[1] if "/" in m.name else m.name)
    except tarfile.TarError as e:
        proc.kill()
        raise SealError(f"the bundle does not read back ({e})") from None
    finally:
        tar.close()
    err = proc.stderr.read().decode(errors="replace").strip()
    if proc.wait(timeout=60):
        raise SealError(f"the bundle does not decrypt: {err}")
    want = {"index.json"} | {f"item/{i}" for i in range(n)}
    if not want <= names:
        raise SealError(f"the bundle is missing {sorted(want - names)[:3]}")


# --- sealing -------------------------------------------------------------------


def _running(session: str) -> bool:
    if sessions.live_sessions().get(session):
        return True
    from agent_media_core import procinfo

    return any(argv and session in " ".join(argv) for _pid, argv in procinfo.processes())


def _end(session: str) -> bool:
    """End a live session and wait for it to be gone. True when it is."""
    if not _running(session):
        return True
    from . import driver

    ok, _detail = driver.for_session(session).close(session)
    if not ok:
        return False
    deadline = time.monotonic() + EXIT_WAIT_S
    while time.monotonic() < deadline:
        if not _running(session):
            time.sleep(SETTLE_S)
            return True
        time.sleep(0.5)
    return False


def seal(session: str) -> dict:
    """Seal `session` (see the module docstring). The plan, with outcomes."""
    from . import search

    if sealed.is_sealed(session):
        raise SealError("already sealed")
    if not _end(session):
        raise SealError("the session did not exit")
    report = plan(session)
    cwd = sessions.session_cwd(session)
    from . import moves

    project = moves.moved_project(session) or sessions.project_of(cwd, report["folder"])
    paths = [Path(p) for p in report["transcript"] + report["clips"]]
    if report["manifest"]:
        paths.append(Path(report["manifest"]))
    if report["folder"]:
        paths.append(Path(report["folder"]))
    if not paths and not report["history"]:
        raise SealError("nothing of this thread is kept here")
    index = {"session": session, "harness": report["harness"], "cwd": cwd,
             "project": project, "title": sessions._claude_first_prompt(session) or "",
             "sealed_at": time.time(), "items": [str(p) for p in paths],
             "history": report["history"]}
    dest = bundle_path(session)
    # Marked first, so nothing rebuilds the folder or re-indexes the
    # transcript while it is being taken; unmarked if the bundle fails.
    sealed.mark(session, harness=report["harness"] or "", cwd=cwd, project=project or "",
                bundle=str(dest))
    try:
        _write_bundle(dest, index, paths)
        _check_bundle(dest, len(paths))
    except BaseException:
        sealed.unmark(session)
        dest.unlink(missing_ok=True)
        raise
    # Read back whole: only now does the plaintext go.
    conn = sqlite3.connect(default_db_path(), timeout=10.0)
    try:
        with conn:
            conn.execute(
                "DELETE FROM history WHERE json_extract(extras, '$.source_session') = ?",
                (session,))
    finally:
        conn.close()
    for p in paths:
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink(missing_ok=True)
    report["search_rows"] = search.forget(session)
    items = []
    if report["folder"]:
        for url, token, libs in book_tracks._abs_ready_all():
            item = book_tracks._find_item(url, token, libs, Path(report["folder"]))
            if item and item.get("id"):
                items.append({"id": item["id"], "status": forget._abs_delete(url, token, item["id"])})
    report.update(history=len(report["history"]), bundle=str(dest), abs_items=items)
    return report


def unseal(session: str) -> dict:
    """Put a sealed thread back where it was. Files that exist again are
    left alone (and named in `skipped`); history rows go back by id."""
    rec = sealed.sealed().get(session)
    if not rec:
        raise SealError("not sealed")
    path = Path(rec.get("bundle") or bundle_path(session))
    if not path.exists():
        raise SealError(f"the bundle is gone: {path}")
    restored, skipped = [], []
    proc, tar = _open_bundle(path)
    index: dict = {}
    try:
        # index.json is written first, so every item after it has a place.
        for m in tar:
            if m.name == "index.json":
                index = json.loads(tar.extractfile(m).read())
                continue
            parts = m.name.split("/", 2)
            if len(parts) < 2 or parts[0] != "item" or not index:
                continue
            dest = Path(index["items"][int(parts[1])])
            target = dest / parts[2] if len(parts) == 3 else dest
            if len(parts) == 2 and target.exists():
                skipped.append(str(target))
                continue
            if any(str(target).startswith(s + os.sep) for s in skipped):
                continue
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif m.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                with open(target, "wb") as out:
                    shutil.copyfileobj(tar.extractfile(m), out)
                os.chmod(target, m.mode & 0o777 or 0o600)
                os.utime(target, (m.mtime, m.mtime))
            if len(parts) == 2:
                restored.append(str(target))
    except (tarfile.TarError, ValueError, KeyError, IndexError) as e:
        proc.kill()
        raise SealError(f"the bundle does not read back ({e})") from None
    finally:
        tar.close()
    err = proc.stderr.read().decode(errors="replace").strip()
    if proc.wait(timeout=60):
        raise SealError(f"the bundle does not decrypt: {err}")
    rows = index.get("history") or []
    if rows:
        cols = list(rows[0].keys())
        conn = sqlite3.connect(default_db_path(), timeout=10.0)
        try:
            with conn:
                conn.executemany(
                    f"INSERT OR IGNORE INTO history ({', '.join(cols)}) "
                    f"VALUES ({', '.join('?' for _ in cols)})",
                    [tuple(r.get(c) for c in cols) for r in rows])
        finally:
            conn.close()
    sealed.unmark(session)
    path.unlink(missing_ok=True)
    return {"session": session, "restored": restored, "skipped": skipped, "history": len(rows)}


# --- the route -------------------------------------------------------------------


def session_seal(session: str, flag, bearer: str) -> tuple[bool, dict]:
    """`POST /session/seal {"session", "sealed"}`. Gated like `/session/close`.

    `sealed` defaults to true; anything but a JSON boolean is 400. Sealing a
    live session ends it first. 409 when it cannot be sealed (a shared-store
    harness, already sealed, would not exit), 404 when nothing of it is here.
    """
    session = (session or "").strip()
    if not sessions._SESSION.fullmatch(session):
        return False, {"error": "not a session id", "status": 400}
    if flag is None:
        flag = True
    if not isinstance(flag, bool):
        return False, {"error": "sealed must be true or false", "status": 400}
    user, err = auth.gate(bearer)
    if not user:
        return False, err
    try:
        if flag:
            got = seal(session)
            return True, {"session": session, "sealed": True, "history": got["history"],
                          "items": len(got["transcript"]) + len(got["clips"])}
        got = unseal(session)
        return True, {"session": session, "sealed": False, "skipped": got["skipped"]}
    except SealError as e:
        msg = str(e)
        status = 404 if msg.startswith("nothing") else 409
        return False, {"error": msg, "status": status}
    except OSError as e:
        return False, {"error": f"could not seal ({e})", "status": 500}


def run(session: str, *, unseal_: bool = False, dry_run: bool = False) -> int:
    """`media session-seal`."""
    try:
        if dry_run:
            got = plan(session)
            print(f"{session[:8]}: would seal {len(got['transcript'])} transcript file(s), "
                  f"{len(got['history'])} history rows, {len(got['clips'])} clip files, "
                  f"{got['folder'] or 'no folder'}")
            return 0
        if unseal_:
            got = unseal(session)
            print(f"{session[:8]}: unsealed {len(got['restored'])} item(s), "
                  f"{got['history']} history rows"
                  + (f"; left alone (exist again): {', '.join(got['skipped'])}" if got["skipped"] else ""))
            return 0
        got = seal(session)
        print(f"{session[:8]}: sealed {len(got['transcript'])} transcript file(s), "
              f"{got['history']} history rows, {len(got['clips'])} clip files, "
              f"{got['folder'] or 'no folder'} -> {got['bundle']}")
        return 0
    except SealError as e:
        print(f"media session-seal: {e}")
        return 2
