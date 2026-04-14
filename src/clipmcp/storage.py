"""
storage.py — All SQLite read/write operations for ClipMCP.

This is the only module that touches the database.
All other modules call functions here — never raw SQL elsewhere.
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
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_created_at   ON clipboard_history(created_at);
CREATE INDEX IF NOT EXISTS idx_category     ON clipboard_history(category);
CREATE INDEX IF NOT EXISTS idx_pinned       ON clipboard_history(is_pinned);
CREATE INDEX IF NOT EXISTS idx_content_hash ON clipboard_history(content_hash);
"""

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
    created_at: str

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
            "created_at": self.created_at,
        }


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------

def _db_path() -> Path:
    return config.db_path_resolved


def _ensure_db() -> None:
    """Create the DB file and schema if they don't exist."""
    _db_path().parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(_db_path()) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


@contextmanager
def _conn() -> Generator[sqlite3.Connection, None, None]:
    """Context manager yielding a connected, row_factory-enabled connection."""
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # better concurrent read performance
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
        created_at=row["created_at"],
    )


# ---------------------------------------------------------------------------
# Write operations
# ---------------------------------------------------------------------------

def insert_clip(
    content: str,
    category: str = "text",
    source_app: Optional[str] = None,
    is_sensitive: bool = False,
) -> Optional[int]:
    """
    Insert a new clip. Returns the new row id, or None if it's a duplicate
    of the most recent clip.
    """
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
                (content, content_preview, content_hash, category, source_app, char_count, is_sensitive)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                content,
                _make_preview(content),
                content_hash,
                category,
                source_app,
                len(content),
                int(is_sensitive),
            ),
        )

        new_id = cursor.lastrowid

    # Prune if we've exceeded max history size
    _prune_if_needed()

    return new_id


def pin_clip(clip_id: int) -> bool:
    """Pin a clip so it won't be pruned. Returns True if found."""
    with _conn() as conn:
        cursor = conn.execute(
            "UPDATE clipboard_history SET is_pinned = 1 WHERE id = ?", (clip_id,)
        )
        return cursor.rowcount > 0


def unpin_clip(clip_id: int) -> bool:
    """Unpin a clip. Returns True if found."""
    with _conn() as conn:
        cursor = conn.execute(
            "UPDATE clipboard_history SET is_pinned = 0 WHERE id = ?", (clip_id,)
        )
        return cursor.rowcount > 0


def delete_clip(clip_id: int) -> bool:
    """Delete a clip by id. Returns True if found."""
    with _conn() as conn:
        cursor = conn.execute(
            "DELETE FROM clipboard_history WHERE id = ?", (clip_id,)
        )
        return cursor.rowcount > 0


def clear_history(keep_pinned: bool = True) -> int:
    """
    Delete all clips (optionally keeping pinned ones).
    Returns number of clips deleted.
    """
    with _conn() as conn:
        if keep_pinned:
            cursor = conn.execute(
                "DELETE FROM clipboard_history WHERE is_pinned = 0"
            )
        else:
            cursor = conn.execute("DELETE FROM clipboard_history")
        return cursor.rowcount


# ---------------------------------------------------------------------------
# Read operations
# ---------------------------------------------------------------------------

def get_recent(
    count: int = 10,
    category: Optional[str] = None,
    full_content: bool = False,
) -> list[Clip]:
    """Return the N most recent clips, optionally filtered by category."""
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
    """
    Search clips by content (LIKE match on content and preview).
    Optionally filter by category and date range.
    """
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
    """Internal helper — fetch a single clip by id. Not exposed as MCP tool."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM clipboard_history WHERE id = ?", (clip_id,)
        ).fetchone()

    if not row:
        return None
    return _row_to_clip(row, full_content=full_content)


def get_stats() -> dict:
    """Return usage statistics."""
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

    db_size_bytes = _db_path().stat().st_size if _db_path().exists() else 0

    return {
        "total_clips": total,
        "clips_today": today,
        "top_categories": [{"category": r["category"], "count": r["n"]} for r in top_categories],
        "top_source_apps": [{"app": r["source_app"], "count": r["n"]} for r in top_apps],
        "storage_size_bytes": db_size_bytes,
        "storage_size_kb": round(db_size_bytes / 1024, 1),
    }


# ---------------------------------------------------------------------------
# Pruning
# ---------------------------------------------------------------------------

def _prune_if_needed() -> None:
    """Prune old clips if auto_prune is enabled and size exceeds limit."""
    if not config.auto_prune:
        return

    with _conn() as conn:
        # Prune by age
        cutoff = (datetime.now() - timedelta(days=config.prune_after_days)).isoformat()
        conn.execute(
            "DELETE FROM clipboard_history WHERE created_at < ? AND is_pinned = 0",
            (cutoff,),
        )

        # Prune by count — keep only the most recent N (excluding pinned)
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


def prune_old() -> int:
    """Manually trigger pruning. Returns number of clips removed."""
    with _conn() as conn:
        cutoff = (datetime.now() - timedelta(days=config.prune_after_days)).isoformat()
        cursor = conn.execute(
            "DELETE FROM clipboard_history WHERE created_at < ? AND is_pinned = 0",
            (cutoff,),
        )
        return cursor.rowcount


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------

_ensure_db()
