"""
html_handler.py — HTML processing for ClipMCP v1.2.

Strips HTML to readable plain text using stdlib html.parser only.
No third-party dependencies.

Used when the clipboard contains public.html content (e.g. copying from
web pages, Notion, Google Docs, Slack).

Raw HTML is stored in the DB for fidelity.
Stripped plain text is what Claude and previews show.

Robustness note:
  Slack and other apps embed <svg> icon elements throughout their HTML.
  Relying on tag-depth tracking to skip these is fragile — malformed or
  self-closing SVGs leave _skip_depth permanently incremented, silently
  dropping all text that follows. We pre-strip noisy blocks with regex
  before feeding to the parser, avoiding this class of bug entirely.
"""

from __future__ import annotations

import html as _html_stdlib
import re
from html.parser import HTMLParser
from typing import Optional


# ---------------------------------------------------------------------------
# Pre-processing: regex-strip noisy blocks before HTML parsing
# ---------------------------------------------------------------------------

# These blocks are stripped entirely (tags + contents) before parsing.
# Using regex here is intentional and safe — these are well-delimited blocks.
_BLOCK_STRIP_PATTERNS = [
    re.compile(r"<script[\s\S]*?</script>", re.IGNORECASE),
    re.compile(r"<style[\s\S]*?</style>", re.IGNORECASE),
    re.compile(r"<svg[\s\S]*?</svg>", re.IGNORECASE),    # Slack/web icons
    re.compile(r"<head[\s\S]*?</head>", re.IGNORECASE),
    re.compile(r"<noscript[\s\S]*?</noscript>", re.IGNORECASE),
    re.compile(r"<iframe[\s\S]*?</iframe>", re.IGNORECASE),
]


def _preprocess(html_content: str) -> str:
    """Remove noisy block elements before parsing."""
    result = html_content
    for pattern in _BLOCK_STRIP_PATTERNS:
        result = pattern.sub(" ", result)
    return result


# ---------------------------------------------------------------------------
# HTML → plain text stripper
# ---------------------------------------------------------------------------

# Block-level tags that should produce a newline when encountered
_BLOCK_TAGS = {
    "p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6",
    "tr", "blockquote", "pre", "article", "section", "header",
    "footer", "nav", "aside", "figure", "figcaption",
}


class _TextExtractor(HTMLParser):
    """
    Minimal HTML → plain text extractor.
    Preserves paragraph breaks, collapses whitespace.

    After pre-processing, no skip-tag logic is needed — noisy blocks
    have already been removed by regex.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
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

    - Pre-strips <script>, <style>, <svg>, <head>, <noscript>, <iframe> blocks
    - Removes all remaining tags
    - Decodes HTML entities (&amp; → &, &lt; → <, etc.)
    - Preserves paragraph structure as newlines
    - Collapses whitespace

    Returns an empty string if parsing fails.
    """
    if not html_content:
        return ""
    try:
        preprocessed = _preprocess(html_content)
        parser = _TextExtractor()
        parser.feed(preprocessed)
        return parser.get_text()
    except Exception:
        # Malformed HTML — fall back to naive tag stripping
        return _naive_strip(html_content)


def _naive_strip(html_content: str) -> str:
    """Last-resort fallback: remove all < > tags, unescape entities."""
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
