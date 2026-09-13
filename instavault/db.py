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


# Columns added after the first release. Applied with ALTER TABLE so an existing
# index survives - the database holds the user's whole sync and must never be
# recreated to pick up a new field.
MIGRATIONS: dict[str, dict[str, str]] = {
    "items": {
        "audio_url": "TEXT DEFAULT ''",
        "audio_title": "TEXT DEFAULT ''",
        "audio_artist": "TEXT DEFAULT ''",
        "audio_kind": "TEXT DEFAULT ''",       # original | music | ''
        "audio_asset_id": "TEXT DEFAULT ''",
    },
    "downloads": {
        "mode": "TEXT DEFAULT 'full'",         # full | audio
    },
}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn: sqlite3.Connection) -> list[str]:
    applied = []
    for table, columns in MIGRATIONS.items():
        existing = _columns(conn, table)
        for name, decl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                applied.append(f"{table}.{name}")
    return applied


def init() -> None:
    with transaction() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Every column the item upsert binds, with a safe default. Callers that predate
# a field (or rows built from a partial payload) still insert cleanly.
ITEM_DEFAULTS: dict[str, Any] = {
    "typename": "GraphImage",
    "is_video": 0,
    "caption": "",
    "owner": "",
    "thumb_url": "",
    "taken_at": None,
    "media_count": 1,
    "video_duration": None,
    "collection": "",
    "audio_url": "",
    "audio_title": "",
    "audio_artist": "",
    "audio_kind": "",
    "audio_asset_id": "",
}


def _normalise(row: dict[str, Any]) -> dict[str, Any]:
    out = {**ITEM_DEFAULTS, **row}
    out["discovered_at"] = row.get("discovered_at") or _now()
    return out


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
                               collection, discovered_at, unsaved,
                               audio_url, audio_title, audio_artist, audio_kind,
                               audio_asset_id)
            VALUES (:shortcode, :typename, :is_video, :caption, :owner,
                    :thumb_url, :taken_at, :media_count, :video_duration,
                    :collection, :discovered_at, 0,
                    :audio_url, :audio_title, :audio_artist, :audio_kind,
                    :audio_asset_id)
            ON CONFLICT(shortcode) DO UPDATE SET
                thumb_url      = excluded.thumb_url,
                caption        = excluded.caption,
                owner          = excluded.owner,
                collection     = CASE WHEN excluded.collection != ''
                                      THEN excluded.collection ELSE items.collection END,
                audio_url      = excluded.audio_url,
                audio_title    = excluded.audio_title,
                audio_artist   = excluded.audio_artist,
                audio_kind     = excluded.audio_kind,
                audio_asset_id = excluded.audio_asset_id,
                unsaved        = 0
            """,
            [_normalise(row) for row in rows],
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


def _filters(
    search: str = "",
    kind: str = "all",
    state: str = "all",
    collection: str = "",
    audio: str = "any",
    artist: str = "",
    owner: str = "",
    duration: str = "any",
):
    """Build the WHERE clause every query, count and bulk-select shares.

    Everything funnels through here so a new filter automatically applies to the
    grid, the sidebar counts and "select all matching" at once.
    """
    where = ["i.unsaved = 0"]
    params: list[Any] = []

    if search:
        where.append(
            "(i.caption LIKE ? OR i.owner LIKE ? "
            "OR i.audio_title LIKE ? OR i.audio_artist LIKE ?)"
        )
        params += [f"%{search}%"] * 4

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

    if audio == "original":
        where.append("i.audio_kind = 'original'")
    elif audio == "licensed":
        where.append("i.audio_kind = 'music'")
    elif audio == "any_audio":
        where.append("i.audio_kind != ''")
    elif audio == "none":
        where.append("(i.audio_kind IS NULL OR i.audio_kind = '')")

    if artist:
        where.append("i.audio_artist LIKE ?")
        params.append(f"%{artist}%")

    if owner:
        where.append("i.owner LIKE ?")
        params.append(f"%{owner}%")

    # Durations are only meaningful for videos; photos have NULL.
    if duration == "short":
        where.append("i.video_duration > 0 AND i.video_duration < 30")
    elif duration == "medium":
        where.append("i.video_duration >= 30 AND i.video_duration <= 60")
    elif duration == "long":
        where.append("i.video_duration > 60")

    return " AND ".join(where), params


# Accepted filter keys, so callers can forward request args without spelling
# every parameter out three times over.
FILTER_KEYS = ("search", "kind", "state", "collection", "audio", "artist", "owner", "duration")

ORDERS = {
    "newest": "i.taken_at DESC",
    "oldest": "i.taken_at ASC",
    "owner": "i.owner ASC, i.taken_at DESC",
    "added": "i.discovered_at DESC",
    "longest": "i.video_duration DESC NULLS LAST",
    "shortest": "i.video_duration ASC NULLS LAST",
    "artist": "i.audio_artist ASC, i.taken_at DESC",
}


def query_items(
    sort: str = "newest", limit: int = 200, offset: int = 0, **filters: Any
) -> list[dict[str, Any]]:
    clause, params = _filters(**filters)
    order = ORDERS.get(sort, ORDERS["newest"])

    sql = f"""
        SELECT i.*, d.status AS download_status, d.path AS download_path,
               d.bytes AS download_bytes, d.error AS download_error,
               d.mode AS download_mode
        FROM items i
        LEFT JOIN downloads d ON d.shortcode = i.shortcode
        WHERE {clause}
        ORDER BY {order}
        LIMIT ? OFFSET ?
    """
    return [dict(r) for r in connection().execute(sql, params + [limit, offset])]


def count_items(**filters: Any) -> int:
    clause, params = _filters(**filters)
    sql = f"""
        SELECT COUNT(*) FROM items i
        LEFT JOIN downloads d ON d.shortcode = i.shortcode
        WHERE {clause}
    """
    return connection().execute(sql, params).fetchone()[0]


def all_shortcodes(**filters: Any) -> list[str]:
    """Every shortcode matching a filter - used by 'select all matching'."""
    clause, params = _filters(**filters)
    sql = f"""
        SELECT i.shortcode FROM items i
        LEFT JOIN downloads d ON d.shortcode = i.shortcode
        WHERE {clause} ORDER BY i.taken_at DESC
    """
    return [r["shortcode"] for r in connection().execute(sql, params)]


def artists(limit: int = 200) -> list[str]:
    """Artists actually present, for the filter's autocomplete list."""
    rows = connection().execute(
        """SELECT audio_artist, COUNT(*) AS n FROM items
           WHERE unsaved = 0 AND audio_artist != ''
           GROUP BY audio_artist ORDER BY n DESC LIMIT ?""",
        (limit,),
    )
    return [r["audio_artist"] for r in rows]


