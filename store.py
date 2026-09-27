"""SQLite state: what has been captured, what each version contained, what is due.

Storing each version's exact comment-id set is what makes "what is actually new"
a set difference rather than a guess from totals.
"""
from __future__ import annotations

import sqlite3
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    post_id               TEXT PRIMARY KEY,
    url                   TEXT NOT NULL,
    title                 TEXT,
    subreddit             TEXT,
    file_path             TEXT,
    first_captured        TEXT,
    last_checked          TEXT,
    next_check_at         TEXT,
    watch_until           TEXT,
    current_version       INTEGER NOT NULL DEFAULT 0,
    check_index           INTEGER NOT NULL DEFAULT 0,
    consecutive_no_change INTEGER NOT NULL DEFAULT 0,
    retired               INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS versions (
    post_id           TEXT NOT NULL,
    version           INTEGER NOT NULL,
    created_at        TEXT NOT NULL,
    kept_comments     INTEGER NOT NULL,
    kept_words        INTEGER NOT NULL,
    top_comment_score INTEGER NOT NULL,
    post_score        INTEGER NOT NULL,
    note              TEXT,
    comment_ids       BLOB,
    PRIMARY KEY (post_id, version)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS pending (
    url         TEXT PRIMARY KEY,
    chat_id     TEXT NOT NULL,
    message_id  INTEGER,
    force       INTEGER NOT NULL DEFAULT 0,
    attempts    INTEGER NOT NULL DEFAULT 1,
    first_tried TEXT NOT NULL,
    next_try    TEXT NOT NULL,
    last_error  TEXT
);
CREATE INDEX IF NOT EXISTS idx_posts_due ON posts (retired, next_check_at);
"""


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# ---------- comment id sets ----------

def pack_ids(ids) -> bytes:
    return zlib.compress(",".join(sorted(ids)).encode("utf-8"))


def unpack_ids(blob: bytes | None) -> set[str]:
    if not blob:
        return set()
    raw = zlib.decompress(blob).decode("utf-8")
    return set(raw.split(",")) if raw else set()


# ---------- posts ----------

def get_post(conn: sqlite3.Connection, post_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM posts WHERE post_id = ?", (post_id,)).fetchone()


def create_post(conn: sqlite3.Connection, *, post_id: str, url: str, title: str,
                subreddit: str, file_path: str, watch_days: int,
                first_check_in_hours: int) -> None:
    start = now_utc()
    conn.execute(
        """INSERT INTO posts (post_id, url, title, subreddit, file_path,
                              first_captured, last_checked, next_check_at,
                              watch_until, current_version, check_index)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1)""",
        (post_id, url, title, subreddit, file_path, iso(start), iso(start),
         iso(start + timedelta(hours=first_check_in_hours)),
         iso(start + timedelta(days=watch_days))),
    )
    conn.commit()


def update_after_check(conn: sqlite3.Connection, post_id: str, *,
                       next_check_at: datetime | None, check_index: int,
                       consecutive_no_change: int, retired: bool,
                       version: int | None = None, file_path: str | None = None,
                       title: str | None = None) -> None:
    sets = ["last_checked = ?", "next_check_at = ?", "check_index = ?",
            "consecutive_no_change = ?", "retired = ?"]
    args: list = [iso(now_utc()), iso(next_check_at) if next_check_at else None,
                  check_index, consecutive_no_change, int(retired)]
    if version is not None:
        sets.append("current_version = ?")
        args.append(version)
    if file_path is not None:
        sets.append("file_path = ?")
        args.append(file_path)
    if title is not None:
        sets.append("title = ?")
        args.append(title)
    args.append(post_id)
    conn.execute(f"UPDATE posts SET {', '.join(sets)} WHERE post_id = ?", args)
    conn.commit()


def due_posts(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT * FROM posts
           WHERE retired = 0 AND next_check_at IS NOT NULL AND next_check_at <= ?
           ORDER BY next_check_at LIMIT ?""",
        (iso(now_utc()), limit),
    ).fetchall()


def watched_posts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM posts WHERE retired = 0 ORDER BY next_check_at"
    ).fetchall()


def retire(conn: sqlite3.Connection, post_id: str) -> None:
    conn.execute("UPDATE posts SET retired = 1, next_check_at = NULL WHERE post_id = ?",
                 (post_id,))
    conn.commit()


def postpone(conn: sqlite3.Connection, post_id: str, when: datetime) -> None:
    """Move the next check without touching the ladder (used after an outage)."""
    conn.execute("UPDATE posts SET next_check_at = ? WHERE post_id = ?", (iso(when), post_id))
    conn.commit()


# ---------- retry queue: shares that failed because a source was down ----------

def queue_pending(conn: sqlite3.Connection, url: str, chat_id: str, message_id: int | None,
                  force: bool, next_try: datetime, error: str) -> None:
    conn.execute(
        """INSERT INTO pending (url, chat_id, message_id, force, attempts, first_tried,
                                next_try, last_error)
           VALUES (?, ?, ?, ?, 1, ?, ?, ?)
           ON CONFLICT(url) DO UPDATE SET next_try = excluded.next_try,
                                          last_error = excluded.last_error""",
        (url, chat_id, message_id, int(force), iso(now_utc()), iso(next_try), error))
    conn.commit()


def due_pending(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM pending WHERE next_try <= ? ORDER BY next_try LIMIT ?",
        (iso(now_utc()), limit)).fetchall()


def all_pending(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM pending ORDER BY next_try").fetchall()


def reschedule_pending(conn: sqlite3.Connection, url: str, next_try: datetime,
                       error: str) -> None:
    conn.execute(
        """UPDATE pending SET attempts = attempts + 1, next_try = ?, last_error = ?
           WHERE url = ?""", (iso(next_try), error, url))
    conn.commit()


def drop_pending(conn: sqlite3.Connection, url: str) -> None:
    conn.execute("DELETE FROM pending WHERE url = ?", (url,))
    conn.commit()


# ---------- versions ----------

def add_version(conn: sqlite3.Connection, post_id: str, version: int, metrics,
                note: str = "") -> None:
    conn.execute(
        """INSERT OR REPLACE INTO versions
           (post_id, version, created_at, kept_comments, kept_words,
            top_comment_score, post_score, note, comment_ids)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (post_id, version, iso(now_utc()), metrics.kept_comments, metrics.kept_words,
         metrics.top_comment_score, metrics.post_score, note,
         pack_ids(metrics.comment_ids)),
    )
    conn.commit()


def latest_version(conn: sqlite3.Connection, post_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM versions WHERE post_id = ? ORDER BY version DESC LIMIT 1",
        (post_id,),
    ).fetchone()


def all_versions(conn: sqlite3.Connection, post_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM versions WHERE post_id = ? ORDER BY version", (post_id,)
    ).fetchall()


# ---------- meta ----------

def get_meta(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
