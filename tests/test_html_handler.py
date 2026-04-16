"""
Tests for html_handler.py — HTML stripping and classification logic.
"""

import pytest
from clipmcp.html_handler import strip_html, is_meaningful_html


class TestStripHtml:
    def test_strips_basic_tags(self):
        assert strip_html("<p>Hello world</p>") == "Hello world"

    def test_strips_nested_tags(self):
        result = strip_html("<div><p>Hello <strong>world</strong></p></div>")
        assert "Hello" in result
        assert "world" in result
        assert "<" not in result

    def test_decodes_entities(self):
        result = strip_html("<p>AT&amp;T &lt;rocks&gt;</p>")
        assert "AT&T" in result
        assert "<rocks>" in result

    def test_removes_script_content(self):
        result = strip_html("<p>Hello</p><script>alert('xss')</script><p>World</p>")
        assert "Hello" in result
        assert "World" in result
        assert "alert" not in result
        assert "xss" not in result

    def test_removes_style_content(self):
        result = strip_html("<style>body { color: red; }</style><p>Text</p>")
        assert "Text" in result
        assert "color" not in result

    def test_preserves_text_across_block_elements(self):
        result = strip_html("<h1>Title</h1><p>Body text</p>")
        assert "Title" in result
        assert "Body text" in result

    def test_collapses_whitespace(self):
        result = strip_html("<p>Hello   \n\n   world</p>")
        # Should not have multiple consecutive spaces
        assert "  " not in result
        assert "Hello" in result
        assert "world" in result

    def test_handles_br_tag(self):
        result = strip_html("Line one<br>Line two")
        assert "Line one" in result
        assert "Line two" in result

    def test_handles_empty_string(self):
        assert strip_html("") == ""

    def test_handles_plain_text_no_tags(self):
        result = strip_html("Just plain text")
        assert result == "Just plain text"

    def test_handles_malformed_html(self):
        # Should not raise, should return something readable
        result = strip_html("<p>Unclosed tag <b>bold text")
        assert "bold text" in result

    def test_link_text_preserved(self):
        result = strip_html('<a href="https://example.com">Click here</a>')
        assert "Click here" in result
        assert "href" not in result

    def test_table_content_preserved(self):
        result = strip_html("<table><tr><td>Cell 1</td><td>Cell 2</td></tr></table>")
        assert "Cell 1" in result
        assert "Cell 2" in result

    def test_list_items_preserved(self):
        result = strip_html("<ul><li>Item one</li><li>Item two</li></ul>")
        assert "Item one" in result
        assert "Item two" in result


class TestIsMeaningfulHtml:
    def test_html_with_links_is_meaningful(self):
        assert is_meaningful_html('<p>See <a href="https://example.com">this link</a></p>') is True

    def test_html_with_list_is_meaningful(self):
        assert is_meaningful_html("<ul><li>Item 1</li><li>Item 2</li></ul>") is True

    def test_html_with_heading_is_meaningful(self):
        assert is_meaningful_html("<h1>Title</h1><p>Body</p>") is True

    def test_html_with_code_block_is_meaningful(self):
        assert is_meaningful_html("<pre><code>print('hello')</code></pre>") is True

    def test_plain_span_is_not_meaningful(self):
        # A plain text string wrapped in a span has no rich structure
        assert is_meaningful_html("<span>just some text</span>") is False

    def test_empty_string_is_not_meaningful(self):
        assert is_meaningful_html("") is False

    def test_whitespace_only_is_not_meaningful(self):
        assert is_meaningful_html("   \n  ") is False

    def test_html_that_strips_to_empty_is_not_meaningful(self):
        assert is_meaningful_html("<script>alert(1)</script>") is False

    def test_table_is_meaningful(self):
        assert is_meaningful_html("<table><tr><td>data</td></tr></table>") is True

    def test_strong_em_are_meaningful(self):
        assert is_meaningful_html("<p>This is <strong>important</strong></p>") is True