def get_item(shortcode: str) -> dict[str, Any] | None:
    row = connection().execute(
        """SELECT i.*, d.status AS download_status, d.path AS download_path
           FROM items i LEFT JOIN downloads d ON d.shortcode = i.shortcode
           WHERE i.shortcode = ?""",
        (shortcode,),
    ).fetchone()
    return dict(row) if row else None


def collections() -> list[dict[str, Any]]:
    """Collections grouped by id, with the user-assigned name when there is one.

    Instagram gives us only the id over a cookie session, so an unnamed
    collection shows a short label the user can rename.
    """
    import json

    names = json.loads(get_meta("collection_names", "{}") or "{}")
    rows = connection().execute(
        """SELECT collection AS id, COUNT(*) AS count
           FROM items WHERE unsaved = 0 AND collection != ''
           GROUP BY collection ORDER BY count DESC"""
    )
    out = []
    for r in rows:
        cid = r["id"]
        out.append(
            {
                "id": cid,
                "name": names.get(cid) or f"Collection {cid[-4:]}",
                "named": cid in names,
                "count": r["count"],
            }
        )
    return out


def rename_collection(collection_id: str, name: str) -> None:
    import json

    names = json.loads(get_meta("collection_names", "{}") or "{}")
    name = name.strip()
    if name:
        names[collection_id] = name[:60]
    else:
        names.pop(collection_id, None)
    set_meta("collection_names", json.dumps(names))


def collection_name(collection_id: str) -> str:
    import json

    if not collection_id:
        return "saved"
    names = json.loads(get_meta("collection_names", "{}") or "{}")
    return names.get(collection_id) or f"collection_{collection_id[-4:]}"


# ----------------------------------------------------------------------- downloads


def record_download(
    shortcode: str,
    status: str,
    path: str = "",
    size: int = 0,
    files: int = 0,
    error: str = "",
    mode: str = "full",
) -> None:
    with transaction() as conn:
        conn.execute(
            """
            INSERT INTO downloads (shortcode, status, path, bytes, files, error,
                                   mode, attempts, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(shortcode) DO UPDATE SET
                status       = excluded.status,
                path         = excluded.path,
                bytes        = excluded.bytes,
                files        = excluded.files,
                error        = excluded.error,
                mode         = excluded.mode,
                attempts     = downloads.attempts + 1,
                completed_at = excluded.completed_at
            """,
            (shortcode, status, path, size, files, error, mode, _now()),
        )


def is_downloaded(shortcode: str, mode: str = "full") -> bool:
    """Has this item already been fetched to satisfy `mode`?

    A full download satisfies an audio-only request, but not the reverse -
    otherwise an audio-only grab would block ever fetching the video.
    """
    row = connection().execute(
        "SELECT mode FROM downloads WHERE shortcode = ? AND status = 'done'",
        (shortcode,),
    ).fetchone()
    if row is None:
        return False
    return True if mode == "audio" else (row["mode"] or "full") == "full"


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
    original = conn.execute(
        "SELECT COUNT(*) FROM items WHERE unsaved = 0 AND audio_kind = 'original'"
    ).fetchone()[0]
    licensed = conn.execute(
        "SELECT COUNT(*) FROM items WHERE unsaved = 0 AND audio_kind = 'music'"
    ).fetchone()[0]
    return {
        "total": total,
        "downloaded": done,
        "failed": failed,
        "pending": max(total - done, 0),
        "videos": videos,
        "photos": max(total - videos, 0),
        "bytes": size,
        "audio_original": original,
        "audio_licensed": licensed,
        "audio_none": max(total - original - licensed, 0),
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
