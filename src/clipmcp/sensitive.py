"""
sensitive.py — Sensitive data detection for ClipMCP.

Purely regex-based. No network calls, no ML.
Returns True if the content looks like a secret or credential.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

_PATTERNS: list[tuple[str, re.Pattern]] = [
    # OpenAI / Anthropic / generic API keys
    ("api_key_prefix", re.compile(
        r'\b(sk-|pk_|api_|key-)[A-Za-z0-9_\-]{16,}', re.IGNORECASE
    )),

    # AWS access keys
    ("aws_access_key", re.compile(
        r'\bAKIA[0-9A-Z]{16}\b'
    )),

    # AWS secret keys (40 char base64-ish)
    ("aws_secret_key", re.compile(
        r'\b[A-Za-z0-9/+]{40}\b'
    )),

    # JWT tokens
    ("jwt", re.compile(
        r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b'
    )),

    # GitHub tokens (length varies — use 20+ to be safe)
    ("github_token", re.compile(
        r'\bgh[pousr]_[A-Za-z0-9]{20,}\b'
    )),

    # Slack tokens
    ("slack_token", re.compile(
        r'\bxox[baprs]-[A-Za-z0-9\-]{10,}\b'
    )),

    # Private keys (PEM format)
    ("private_key", re.compile(
        r'-----BEGIN( [A-Z]+)? PRIVATE KEY-----'
    )),

    # Generic long random-looking alphanumeric strings (40+ chars, no spaces)
    # Catches many secret formats not covered above
    ("generic_secret", re.compile(
        r'\b[A-Za-z0-9_\-]{40,}\b'
    )),

    # Passwords in key=value form
    ("password_kv", re.compile(
        r'(password|passwd|pwd|secret)\s*[=:]\s*\S+', re.IGNORECASE
    )),
]

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_sensitive(content: str) -> bool:
    """
    Returns True if content looks like a secret or credential.
    Fast-fails on first match.
    """
    for _name, pattern in _PATTERNS:
        if pattern.search(content):
            return True
    return False


def matched_pattern(content: str) -> str | None:
    """
    Returns the name of the first matching pattern, or None.
    Useful for debugging and logging.
    """
    for name, pattern in _PATTERNS:
        if pattern.search(content):
            return name
    return None
