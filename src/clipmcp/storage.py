"""
storage.py — All SQLite read/write operations for ClipMCP.

This is the only module that touches the database.
All other modules call functions here — never raw SQL elsewhere.

v1.1 additions:
  - content_type column: 'text' | 'image'
  - file_path column: path to image file on disk (null for text clips)
  - image file cleanup on delete/prune
"""

from __future__ import annotations

import hashlib
import sqlite3
import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Generator, Optional

from .config import config

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS clipboard_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    content         TEXT NOT NULL,
    content_preview TEXT,
    content_hash    TEXT NOT NULL,
    category        TEXT DEFAULT 'text',
    source_app      TEXT,
    char_count      INTEGER,
    is_pinned       BOOLEAN DEFAULT 0,
    is_sensitive    BOOLEAN DEFAULT 0,
    content_type    TEXT DEFAULT 'text',
    file_path       TEXT,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_created_at    ON clipboard_history(created_at);
CREATE INDEX IF NOT EXISTS idx_category      ON clipboard_history(category);
CREATE INDEX IF NOT EXISTS idx_pinned        ON clipboard_history(is_pinned);
CREATE INDEX IF NOT EXISTS idx_content_hash  ON clipboard_history(content_hash);
"""

# Migrations: each entry is tried in order; OperationalError means already applied, skip.
# IMPORTANT: column additions must come before any index that references them.
MIGRATIONS = [
    "ALTER TABLE clipboard_history ADD COLUMN content_type TEXT DEFAULT 'text'",
    "ALTER TABLE clipboard_history ADD COLUMN file_path TEXT",
    # Index for content_type — added after the column migration so it works on old DBs too
    "CREATE INDEX IF NOT EXISTS idx_content_type ON clipboard_history(content_type)",
]

PREVIEW_LENGTH = 100  # chars shown in preview


# ---------------------------------------------------------------------------
# Data class returned by queries
# ---------------------------------------------------------------------------

@dataclass
class Clip:
    id: int
    content: str               # full content or preview depending on full_content flag
    content_preview: str
    category: str
    source_app: Optional[str]
    char_count: int
    is_pinned: bool
    is_sensitive: bool
    content_type: str          # 'text' or 'image'
    file_path: Optional[str]   # path to image file, None for text clips
    created_at: str

    @property
    def is_image(self) -> bool:
        return self.content_type == "image"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "content": self.content,
            "content_preview": self.content_preview,
            "category": self.category,
            "source_app": self.source_app,
            "char_count": self.char_count,
            "is_pinned": self.is_pinned,
            "is_sensitive": self.is_sensitive,
            "content_type": self.content_type,
            "file_path": self.file_path,
            "created_at": self.created_at,
        }


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------

def _db_path() -> Path:
    return config.db_path_resolved


def _ensure_db() -> None:
    """Create the DB file and schema if they don't exist. Run migrations on existing DBs."""
    _db_path().parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(_db_path()) as conn:
        conn.executescript(SCHEMA)
        conn.commit()
        _run_migrations(conn)


def _run_migrations(conn: sqlite3.Connection) -> None:
    """Apply any schema migrations that haven't been applied yet."""
    for migration in MIGRATIONS:
        try:
            conn.execute(migration)
            conn.commit()
        except sqlite3.OperationalError:
            # Column already exists — migration already applied, skip
            pass


@contextmanager
def _conn() -> Generator[sqlite3.Connection, None, None]:
    """Context manager yielding a connected, row_factory-enabled connection."""
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _make_preview(content: str) -> str:
    return content[:PREVIEW_LENGTH] + ("…" if len(content) > PREVIEW_LENGTH else "")


def _row_to_clip(row: sqlite3.Row, full_content: bool = False) -> Clip:
    content = row["content"] if full_content else row["content_preview"]
    return Clip(
        id=row["id"],
        content=content,
        content_preview=row["content_preview"],
        category=row["category"],
        source_app=row["source_app"],
        char_count=row["char_count"],
        is_pinned=bool(row["is_pinned"]),
        is_sensitive=bool(row["is_sensitive"]),
        content_type=row["content_type"] or "text",
        file_path=row["file_path"],
        created_at=row["created_at"],
    )


