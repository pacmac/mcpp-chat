"""SQLite plumbing for mcpp-chat.

N agent sessions are N processes writing one file, so every connection gets WAL
plus a busy timeout, and every write runs inside a short BEGIN IMMEDIATE block.
"""

from __future__ import annotations

import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

from .config import get_config, module_dir

SCHEMA_VERSION = 1

BUSY_TIMEOUT_MS = 5000


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Paths ──

def resolve_db_path() -> Path:
    """Resolve the chat database path.

    Order: MCPP_CHAT_DB env > storage.db_path in config > chat.db beside module.
    Resolved on every call so tests can repoint it without reimporting.
    """
    env = os.environ.get("MCPP_CHAT_DB", "").strip()
    if env:
        return Path(env).expanduser()
    configured = str(get_config().get("storage", {}).get("db_path", "") or "").strip()
    if configured:
        p = Path(configured).expanduser()
        return p if p.is_absolute() else (module_dir() / p)
    return module_dir() / "chat.db"


def tick_path() -> Optional[Path]:
    """Path of the watcher tick file, or None when disabled."""
    name = str(get_config().get("chat", {}).get("tick_file", "") or "").strip()
    if not name:
        return None
    p = Path(name).expanduser()
    return p if p.is_absolute() else (resolve_db_path().parent / p)


def write_tick(channel: str, seq: int, author: str) -> None:
    """Overwrite the tick file with one line describing the newest write.

    Overwrite (not append) keeps it O(1) forever. A watcher only needs to see
    that the file changed; it then calls chat_read for the actual content.
    """
    p = tick_path()
    if p is None:
        return
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"{utc_now_iso()} {channel} {seq} {author}\n", encoding="utf-8")
    except OSError:
        pass  # a missing tick file must never fail a write


# ── Connection ──

def connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else resolve_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS};")
    return conn


@contextmanager
def writing(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Short exclusive write transaction.

    BEGIN IMMEDIATE takes the write lock up front, so two sessions allocating a
    per-channel seq at the same moment serialise instead of colliding on the
    UNIQUE(channel_id, seq) index.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


# ── Schema ──

def get_schema_version(conn: sqlite3.Connection) -> Optional[int]:
    try:
        row = conn.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()
    except sqlite3.OperationalError:
        return None
    return int(row["version"]) if row else None


def set_schema_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO schema_version (id, version, updated_at) VALUES (1, ?, ?)",
        (version, utc_now_iso()),
    )


def _apply_patches(conn: sqlite3.Connection, current: int) -> int:
    patches_dir = module_dir() / "schema_patches"
    if not patches_dir.is_dir():
        return current
    found: list[tuple[int, Path]] = []
    for path in patches_dir.glob("patch-*.sql"):
        m = re.match(r"patch-(\d+)\.sql$", path.name)
        if m:
            found.append((int(m.group(1)), path))
    for version, path in sorted(found):
        if version <= current:
            continue
        conn.executescript(path.read_text(encoding="utf-8"))
        set_schema_version(conn, version)
        current = version
    return current


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the schema if absent, then apply any ordered patches."""
    conn.executescript((module_dir() / "schema.sql").read_text(encoding="utf-8"))
    version = get_schema_version(conn)
    if version is None:
        set_schema_version(conn, SCHEMA_VERSION)
        version = SCHEMA_VERSION
    if version < SCHEMA_VERSION:
        _apply_patches(conn, version)


def open_db() -> sqlite3.Connection:
    """Connect + ensure schema + opportunistic daily backup."""
    conn = connect()
    ensure_schema(conn)
    _maybe_daily_backup()
    return conn


# ── Backups ──

def _maybe_daily_backup() -> None:
    cfg = get_config().get("storage", {})
    if not cfg.get("daily_backup", True):
        return
    db_path = resolve_db_path()
    if not db_path.exists():
        return
    backup_dir = db_path.parent / ".backups"
    stamp = datetime.now().strftime("%y%m%d")
    target = backup_dir / f"{db_path.name}.{stamp}"
    if target.exists():
        return
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        # sqlite3 online backup: safe while other processes hold the DB.
        src = connect(db_path)
        try:
            dest = sqlite3.connect(str(target))
            try:
                src.backup(dest)
            finally:
                dest.close()
        finally:
            src.close()
        _prune_backups(backup_dir, db_path.name, int(cfg.get("backup_retain_days", 7)))
    except (OSError, sqlite3.Error):
        pass  # a failed backup must never break a chat call


def _prune_backups(backup_dir: Path, db_name: str, retain_days: int) -> None:
    if retain_days <= 0:
        return
    cutoff = datetime.now() - timedelta(days=retain_days)
    for path in backup_dir.glob(f"{db_name}.*"):
        m = re.search(r"\.(\d{6})$", path.name)
        if not m:
            continue
        try:
            when = datetime.strptime(m.group(1), "%y%m%d")
        except ValueError:
            continue
        if when < cutoff:
            try:
                path.unlink()
            except OSError:
                pass


__all__ = [
    "SCHEMA_VERSION", "connect", "open_db", "ensure_schema", "writing",
    "resolve_db_path", "tick_path", "write_tick", "utc_now_iso",
    "get_schema_version", "set_schema_version",
]
