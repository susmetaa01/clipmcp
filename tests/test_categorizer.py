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


class TestError:
    def test_python_traceback(self):
        tb = (
            "Traceback (most recent call last):\n"
            "  File \"app.py\", line 42, in <module>\n"
            "    result = process(data)\n"
            "  File \"app.py\", line 17, in process\n"
            "    return data['key']\n"
            "KeyError: 'key'"
        )
        assert categorize(tb) == "error"

    def test_java_exception(self):
        tb = (
            "Exception in thread \"main\" java.lang.NullPointerException\n"
            "\tat com.example.Foo.bar(Foo.java:42)\n"
            "\tat com.example.Main.main(Main.java:10)"
        )
        assert categorize(tb) == "error"

    def test_javascript_type_error(self):
        assert categorize("TypeError: Cannot read properties of undefined (reading 'map')") == "error"

    def test_node_stack_trace(self):
        tb = (
            "ReferenceError: x is not defined\n"
            "    at Object.<anonymous> (/app/index.js:5:1)\n"
            "    at Module._compile (node:internal/modules/cjs/loader:1376:14)"
        )
        assert categorize(tb) == "error"

    def test_go_panic(self):
        assert categorize("panic: runtime error: index out of range [3] with length 2") == "error"

    def test_log_error_line(self):
        assert categorize("2024-01-15 10:23:45 ERROR Failed to connect to database: connection refused") == "error"

    def test_http_error_status(self):
        assert categorize('{"status": 503, "message": "Service Unavailable"}') == "error"

    def test_connection_refused(self):
        assert categorize("dial tcp 127.0.0.1:5432: connect: connection refused") == "error"

    def test_permission_denied(self):
        assert categorize("open /etc/secrets.env: permission denied") == "error"

    def test_error_overrides_code(self):
        # A stack trace looks like code but should be categorised as error
        tb = (
            "Traceback (most recent call last):\n"
            "  File \"main.py\", line 1, in <module>\n"
            "    import missing_module\n"
            "ModuleNotFoundError: No module named 'missing_module'"
        )
        assert categorize(tb) == "error"

    def test_plain_text_not_error(self):
        assert categorize("The meeting is at 3pm") != "error"

    def test_url_not_error(self):
        assert categorize("https://example.com") != "error"


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
