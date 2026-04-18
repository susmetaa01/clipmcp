"""
storage.py — All SQLite read/write operations for ClipMCP.

Design: Repository pattern.

``ClipRepository`` encapsulates every DB operation.  It holds no persistent
state other than the DB path (resolved from config at call time so that
test fixtures can monkeypatch config.db_path after import).

A module-level singleton (``_default_repo``) is created at import time.
Module-level wrapper functions delegate to it so existing call sites and
tests continue to work without changes.

This is the only module that touches the database.
All other modules call functions here — never raw SQL elsewhere.
"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Generator, Optional

from typing import Any

from .config import config
from .models import Clip, ContentCategory, ContentType

# Re-export Clip so ``storage.Clip`` still works for callers that import it
# from this module.
__all__ = [
    "Clip",
    "ContentCategory",
    "ContentType",
    "ClipRepository",
    "insert_clip",
    "get_recent",
    "search",
    "get_by_id",
    "pin_clip",
    "unpin_clip",
    "delete_clip",
    "clear_history",
    "get_stats",
    "store_embedding",
    "get_clips_without_embeddings",
    "semantic_search_by_vector",
    "prune_old",
]


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
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

# Each migration is tried in order.
# OperationalError means already applied → skip.
# IMPORTANT: column additions must precede any index that references them.
_MIGRATIONS = [
    "ALTER TABLE clipboard_history ADD COLUMN content_type TEXT DEFAULT 'text'",
    "ALTER TABLE clipboard_history ADD COLUMN file_path TEXT",
    "CREATE INDEX IF NOT EXISTS idx_content_type ON clipboard_history(content_type)",
    "ALTER TABLE clipboard_history ADD COLUMN embedding BLOB",
]

_PREVIEW_LENGTH = 100  # chars stored in content_preview


# ---------------------------------------------------------------------------
# Pure helpers (stateless — no DB access)
# ---------------------------------------------------------------------------

def _hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _make_preview(content: str) -> str:
    return content[:_PREVIEW_LENGTH] + ("…" if len(content) > _PREVIEW_LENGTH else "")


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
        content_type=row["content_type"] or ContentType.TEXT,
        file_path=row["file_path"],
        created_at=row["created_at"],
    )


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------

class ClipRepository:
    """
    All SQLite CRUD operations for clipboard history.

    The DB path is resolved from ``config.db_path_resolved`` at call time
    (not at construction time) so test fixtures can monkeypatch
    ``config.db_path`` after the module has been imported.
    """

    # ------------------------------------------------------------------
    # Infrastructure
    # ------------------------------------------------------------------

    def _db_path(self) -> Path:
        """Always reads from the live config — respects test monkeypatching."""
        return config.db_path_resolved

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        """Yield a WAL-mode connection with row_factory, auto-commit/rollback."""
        conn = sqlite3.connect(self._db_path())
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

    def _ensure_db(self) -> None:
        """Create schema and run migrations. Safe to call multiple times."""
        self._db_path().parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._db_path()) as conn:
            conn.executescript(_SCHEMA)
            conn.commit()
            self._run_migrations(conn)

    def _run_migrations(self, conn: sqlite3.Connection) -> None:
        for migration in _MIGRATIONS:
            try:
                conn.execute(migration)
                conn.commit()
            except sqlite3.OperationalError:
                pass  # already applied

    def _delete_image_files(self, file_paths: list[str]) -> None:
        from .image_handler import delete_image_file
        for path in file_paths:
            if path:
                delete_image_file(path)

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def insert_clip(
        self,
        content: str,
        category: str = ContentCategory.TEXT,
        source_app: Optional[str] = None,
        is_sensitive: bool = False,
        content_type: str = ContentType.TEXT,
        file_path: Optional[str] = None,
        content_hash: Optional[str] = None,
        stripped_text: Optional[str] = None,
    ) -> Optional[int]:
        """
        Persist a new clipboard entry.

        Returns the new row id, or None if the content is an exact duplicate
        of the most recent entry.

        For HTML clips: pass ``content_type=ContentType.HTML``,
        ``content=<raw HTML>``, and ``stripped_text=<plain text>``.  The
        stripped text is stored as ``content_preview`` so tools always see
        readable text rather than raw markup.

        For image clips: pass ``content_type=ContentType.IMAGE``,
        ``file_path=<path>``, and ``content_hash=<hash of PNG bytes>``.
        """
        if content_hash is None:
            content_hash = _hash(content)

        preview = _make_preview(stripped_text) if stripped_text is not None else _make_preview(content)

        with self._conn() as conn:
            row = conn.execute(
                "SELECT content_hash FROM clipboard_history ORDER BY created_at DESC LIMIT 1"
            ).fetchone()

            if row and row["content_hash"] == content_hash:
                return None  # consecutive duplicate — skip

            cursor = conn.execute(
                """
                INSERT INTO clipboard_history
                    (content, content_preview, content_hash, category, source_app,
                     char_count, is_sensitive, content_type, file_path)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    content,
                    preview,
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

        self._prune_if_needed()
        return new_id

    def pin_clip(self, clip_id: int) -> bool:
        with self._conn() as conn:
            cursor = conn.execute(
                "UPDATE clipboard_history SET is_pinned = 1 WHERE id = ?", (clip_id,)
            )
            return cursor.rowcount > 0

    def unpin_clip(self, clip_id: int) -> bool:
        with self._conn() as conn:
            cursor = conn.execute(
                "UPDATE clipboard_history SET is_pinned = 0 WHERE id = ?", (clip_id,)
            )
            return cursor.rowcount > 0

    def delete_clip(self, clip_id: int) -> bool:
        """Delete a clip by id. Also removes the image file from disk."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT file_path FROM clipboard_history WHERE id = ?", (clip_id,)
            ).fetchone()
            cursor = conn.execute(
                "DELETE FROM clipboard_history WHERE id = ?", (clip_id,)
            )
            if cursor.rowcount > 0 and row and row["file_path"]:
                self._delete_image_files([row["file_path"]])
            return cursor.rowcount > 0

    def clear_history(self, keep_pinned: bool = True) -> int:
        """Delete clipboard history. Returns the number of deleted rows."""
        with self._conn() as conn:
            if keep_pinned:
                file_rows = conn.execute(
                    "SELECT file_path FROM clipboard_history "
                    "WHERE is_pinned = 0 AND file_path IS NOT NULL"
                ).fetchall()
                cursor = conn.execute(
                    "DELETE FROM clipboard_history WHERE is_pinned = 0"
                )
            else:
                file_rows = conn.execute(
                    "SELECT file_path FROM clipboard_history WHERE file_path IS NOT NULL"
                ).fetchall()
                cursor = conn.execute("DELETE FROM clipboard_history")

            self._delete_image_files([r["file_path"] for r in file_rows])
            return cursor.rowcount

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def get_recent(
        self,
        count: int = 10,
        category: Optional[str] = None,
        full_content: bool = False,
    ) -> list[Clip]:
        with self._conn() as conn:
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
        self,
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

        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM clipboard_history WHERE {where} ORDER BY created_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [_row_to_clip(r, full_content=full_content) for r in rows]

    def get_by_id(self, clip_id: int, full_content: bool = True) -> Optional[Clip]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM clipboard_history WHERE id = ?", (clip_id,)
            ).fetchone()
        if not row:
            return None
        return _row_to_clip(row, full_content=full_content)

    def get_stats(self) -> dict:
        with self._conn() as conn:
            total = conn.execute(
                "SELECT COUNT(*) as n FROM clipboard_history"
            ).fetchone()["n"]
            today = conn.execute(
                "SELECT COUNT(*) as n FROM clipboard_history WHERE DATE(created_at) = DATE('now')"
            ).fetchone()["n"]
            top_categories = conn.execute(
                """
                SELECT category, COUNT(*) as n FROM clipboard_history
                GROUP BY category ORDER BY n DESC LIMIT 5
                """
            ).fetchall()
            top_apps = conn.execute(
                """
                SELECT source_app, COUNT(*) as n FROM clipboard_history
                WHERE source_app IS NOT NULL
                GROUP BY source_app ORDER BY n DESC LIMIT 5
                """
            ).fetchall()
            image_count = conn.execute(
                "SELECT COUNT(*) as n FROM clipboard_history WHERE content_type = 'image'"
            ).fetchone()["n"]

        db_path = self._db_path()
        db_size = db_path.stat().st_size if db_path.exists() else 0
        return {
            "total_clips":        total,
            "clips_today":        today,
            "image_clips":        image_count,
            "top_categories":     [{"category": r["category"], "count": r["n"]} for r in top_categories],
            "top_source_apps":    [{"app": r["source_app"], "count": r["n"]} for r in top_apps],
            "storage_size_bytes": db_size,
            "storage_size_kb":    round(db_size / 1024, 1),
        }

    # ------------------------------------------------------------------
    # Semantic search (v2.5)
    # ------------------------------------------------------------------

    def store_embedding(self, clip_id: int, embedding: Any) -> None:
        """Persist an embedding vector for a clip as a BLOB."""
        from .embeddings import to_blob
        blob = to_blob(embedding)
        with self._conn() as conn:
            conn.execute(
                "UPDATE clipboard_history SET embedding = ? WHERE id = ?",
                (blob, clip_id),
            )

    def get_clips_without_embeddings(
        self, limit: int = 500
    ) -> list[tuple[int, str, str, str]]:
        """
        Return ``(id, content, content_type, content_preview)`` tuples for
        clips that are embeddable (not images) but don't yet have an embedding.
        Used for backfill on first semantic_search call.
        """
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT id, content, content_type, content_preview
                FROM clipboard_history
                WHERE embedding IS NULL
                  AND content_type != 'image'
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [(r["id"], r["content"], r["content_type"], r["content_preview"]) for r in rows]

    def semantic_search_by_vector(
        self,
        query_vec: Any,
        limit: int = 10,
        category: Optional[str] = None,
        threshold: float = 0.3,
        full_content: bool = False,
    ) -> list[tuple[Clip, float]]:
        """
        Rank all embedded clips by cosine similarity to *query_vec*.
        Returns ``(Clip, score)`` pairs sorted by descending similarity,
        filtered to scores >= *threshold*.
        """
        import numpy as np
        from .embeddings import from_blob, rank_by_similarity

        conditions = ["embedding IS NOT NULL"]
        params: list = []
        if category:
            conditions.append("category = ?")
            params.append(category)

        where = " AND ".join(conditions)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM clipboard_history WHERE {where} ORDER BY created_at DESC",
                params,
            ).fetchall()

        if not rows:
            return []

        clips = [_row_to_clip(r, full_content=full_content) for r in rows]
        vecs  = np.stack([from_blob(r["embedding"]) for r in rows])
        scores = rank_by_similarity(query_vec, vecs, threshold=threshold)

        paired = [
            (clip, float(score))
            for clip, score in zip(clips, scores)
            if score >= threshold
        ]
        paired.sort(key=lambda x: x[1], reverse=True)
        return paired[:limit]

    # ------------------------------------------------------------------
    # Pruning
    # ------------------------------------------------------------------

    def _prune_if_needed(self) -> None:
        if not config.auto_prune:
            return

        with self._conn() as conn:
            cutoff = (datetime.now() - timedelta(days=config.prune_after_days)).isoformat()

            old_images = conn.execute(
                "SELECT file_path FROM clipboard_history "
                "WHERE created_at < ? AND is_pinned = 0 AND file_path IS NOT NULL",
                (cutoff,),
            ).fetchall()
            conn.execute(
                "DELETE FROM clipboard_history WHERE created_at < ? AND is_pinned = 0",
                (cutoff,),
            )

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

            self._delete_image_files(
                [r["file_path"] for r in old_images] +
                [r["file_path"] for r in excess_images]
            )

    def prune_old(self) -> int:
        with self._conn() as conn:
            cutoff = (datetime.now() - timedelta(days=config.prune_after_days)).isoformat()
            file_rows = conn.execute(
                "SELECT file_path FROM clipboard_history "
                "WHERE created_at < ? AND is_pinned = 0 AND file_path IS NOT NULL",
                (cutoff,),
            ).fetchall()
            cursor = conn.execute(
                "DELETE FROM clipboard_history WHERE created_at < ? AND is_pinned = 0",
                (cutoff,),
            )
            self._delete_image_files([r["file_path"] for r in file_rows])
            return cursor.rowcount


# ---------------------------------------------------------------------------
# Module-level singleton + backward-compatible function API
# ---------------------------------------------------------------------------
#
# All functions delegate to ``_default_repo`` so callers that imported the
# old procedural API (including all existing tests) continue to work.
#
# Test fixtures patch ``config.db_path`` after import and then call
# ``_ensure_db()`` directly — both still work because ClipRepository
# resolves the DB path from config at call time, not construction time.

_default_repo = ClipRepository()


def _ensure_db() -> None:
    """Create / migrate the database.  Called by tests after patching config."""
    _default_repo._ensure_db()


def insert_clip(
    content: str,
    category: str = ContentCategory.TEXT,
    source_app: Optional[str] = None,
    is_sensitive: bool = False,
    content_type: str = ContentType.TEXT,
    file_path: Optional[str] = None,
    content_hash: Optional[str] = None,
    stripped_text: Optional[str] = None,
) -> Optional[int]:
    return _default_repo.insert_clip(
        content=content,
        category=category,
        source_app=source_app,
        is_sensitive=is_sensitive,
        content_type=content_type,
        file_path=file_path,
        content_hash=content_hash,
        stripped_text=stripped_text,
    )


def pin_clip(clip_id: int) -> bool:
    return _default_repo.pin_clip(clip_id)


def unpin_clip(clip_id: int) -> bool:
    return _default_repo.unpin_clip(clip_id)


def delete_clip(clip_id: int) -> bool:
    return _default_repo.delete_clip(clip_id)


def clear_history(keep_pinned: bool = True) -> int:
    return _default_repo.clear_history(keep_pinned=keep_pinned)


def get_recent(
    count: int = 10,
    category: Optional[str] = None,
    full_content: bool = False,
) -> list[Clip]:
    return _default_repo.get_recent(count=count, category=category, full_content=full_content)


def search(
    query: str,
    category: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 20,
    full_content: bool = False,
) -> list[Clip]:
    return _default_repo.search(
        query=query,
        category=category,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        full_content=full_content,
    )


def get_by_id(clip_id: int, full_content: bool = True) -> Optional[Clip]:
    return _default_repo.get_by_id(clip_id, full_content=full_content)


def get_stats() -> dict:
    return _default_repo.get_stats()


def store_embedding(clip_id: int, embedding: Any) -> None:
    return _default_repo.store_embedding(clip_id, embedding)


def get_clips_without_embeddings(limit: int = 500) -> list[tuple[int, str, str, str]]:
    return _default_repo.get_clips_without_embeddings(limit=limit)


def semantic_search_by_vector(
    query_vec: Any,
    limit: int = 10,
    category: Optional[str] = None,
    threshold: float = 0.3,
    full_content: bool = False,
) -> list[tuple[Clip, float]]:
    return _default_repo.semantic_search_by_vector(
        query_vec=query_vec,
        limit=limit,
        category=category,
        threshold=threshold,
        full_content=full_content,
    )


def prune_old() -> int:
    return _default_repo.prune_old()


# ---------------------------------------------------------------------------
# Init — create schema on first import
# ---------------------------------------------------------------------------

_ensure_db()
