"""
html_handler.py — HTML processing for ClipMCP v1.2.

Strips HTML to readable plain text using stdlib html.parser only.
No third-party dependencies.

Used when the clipboard contains public.html content (e.g. copying from
web pages, Notion, Google Docs, Slack).

Raw HTML is stored in the DB for fidelity.
Stripped plain text is what Claude and previews show.
"""

from __future__ import annotations

import html as _html_stdlib
from html.parser import HTMLParser
from typing import Optional


# ---------------------------------------------------------------------------
# HTML → plain text stripper
# ---------------------------------------------------------------------------

# Block-level tags that should produce a newline when encountered
_BLOCK_TAGS = {
    "p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6",
    "tr", "blockquote", "pre", "article", "section", "header",
    "footer", "nav", "aside", "figure", "figcaption",
}

# Tags whose content we skip entirely (scripts, styles, metadata)
_SKIP_TAGS = {"script", "style", "head", "noscript", "svg", "iframe"}


class _TextExtractor(HTMLParser):
    """
    Minimal HTML → plain text extractor.
    Preserves paragraph breaks, collapses whitespace.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth: int = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._parts.append(data)

    def get_text(self) -> str:
        raw = "".join(self._parts)
        # Collapse runs of whitespace/newlines into single spaces or single newlines
        lines = [" ".join(line.split()) for line in raw.splitlines()]
        # Remove blank lines, join with single newline
        cleaned = "\n".join(line for line in lines if line)
        return cleaned.strip()


def strip_html(html_content: str) -> str:
    """
    Extract readable plain text from an HTML string.

    - Removes all tags
    - Decodes HTML entities (&amp; → &, &lt; → <, etc.)
    - Drops script/style/head content
    - Preserves paragraph structure as newlines
    - Collapses whitespace

    Returns an empty string if parsing fails.
    """
    try:
        parser = _TextExtractor()
        parser.feed(html_content)
        return parser.get_text()
    except Exception:
        # Malformed HTML — fall back to naive tag stripping
        return _naive_strip(html_content)


def _naive_strip(html_content: str) -> str:
    """Last-resort fallback: remove all < > tags, unescape entities."""
    import re
    no_tags = re.sub(r"<[^>]+>", " ", html_content)
    return " ".join(_html_stdlib.unescape(no_tags).split())


def is_meaningful_html(html_content: str) -> bool:
    """
    Returns True if the HTML content is worth storing separately from plain text.
    Filters out trivial cases like a plain-text string wrapped in a single <span>.
    """
    if not html_content or not html_content.strip():
        return False

    stripped = strip_html(html_content)
    if not stripped:
        return False

    # If the HTML has links, lists, headings, or tables it's genuinely rich
    lower = html_content.lower()
    rich_indicators = ["<a ", "<ul", "<ol", "<li", "<table", "<h1", "<h2",
                       "<h3", "<h4", "<h5", "<h6", "<img", "<code", "<pre",
                       "<blockquote", "<strong", "<em", "<b>", "<i>"]
    return any(tag in lower for tag in rich_indicators)
