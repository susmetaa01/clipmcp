"""Tests for categorizer.py — content classification."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../src"))

from clipmcp.categorizer import categorize


class TestURL:
    def test_https_url(self):
        assert categorize("https://example.com") == "url"

    def test_http_url(self):
        assert categorize("http://example.com/path/to/page") == "url"

    def test_url_with_query(self):
        assert categorize("https://api.example.com/v1/users?id=123") == "url"


class TestEmail:
    def test_basic_email(self):
        assert categorize("susmeta@example.com") == "email"

    def test_email_with_dots(self):
        assert categorize("first.last@company.co.uk") == "email"


class TestCode:
    def test_python_function(self):
        snippet = "def hello():\n    print('hello world')\n    return True"
        assert categorize(snippet) == "code"

    def test_javascript_const(self):
        assert categorize("const x = () => { return 42; }") == "code"

    def test_import_statement(self):
        assert categorize("import pandas as pd") == "code"

    def test_curly_braces(self):
        assert categorize('{"key": "value", "nested": {}}') == "code"

    def test_indented_block(self):
        code = "\n".join(["    line one", "    line two", "    line three", "    line four"])
        assert categorize(code) == "code"


class TestPath:
    def test_unix_absolute_path(self):
        assert categorize("/Users/susmeta/Documents/file.txt") == "path"

    def test_home_relative_path(self):
        assert categorize("~/Downloads/report.pdf") == "path"

    def test_windows_path(self):
        assert categorize("C:\\Users\\Susmeta\\Documents\\file.txt") == "path"


class TestSensitive:
    def test_api_key_overrides_all(self):
        # Even if it looks like a code snippet, sensitive wins
        result = categorize("token = sk-abc123XYZ789abc123XYZ789abc123XYZ789abc123XYZ789")
        assert result == "sensitive"


class TestText:
    def test_plain_sentence(self):
        assert categorize("Meeting at 3pm in the main conference room.") == "text"

    def test_multiline_plain_text(self):
        text = "First line\nSecond line\nThird line"
        assert categorize(text) == "text"

    def test_number(self):
        assert categorize("42") == "text"
