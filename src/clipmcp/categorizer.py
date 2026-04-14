"""
categorizer.py — Rule-based clipboard content classification.

Applies rules in priority order. Returns one of:
  'sensitive' | 'url' | 'email' | 'code' | 'path' | 'text'
"""

from __future__ import annotations

import re
from .sensitive import is_sensitive

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

_URL_RE = re.compile(
    r'^https?://[^\s]+$|'                          # starts with http(s)://
    r'^(www\.)[^\s]+\.[a-zA-Z]{2,}(/[^\s]*)?$',   # or starts with www.
    re.IGNORECASE,
)

_EMAIL_RE = re.compile(
    r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$'
)

_PATH_RE = re.compile(
    r'^(/|~/|[A-Za-z]:\\)',   # Unix absolute, home-relative, or Windows path
)

# Keywords and symbols that strongly suggest code
_CODE_KEYWORDS = [
    "def ", "import ", "class ", "function ", "const ", "let ", "var ",
    "=>", "return ", "if (", "for (", "while (", "async ", "await ",
    "#include", "public static", "fn ", "mod ", "use ",
]

_CODE_SYMBOLS_RE = re.compile(r'[{}]')

def _looks_like_code(content: str) -> bool:
    """Heuristic: does this look like a code snippet?"""
    # Check for keywords
    for keyword in _CODE_KEYWORDS:
        if keyword in content:
            return True

    # Check for braces
    if _CODE_SYMBOLS_RE.search(content):
        return True

    # Check for 3+ lines with leading indentation
    lines = content.splitlines()
    if len(lines) >= 3:
        indented = sum(1 for line in lines if line.startswith(("    ", "\t")))
        if indented >= 3:
            return True

    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def categorize(content: str) -> str:
    """
    Classify clipboard content into a category.

    Priority order:
      1. sensitive  — checked first because it overrides all others
      2. url
      3. email
      4. code
      5. path
      6. text       — default fallback
    """
    stripped = content.strip()

    # 1. Sensitive (highest priority — marks clearly dangerous content)
    if is_sensitive(stripped):
        return "sensitive"

    # 2. URL — single-line content matching a URL pattern
    if "\n" not in stripped and _URL_RE.match(stripped):
        return "url"

    # 3. Email — single-line content matching email pattern
    if "\n" not in stripped and _EMAIL_RE.match(stripped):
        return "email"

    # 4. Code — multi-indicator heuristic
    if _looks_like_code(stripped):
        return "code"

    # 5. Path — starts with /, ~/, or C:\
    if "\n" not in stripped and _PATH_RE.match(stripped):
        return "path"

    # 6. Default
    return "text"
