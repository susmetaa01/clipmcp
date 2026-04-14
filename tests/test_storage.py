"""Tests for storage.py — SQLite CRUD operations."""

import sys
import os
import tempfile
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../src"))


# Point the DB to a temp file before importing storage
@pytest.fixture(autouse=True)
def temp_db(monkeypatch, tmp_path):
    """Redirect the database to a temp file for each test."""
    import clipmcp.config as cfg_module
    import clipmcp.storage as storage_module

    tmp_db = tmp_path / "test_history.db"

    # Patch config to use temp paths
    monkeypatch.setattr(cfg_module.config, "db_path", str(tmp_db))

    # Re-run schema creation against the new path
    storage_module._ensure_db()

    yield

    # Cleanup is handled by tmp_path fixture


import clipmcp.storage as storage


class TestInsert:
    def test_basic_insert(self):
        clip_id = storage.insert_clip("Hello world", category="text")
        assert clip_id is not None
        assert clip_id > 0

    def test_dedup_consecutive(self):
        storage.insert_clip("duplicate content")
        result = storage.insert_clip("duplicate content")
        assert result is None  # second insert should be skipped

    def test_different_content_not_deduped(self):
        id1 = storage.insert_clip("first clip")
        id2 = storage.insert_clip("second clip")
        assert id1 is not None
        assert id2 is not None
        assert id1 != id2

    def test_same_content_after_different_is_inserted(self):
        storage.insert_clip("clip A")
        storage.insert_clip("clip B")
        id3 = storage.insert_clip("clip A")  # not consecutive — should be inserted
        assert id3 is not None


class TestGetRecent:
    def test_returns_most_recent_first(self):
        storage.insert_clip("oldest")
        storage.insert_clip("middle")
        storage.insert_clip("newest")

        clips = storage.get_recent(count=3)
        assert clips[0].content_preview.startswith("newest")
        assert clips[-1].content_preview.startswith("oldest")

    def test_count_limit(self):
        for i in range(10):
            storage.insert_clip(f"clip number {i}")
        clips = storage.get_recent(count=5)
        assert len(clips) == 5

    def test_category_filter(self):
        storage.insert_clip("https://example.com", category="url")
        storage.insert_clip("plain text", category="text")

        clips = storage.get_recent(count=10, category="url")
        assert all(c.category == "url" for c in clips)

    def test_full_content_flag(self):
        long_content = "x" * 200
        storage.insert_clip(long_content)

        preview_clip = storage.get_recent(count=1, full_content=False)[0]
        full_clip = storage.get_recent(count=1, full_content=True)[0]

        assert len(preview_clip.content) <= 101  # 100 chars + ellipsis
        assert len(full_clip.content) == 200


class TestSearch:
    def test_finds_matching_content(self):
        storage.insert_clip("the quick brown fox")
        storage.insert_clip("something else entirely")

        results = storage.search("quick brown")
        assert len(results) == 1
        assert "quick brown" in results[0].content_preview

    def test_no_results_for_missing_query(self):
        storage.insert_clip("hello world")
        results = storage.search("zzznomatch")
        assert len(results) == 0

    def test_category_filter_in_search(self):
        storage.insert_clip("https://example.com", category="url")
        storage.insert_clip("example sentence", category="text")

        results = storage.search("example", category="url")
        assert all(r.category == "url" for r in results)


class TestPinUnpin:
    def test_pin_clip(self):
        clip_id = storage.insert_clip("important clip")
        success = storage.pin_clip(clip_id)
        assert success

        clip = storage.get_by_id(clip_id)
        assert clip.is_pinned

    def test_unpin_clip(self):
        clip_id = storage.insert_clip("was pinned")
        storage.pin_clip(clip_id)
        storage.unpin_clip(clip_id)

        clip = storage.get_by_id(clip_id)
        assert not clip.is_pinned

    def test_pin_nonexistent(self):
        success = storage.pin_clip(999999)
        assert not success


class TestDelete:
    def test_delete_clip(self):
        clip_id = storage.insert_clip("to be deleted")
        success = storage.delete_clip(clip_id)
        assert success
        assert storage.get_by_id(clip_id) is None

    def test_delete_nonexistent(self):
        success = storage.delete_clip(999999)
        assert not success


class TestClearHistory:
    def test_clear_all(self):
        storage.insert_clip("clip 1")
        storage.insert_clip("clip 2")
        storage.insert_clip("clip 3")

        deleted = storage.clear_history(keep_pinned=False)
        assert deleted == 3
        assert storage.get_recent(count=10) == []

    def test_clear_keeps_pinned(self):
        id1 = storage.insert_clip("unpinned")
        id2 = storage.insert_clip("pinned clip")
        storage.pin_clip(id2)

        storage.clear_history(keep_pinned=True)

        remaining = storage.get_recent(count=10)
        assert len(remaining) == 1
        assert remaining[0].is_pinned


class TestStats:
    def test_stats_structure(self):
        storage.insert_clip("hello", category="text")
        stats = storage.get_stats()

        assert "total_clips" in stats
        assert "clips_today" in stats
        assert "top_categories" in stats
        assert "top_source_apps" in stats
        assert "storage_size_bytes" in stats

    def test_total_count_increments(self):
        before = storage.get_stats()["total_clips"]
        storage.insert_clip("new clip")
        after = storage.get_stats()["total_clips"]
        assert after == before + 1
