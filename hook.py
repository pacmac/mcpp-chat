#!/usr/bin/env python3
"""Claude Code hook: surface unread mcpp-chat without being asked.

The chat_* tools only run when an agent chooses to call one, so an idle session
never notices a peer. The harness does run code on its own schedule — this
script is what it runs.

    stdin   the hook payload JSON (cwd, stop_hook_active, ...)
    stdout  event-shaped JSON, or nothing at all
    exit    always 0

Read-only by construction: it opens the database with mode=ro and never advances
a cursor. Displaying a message must not mark it read, or the agent would never
see it.

Usage:
    python3 hook.py --event SessionStart | UserPromptSubmit | Stop
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

_DIR = Path(__file__).resolve().parent

PREVIEW_CHARS = 140
MAX_PREVIEWS = 3


# ── Database (read-only) ──

def _db_path() -> Path:
    """Mirror db.resolve_db_path() without importing the write path."""
    env = os.environ.get("MCPP_CHAT_DB", "").strip()
    if env:
        return Path(env).expanduser()
    configured = ""
    try:
        import yaml  # optional; absence just means defaults
        cfg_file = Path(os.environ.get("MCPP_CHAT_CONFIG", "").strip() or (_DIR / "config.yaml"))
        if cfg_file.exists():
            loaded = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                configured = str(loaded.get("storage", {}).get("db_path", "") or "").strip()
    except Exception:
        configured = ""
    if configured:
        p = Path(configured).expanduser()
        return p if p.is_absolute() else (_DIR / p)
    return _DIR / "chat.db"


def _connect_ro(path: Path) -> sqlite3.Connection:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        # A WAL database with no -shm yet can refuse a pure read-only open.
        # Fall back to a normal handle; this script still never writes.
        conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


# ── Pending work for one party ──

def _workspace(payload: dict) -> str:
    for candidate in (payload.get("cwd"), os.environ.get("CLAUDE_PROJECT_DIR"), os.getcwd()):
        if candidate:
            try:
                return str(Path(candidate).expanduser().resolve())
            except OSError:
                continue
    return os.getcwd()


def _pending(conn: sqlite3.Connection, workspace: str) -> dict | None:
    """Everything waiting for this repo, or None when there is nothing to say."""
    me = conn.execute(
        "SELECT id, name FROM parties WHERE workspace_path = ?", (workspace,)
    ).fetchone()
    if me is None:
        return None  # this repo has never used chat: nothing to report

    pid = int(me["id"])
    channels = conn.execute(
        "SELECT c.id, c.name FROM channels c JOIN channel_parties cp ON cp.channel_id = c.id "
        "WHERE cp.party_id = ? AND cp.left_at IS NULL AND c.is_closed = 0 ORDER BY c.name",
        (pid,),
    ).fetchall()
    if not channels:
        return None

    unread: list[dict] = []
    previews: list[str] = []
    for ch in channels:
        cursor = conn.execute(
            "SELECT last_seq FROM cursors WHERE channel_id = ? AND party_id = ?",
            (ch["id"], pid),
        ).fetchone()
        last_seq = int(cursor["last_seq"]) if cursor else 0
        rows = conn.execute(
            "SELECT m.seq, m.kind, m.slug, m.title, m.body, p.name AS author "
            "FROM messages m JOIN parties p ON p.id = m.author_id "
            "WHERE m.channel_id = ? AND m.seq > ? AND m.is_archived = 0 AND m.author_id != ? "
            "ORDER BY m.seq",
            (ch["id"], last_seq, pid),
        ).fetchall()
        if not rows:
            continue
        unread.append({"channel": ch["name"], "count": len(rows)})
        for r in rows[-MAX_PREVIEWS:]:
            body = " ".join((r["body"] or "").split())
            if len(body) > PREVIEW_CHARS:
                body = body[:PREVIEW_CHARS] + "…"
            label = f"[{r['slug']}] {r['title']}: " if r["kind"] == "ask" and r["slug"] else ""
            previews.append(f"  {ch['name']} #{r['seq']} {r['author']}: {label}{body}")

    needs: list[str] = []
    items = conn.execute(
        "SELECT m.slug, m.seq, m.title, m.status, m.author_id, c.name AS channel, "
        "       a.name AS author, ic.acked_at, ic.prune_ok_at "
        "FROM messages m "
        "JOIN channels c ON c.id = m.channel_id "
        "JOIN parties a ON a.id = m.author_id "
        "LEFT JOIN item_consumers ic ON ic.message_id = m.id AND ic.party_id = ? "
        "WHERE m.kind = 'ask' AND m.is_archived = 0 "
        "  AND m.channel_id IN (SELECT channel_id FROM channel_parties "
        "                       WHERE party_id = ? AND left_at IS NULL) "
        "ORDER BY c.name, m.seq",
        (pid, pid),
    ).fetchall()
    for it in items:
        ident = f"[{it['slug'] or '#' + str(it['seq'])}] \"{it['title']}\""
        if it["acked_at"] is None and it["status"] in ("open", "ack") and it["author_id"] != pid:
            needs.append(f"  {ident} from {it['author']} — needs your ack")
        elif int(it["author_id"]) == pid and it["status"] == "ack":
            needs.append(f"  {ident} — answered; needs your resolve if you are satisfied")
        elif it["status"] == "done" and it["prune_ok_at"] is None:
            needs.append(f"  {ident} — resolved; needs your prune-ok")

    if not unread and not needs:
        return None
    return {
        "me": me["name"],
        "unread": unread,
        "previews": previews,
        "needs": needs,
        "total_unread": sum(u["count"] for u in unread),
    }


# ── Rendering ──

def _summary(p: dict) -> str:
    lines = [f"mcpp-chat — you are `{p['me']}` on the cross-session channel."]
    if p["unread"]:
        where = ", ".join(f"{u['channel']} ({u['count']})" for u in p["unread"])
        lines.append(f"Unread: {where}")
        lines.extend(p["previews"])
    if p["needs"]:
        lines.append("Waiting on you:")
        lines.extend(p["needs"])
    lines.append(
        "Use chat_inbox / chat_read to take them. Answer only for your own repo — "
        "ask the peer about theirs, and never edit their files."
    )
    return "\n".join(lines)


def _stop_reason(p: dict) -> str:
    head = "Do not finish yet — a session in another repo is waiting on you."
    return (
        f"{head}\n{_summary(p)}\n"
        "Read them now and reply where you can. If an item needs no answer from you, "
        "chat_ack it so the peer knows it is in hand, or chat_prune_ok it if it is resolved. "
        "Then finish."
    )


def main() -> int:
    event = ""
    workspace_override = ""
    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--event" and i + 1 < len(args):
            event = args[i + 1]
        elif arg == "--workspace" and i + 1 < len(args):
            workspace_override = args[i + 1]

    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    except Exception:
        raw = ""
    try:
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
    except json.JSONDecodeError:
        payload = {}

    # A Stop hook that already fired must not fire again: no block loops.
    if event == "Stop" and payload.get("stop_hook_active"):
        return 0

    workspace = workspace_override or _workspace(payload)

    path = _db_path()
    if not path.exists():
        return 0
    conn = None
    try:
        conn = _connect_ro(path)
        pending = _pending(conn, workspace)
    except Exception:
        return 0  # never break a session
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    if not pending:
        return 0

    if event == "Stop":
        out = {"decision": "block", "reason": _stop_reason(pending)}
    elif event in ("SessionStart", "UserPromptSubmit"):
        out = {
            "hookSpecificOutput": {
                "hookEventName": event,
                "additionalContext": _summary(pending),
            },
            "suppressOutput": True,
        }
    else:
        print(_summary(pending))
        return 0

    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        raise SystemExit(0)  # belt and braces: a hook must never fail a session