def _delete_image_files(file_paths: list[str]) -> None:
    """Delete image files from disk. Called after DB rows are removed."""
    from .image_handler import delete_image_file
    for path in file_paths:
        if path:
            delete_image_file(path)


# ---------------------------------------------------------------------------
# Write operations
# ---------------------------------------------------------------------------

def insert_clip(
    content: str,
    category: str = "text",
    source_app: Optional[str] = None,
    is_sensitive: bool = False,
    content_type: str = "text",
    file_path: Optional[str] = None,
    content_hash: Optional[str] = None,
) -> Optional[int]:
    """
    Insert a new clip. Returns the new row id, or None if it's a duplicate
    of the most recent clip.

    For image clips, pass content_type='image', file_path=<path>,
    content=<preview string>, and content_hash=<hash of image bytes>.
    """
    if content_hash is None:
        content_hash = _hash(content)

    with _conn() as conn:
        # Dedup: check if most recent clip has the same hash
        row = conn.execute(
            "SELECT content_hash FROM clipboard_history ORDER BY created_at DESC LIMIT 1"
        ).fetchone()

        if row and row["content_hash"] == content_hash:
            return None  # duplicate of last clip, skip

        cursor = conn.execute(
            """
            INSERT INTO clipboard_history
                (content, content_preview, content_hash, category, source_app,
                 char_count, is_sensitive, content_type, file_path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                content,
                _make_preview(content),
                content_hash,
                category,
                source_app,
                len(content),
                int(is_sensitive),
                content_type,
                file_path,
            ),
        )

        new_id = cursor.lastrowid

    _prune_if_needed()
    return new_id


def pin_clip(clip_id: int) -> bool:
    with _conn() as conn:
        cursor = conn.execute(
            "UPDATE clipboard_history SET is_pinned = 1 WHERE id = ?", (clip_id,)
        )
        return cursor.rowcount > 0


def unpin_clip(clip_id: int) -> bool:
    with _conn() as conn:
        cursor = conn.execute(
            "UPDATE clipboard_history SET is_pinned = 0 WHERE id = ?", (clip_id,)
        )
        return cursor.rowcount > 0


def delete_clip(clip_id: int) -> bool:
    """Delete a clip by id. Also removes image file from disk if applicable."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT file_path FROM clipboard_history WHERE id = ?", (clip_id,)
        ).fetchone()

        cursor = conn.execute(
            "DELETE FROM clipboard_history WHERE id = ?", (clip_id,)
        )
        if cursor.rowcount > 0 and row and row["file_path"]:
            _delete_image_files([row["file_path"]])
        return cursor.rowcount > 0


def clear_history(keep_pinned: bool = True) -> int:
    """Delete all clips. Also removes image files from disk."""
    with _conn() as conn:
        # Collect image file paths before deleting
        if keep_pinned:
            file_rows = conn.execute(
                "SELECT file_path FROM clipboard_history WHERE is_pinned = 0 AND file_path IS NOT NULL"
            ).fetchall()
            cursor = conn.execute(
                "DELETE FROM clipboard_history WHERE is_pinned = 0"
            )
        else:
            file_rows = conn.execute(
                "SELECT file_path FROM clipboard_history WHERE file_path IS NOT NULL"
            ).fetchall()
            cursor = conn.execute("DELETE FROM clipboard_history")

        _delete_image_files([r["file_path"] for r in file_rows])
        return cursor.rowcount


# ---------------------------------------------------------------------------
# Read operations
# ---------------------------------------------------------------------------

