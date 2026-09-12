"""SQLite index of saved items and their download state.

Keeps a local record so the app knows what it has already pulled, can filter and
search without hitting Instagram, and can resume after a crash.
"""

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator

from . import config

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    shortcode       TEXT PRIMARY KEY,
    typename        TEXT,
    is_video        INTEGER DEFAULT 0,
    caption         TEXT DEFAULT '',
    owner           TEXT DEFAULT '',
    thumb_url       TEXT DEFAULT '',
    taken_at        TEXT,
    media_count     INTEGER DEFAULT 1,
    video_duration  REAL,
    collection      TEXT DEFAULT '',
    discovered_at   TEXT,
    unsaved         INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS downloads (
    shortcode    TEXT PRIMARY KEY,
    status       TEXT NOT NULL,
    path         TEXT DEFAULT '',
    bytes        INTEGER DEFAULT 0,
    files        INTEGER DEFAULT 0,
    error        TEXT DEFAULT '',
    attempts     INTEGER DEFAULT 0,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_items_taken ON items(taken_at DESC);
CREATE INDEX IF NOT EXISTS idx_items_owner ON items(owner);
CREATE INDEX IF NOT EXISTS idx_dl_status   ON downloads(status);
"""


def _connect() -> sqlite3.Connection:
    config.ensure_dirs()
    conn = sqlite3.connect(config.DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def connection() -> sqlite3.Connection:
    """One connection per thread - the job worker and request threads differ."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _local.conn = _connect()
    return conn


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    conn = connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init() -> None:
    with transaction() as conn:
        conn.executescript(SCHEMA)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- items


def upsert_items(items: Iterable[dict[str, Any]]) -> int:
    """Insert or refresh saved items. Returns how many were new."""
    rows = list(items)
    if not rows:
        return 0

    placeholders = ",".join("?" * len(rows))
    with transaction() as conn:
        existing = {
            r["shortcode"]
            for r in conn.execute(
                f"SELECT shortcode FROM items WHERE shortcode IN ({placeholders})",
                [r["shortcode"] for r in rows],
            )
        }
        conn.executemany(
            """
            INSERT INTO items (shortcode, typename, is_video, caption, owner,
                               thumb_url, taken_at, media_count, video_duration,
                               collection, discovered_at, unsaved)
            VALUES (:shortcode, :typename, :is_video, :caption, :owner,
                    :thumb_url, :taken_at, :media_count, :video_duration,
                    :collection, :discovered_at, 0)
            ON CONFLICT(shortcode) DO UPDATE SET
                thumb_url  = excluded.thumb_url,
                caption    = excluded.caption,
                owner      = excluded.owner,
                collection = CASE WHEN excluded.collection != ''
                                  THEN excluded.collection ELSE items.collection END,
                unsaved    = 0
            """,
            [{**row, "discovered_at": row.get("discovered_at") or _now()} for row in rows],
        )
    return len(rows) - len(existing)


def mark_unsaved(keep: set[str]) -> int:
    """Flag items that are in our index but no longer in the Saved feed."""
    if not keep:
        return 0
    placeholders = ",".join("?" * len(keep))
    with transaction() as conn:
        cur = conn.execute(
            f"UPDATE items SET unsaved = 1 "
            f"WHERE unsaved = 0 AND shortcode NOT IN ({placeholders})",
            list(keep),
        )
        return cur.rowcount


def _filters(search: str, kind: str, state: str, collection: str):
    where = ["i.unsaved = 0"]
    params: list[Any] = []

    if search:
        where.append("(i.caption LIKE ? OR i.owner LIKE ?)")
        params += [f"%{search}%", f"%{search}%"]

    if kind == "video":
        where.append("i.is_video = 1")
    elif kind == "photo":
        where.append("i.is_video = 0 AND i.typename != 'GraphSidecar'")
    elif kind == "carousel":
        where.append("i.typename = 'GraphSidecar'")

    if state == "downloaded":
        where.append("d.status = 'done'")
    elif state == "pending":
        where.append("(d.status IS NULL OR d.status != 'done')")
    elif state == "failed":
        where.append("d.status = 'failed'")

    if collection:
        where.append("i.collection = ?")
        params.append(collection)

    return " AND ".join(where), params


def query_items(
    search: str = "",
    kind: str = "all",
    state: str = "all",
    collection: str = "",
    sort: str = "newest",
    limit: int = 200,
    offset: int = 0,
) -> list[dict[str, Any]]:
    clause, params = _filters(search, kind, state, collection)
    order = {
        "newest": "i.taken_at DESC",
        "oldest": "i.taken_at ASC",
        "owner": "i.owner ASC, i.taken_at DESC",
        "added": "i.discovered_at DESC",
    }.get(sort, "i.taken_at DESC")

    sql = f"""
        SELECT i.*, d.status AS download_status, d.path AS download_path,
               d.bytes AS download_bytes, d.error AS download_error
        FROM items i
        LEFT JOIN downloads d ON d.shortcode = i.shortcode
        WHERE {clause}
        ORDER BY {order}
        LIMIT ? OFFSET ?
    """
    return [dict(r) for r in connection().execute(sql, params + [limit, offset])]


def count_items(
    search: str = "", kind: str = "all", state: str = "all", collection: str = ""
) -> int:
    clause, params = _filters(search, kind, state, collection)
    sql = f"""
        SELECT COUNT(*) FROM items i
        LEFT JOIN downloads d ON d.shortcode = i.shortcode
        WHERE {clause}
    """
    return connection().execute(sql, params).fetchone()[0]


def all_shortcodes(
    search: str = "", kind: str = "all", state: str = "all", collection: str = ""
) -> list[str]:
    """Every shortcode matching a filter - used by 'select all matching'."""
    clause, params = _filters(search, kind, state, collection)
    sql = f"""
        SELECT i.shortcode FROM items i
        LEFT JOIN downloads d ON d.shortcode = i.shortcode
        WHERE {clause} ORDER BY i.taken_at DESC
    """
    return [r["shortcode"] for r in connection().execute(sql, params)]


def get_item(shortcode: str) -> dict[str, Any] | None:
    row = connection().execute(
        """SELECT i.*, d.status AS download_status, d.path AS download_path
           FROM items i LEFT JOIN downloads d ON d.shortcode = i.shortcode
           WHERE i.shortcode = ?""",
        (shortcode,),
    ).fetchone()
    return dict(row) if row else None


def collections() -> list[dict[str, Any]]:
    rows = connection().execute(
        """SELECT collection AS name, COUNT(*) AS count
           FROM items WHERE unsaved = 0 AND collection != ''
           GROUP BY collection ORDER BY count DESC"""
    )
    return [dict(r) for r in rows]


# ----------------------------------------------------------------------- downloads


def record_download(
    shortcode: str,
    status: str,
    path: str = "",
    size: int = 0,
    files: int = 0,
    error: str = "",
) -> None:
    with transaction() as conn:
        conn.execute(
            """
            INSERT INTO downloads (shortcode, status, path, bytes, files, error,
                                   attempts, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(shortcode) DO UPDATE SET
                status       = excluded.status,
                path         = excluded.path,
                bytes        = excluded.bytes,
                files        = excluded.files,
                error        = excluded.error,
                attempts     = downloads.attempts + 1,
                completed_at = excluded.completed_at
            """,
            (shortcode, status, path, size, files, error, _now()),
        )


def is_downloaded(shortcode: str) -> bool:
    row = connection().execute(
        "SELECT 1 FROM downloads WHERE shortcode = ? AND status = 'done'", (shortcode,)
    ).fetchone()
    return row is not None


def stats() -> dict[str, Any]:
    conn = connection()
    total = conn.execute("SELECT COUNT(*) FROM items WHERE unsaved = 0").fetchone()[0]
    done = conn.execute(
        """SELECT COUNT(*) FROM downloads d JOIN items i ON i.shortcode = d.shortcode
           WHERE d.status = 'done' AND i.unsaved = 0"""
    ).fetchone()[0]
    failed = conn.execute(
        """SELECT COUNT(*) FROM downloads d JOIN items i ON i.shortcode = d.shortcode
           WHERE d.status = 'failed' AND i.unsaved = 0"""
    ).fetchone()[0]
    size = conn.execute("SELECT COALESCE(SUM(bytes), 0) FROM downloads").fetchone()[0]
    videos = conn.execute(
        "SELECT COUNT(*) FROM items WHERE unsaved = 0 AND is_video = 1"
    ).fetchone()[0]
    return {
        "total": total,
        "downloaded": done,
        "failed": failed,
        "pending": max(total - done, 0),
        "videos": videos,
        "photos": max(total - videos, 0),
        "bytes": size,
    }


# ---------------------------------------------------------------------------- meta


def set_meta(key: str, value: str) -> None:
    with transaction() as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def get_meta(key: str, default: str = "") -> str:
    row = connection().execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default
