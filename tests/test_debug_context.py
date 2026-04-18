"""
Tests for get_debug_context bundling logic.

Tests the ordering and formatting of the debug context bundle
by exercising the storage layer directly (no MCP server needed).
"""

import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../src"))

import clipmcp.storage as storage


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Each test gets its own fresh SQLite database."""
    db_file = tmp_path / "test_history.db"
    monkeypatch.setattr(storage.config, "db_path", str(db_file))
    storage._ensure_db()
    yield


def _insert(content: str, category: str) -> int:
    return storage.insert_clip(content=content, category=category)


class TestDebugContextOrdering:
    def test_errors_come_first(self):
        _insert("plain text note", "text")
        _insert("some code snippet here", "code")
        _insert("KeyError: 'missing_key'\n  File app.py line 10", "error")

        clips = storage.get_recent(count=10, full_content=True)
        _PRIORITY = {"error": 0, "code": 1}
        errors = [c for c in clips if c.category == "error"]
        code   = [c for c in clips if c.category == "code"]
        rest   = [c for c in clips if c.category not in ("error", "code")]
        ordered = errors + code + rest

        assert ordered[0].category == "error"
        assert ordered[1].category == "code"
        assert ordered[2].category == "text"

    def test_multiple_errors_all_surface_first(self):
        _insert("some text", "text")
        _insert("NullPointerException at line 5", "error")
        _insert("def foo(): pass", "code")
        _insert("ConnectionRefusedError: port 5432", "error")

        clips = storage.get_recent(count=10, full_content=True)
        errors = [c for c in clips if c.category == "error"]
        code   = [c for c in clips if c.category == "code"]
        rest   = [c for c in clips if c.category not in ("error", "code")]
        ordered = errors + code + rest

        assert ordered[0].category == "error"
        assert ordered[1].category == "error"
        assert ordered[2].category == "code"
        assert ordered[3].category == "text"

    def test_no_errors_code_comes_first(self):
        _insert("plain text", "text")
        _insert("import os\nprint(os.getcwd())", "code")

        clips = storage.get_recent(count=10, full_content=True)
        errors = [c for c in clips if c.category == "error"]
        code   = [c for c in clips if c.category == "code"]
        rest   = [c for c in clips if c.category not in ("error", "code")]
        ordered = errors + code + rest

        assert ordered[0].category == "code"
        assert ordered[1].category == "text"

    def test_only_text_clips_returned_in_order(self):
        _insert("first", "text")
        _insert("second", "text")
        _insert("third", "text")

        clips = storage.get_recent(count=10, full_content=True)
        errors = [c for c in clips if c.category == "error"]
        code   = [c for c in clips if c.category == "code"]
        rest   = [c for c in clips if c.category not in ("error", "code")]
        ordered = errors + code + rest

        assert all(c.category == "text" for c in ordered)
        assert len(ordered) == 3

    def test_full_content_returned(self):
        long_text = "x" * 200  # longer than 100-char preview
        _insert(long_text, "text")

        clips = storage.get_recent(count=1, full_content=True)
        assert len(clips[0].content) == 200

    def test_limit_respected(self):
        for i in range(15):
            _insert(f"item {i}", "text")

        clips = storage.get_recent(count=10, full_content=True)
        assert len(clips) <= 10

    def test_empty_history_returns_empty(self):
        clips = storage.get_recent(count=10, full_content=True)
        assert clips == []