def get_recent(
    count: int = 10,
    category: Optional[str] = None,
    full_content: bool = False,
) -> list[Clip]:
    with _conn() as conn:
        if category:
            rows = conn.execute(
                """
                SELECT * FROM clipboard_history
                WHERE category = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (category, count),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM clipboard_history
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (count,),
            ).fetchall()

    return [_row_to_clip(r, full_content=full_content) for r in rows]


def search(
    query: str,
    category: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 20,
    full_content: bool = False,
) -> list[Clip]:
    conditions = ["(content LIKE ? OR content_preview LIKE ?)"]
    params: list = [f"%{query}%", f"%{query}%"]

    if category:
        conditions.append("category = ?")
        params.append(category)
    if date_from:
        conditions.append("created_at >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("created_at <= ?")
        params.append(date_to)

    where = " AND ".join(conditions)
    params.append(limit)

    with _conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM clipboard_history WHERE {where} ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()

    return [_row_to_clip(r, full_content=full_content) for r in rows]


def get_by_id(clip_id: int, full_content: bool = True) -> Optional[Clip]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM clipboard_history WHERE id = ?", (clip_id,)
        ).fetchone()

    if not row:
        return None
    return _row_to_clip(row, full_content=full_content)


def get_stats() -> dict:
    with _conn() as conn:
        total = conn.execute(
            "SELECT COUNT(*) as n FROM clipboard_history"
        ).fetchone()["n"]

        today = conn.execute(
            "SELECT COUNT(*) as n FROM clipboard_history WHERE DATE(created_at) = DATE('now')"
        ).fetchone()["n"]

        top_categories = conn.execute(
            """
            SELECT category, COUNT(*) as n
            FROM clipboard_history
            GROUP BY category
            ORDER BY n DESC
            LIMIT 5
            """
        ).fetchall()

        top_apps = conn.execute(
            """
            SELECT source_app, COUNT(*) as n
            FROM clipboard_history
            WHERE source_app IS NOT NULL
            GROUP BY source_app
            ORDER BY n DESC
            LIMIT 5
            """
        ).fetchall()

        image_count = conn.execute(
            "SELECT COUNT(*) as n FROM clipboard_history WHERE content_type = 'image'"
        ).fetchone()["n"]

    db_size_bytes = _db_path().stat().st_size if _db_path().exists() else 0

    return {
        "total_clips": total,
        "clips_today": today,
        "image_clips": image_count,
        "top_categories": [{"category": r["category"], "count": r["n"]} for r in top_categories],
        "top_source_apps": [{"app": r["source_app"], "count": r["n"]} for r in top_apps],
        "storage_size_bytes": db_size_bytes,
        "storage_size_kb": round(db_size_bytes / 1024, 1),
    }


# ---------------------------------------------------------------------------
# Pruning
# ---------------------------------------------------------------------------

def _prune_if_needed() -> None:
    if not config.auto_prune:
        return

    with _conn() as conn:
        cutoff = (datetime.now() - timedelta(days=config.prune_after_days)).isoformat()

        # Collect image paths before deleting
        old_images = conn.execute(
            "SELECT file_path FROM clipboard_history WHERE created_at < ? AND is_pinned = 0 AND file_path IS NOT NULL",
            (cutoff,),
        ).fetchall()

        conn.execute(
            "DELETE FROM clipboard_history WHERE created_at < ? AND is_pinned = 0",
            (cutoff,),
        )

        # Prune by count
        excess_images = conn.execute(
            """
            SELECT file_path FROM clipboard_history
            WHERE is_pinned = 0 AND file_path IS NOT NULL
            AND id NOT IN (
                SELECT id FROM clipboard_history
                WHERE is_pinned = 0
                ORDER BY created_at DESC
                LIMIT ?
            )
            """,
            (config.max_history_size,),
        ).fetchall()

        conn.execute(
            """
            DELETE FROM clipboard_history
            WHERE is_pinned = 0
            AND id NOT IN (
                SELECT id FROM clipboard_history
                WHERE is_pinned = 0
                ORDER BY created_at DESC
                LIMIT ?
            )
            """,
            (config.max_history_size,),
        )

        _delete_image_files(
            [r["file_path"] for r in old_images] +
            [r["file_path"] for r in excess_images]
        )


def prune_old() -> int:
    with _conn() as conn:
        cutoff = (datetime.now() - timedelta(days=config.prune_after_days)).isoformat()
        file_rows = conn.execute(
            "SELECT file_path FROM clipboard_history WHERE created_at < ? AND is_pinned = 0 AND file_path IS NOT NULL",
            (cutoff,),
        ).fetchall()
        cursor = conn.execute(
            "DELETE FROM clipboard_history WHERE created_at < ? AND is_pinned = 0",
            (cutoff,),
        )
        _delete_image_files([r["file_path"] for r in file_rows])
        return cursor.rowcount


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------

_ensure_db()
