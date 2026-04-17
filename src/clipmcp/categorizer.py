"""
categorizer.py — Rule-based clipboard content classification.

Design: Strategy / Chain-of-Responsibility pattern.

Each ContentCategory is implemented as a CategoryRule subclass.
Rules are ordered by priority; the first match wins.

Public API (unchanged from v1.x):
    categorize(content: str) -> str

The module-level ``categorize()`` delegates to a default
``RuleBasedCategorizer`` instance built from all registered rules.
Adding a new category = subclass ``CategoryRule``, add to
``_DEFAULT_RULES``.

Priority order:
  0  SensitiveRule  — checked first; overrides all others (security)
  1  ErrorRule      — stack traces, exceptions, log errors (debug workflow)
  2  UrlRule
  3  EmailRule
  4  CodeRule
  5  PathRule
  6  TextRule       — catch-all; always matches
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Sequence

from .models import ContentCategory
from .sensitive import is_sensitive


# ---------------------------------------------------------------------------
# Abstract base rule
# ---------------------------------------------------------------------------

class CategoryRule(ABC):
    """
    Abstract base class for a single content-categorisation rule.

    Subclasses declare their *priority* (lower = evaluated earlier) and
    implement *matches()* to decide whether the rule applies.
    """

    #: Evaluation order — lower values are tested first.
    priority: int

    #: The category assigned when this rule matches.
    category: ContentCategory

    @abstractmethod
    def matches(self, content: str) -> bool:
        """Return True if this rule applies to *content* (already stripped)."""
        ...

    def __repr__(self) -> str:
        return f"<{type(self).__name__} priority={self.priority} category={self.category}>"


# ---------------------------------------------------------------------------
# Concrete rules
# ---------------------------------------------------------------------------

class SensitiveRule(CategoryRule):
    """Detects API keys, passwords, tokens, and other credentials."""

    priority = 0
    category = ContentCategory.SENSITIVE

    def matches(self, content: str) -> bool:
        return is_sensitive(content)


# ---- Error detection helpers -----------------------------------------------

_ERROR_HEADLINE_RE = re.compile(
    r"""
    (
        # Python
        Traceback\s*\(most\s+recent\s+call\s+last\)  |
        \w*(Error|Exception|Warning|Fault|Panic):\s   |

        # Java / Kotlin / Scala
        at\s+[\w.$]+\([\w.]+:\d+\)                   |
        Exception\s+in\s+thread\s+"                   |
        Caused\s+by:\s+\w                             |

        # JavaScript / Node / TypeScript
        (TypeError|ReferenceError|SyntaxError|RangeError|URIError):\s  |
        \s+at\s+\w+\s+\(.*:\d+:\d+\)                |

        # Go
        goroutine\s+\d+\s+\[                          |
        panic:\s                                       |

        # Rust
        thread\s+'[^']+'\s+panicked\s+at\s            |

        # Generic log error levels
        \b(ERROR|FATAL|CRITICAL|SEVERE)\b[\s:\[]       |

        # HTTP error responses
        "status":\s*[45]\d\d                           |
        HTTP/\d\.\d\s+[45]\d\d\s                      |
        status\s+code\s+[45]\d\d                       |

        # SQL / DB errors
        (SQL|Database|DB)\s*(Error|Exception)          |
        ORA-\d{5}                                      |
        SQLSTATE\s*\[                                  |

        # Generic shell / CLI errors
        \bcommand\s+not\s+found\b                      |
        \bNo\s+such\s+file\s+or\s+directory\b          |
        \bPermission\s+denied\b                        |
        \bConnection\s+refused\b                       |
        \bSegmentation\s+fault\b
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)

_STACK_FRAME_RE = re.compile(
    r"""
    (
        ^\s+at\s+[\w.$<>]+                             |  # Java/JS frame
        ^\s+File\s+"[^"]+",\s+line\s+\d+              |  # Python frame
        ^\s+in\s+\w+\s+at\s+                           |  # Rust/Go frame
        ^\s+\w+\.\w+\(                                 |  # Kotlin/Scala
        ^\[?\d{4}-\d{2}-\d{2}.*\b(ERROR|FATAL|WARN)\b    # Timestamped log
    )
    """,
    re.VERBOSE | re.MULTILINE,
)

_MIN_STACK_FRAMES = 2


class ErrorRule(CategoryRule):
    """
    Detects stack traces, exception messages, and log error lines.
    Evaluated before CodeRule because tracebacks look like code.
    """

    priority = 1
    category = ContentCategory.ERROR

    def matches(self, content: str) -> bool:
        if _ERROR_HEADLINE_RE.search(content):
            return True
        return len(_STACK_FRAME_RE.findall(content)) >= _MIN_STACK_FRAMES


# ---- Simpler pattern rules -------------------------------------------------

_URL_RE = re.compile(
    r'^https?://[^\s]+$|'
    r'^(www\.)[^\s]+\.[a-zA-Z]{2,}(/[^\s]*)?$',
    re.IGNORECASE,
)

_EMAIL_RE = re.compile(
    r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$'
)

_PATH_RE = re.compile(
    r'^(/|~/|[A-Za-z]:\\)',  # Unix absolute, home-relative, or Windows
)

_CODE_KEYWORDS: tuple[str, ...] = (
    "def ", "import ", "class ", "function ", "const ", "let ", "var ",
    "=>", "return ", "if (", "for (", "while (", "async ", "await ",
    "#include", "public static", "fn ", "mod ", "use ",
)

_CODE_SYMBOLS_RE = re.compile(r'[{}]')


class UrlRule(CategoryRule):
    priority = 2
    category = ContentCategory.URL

    def matches(self, content: str) -> bool:
        return "\n" not in content and bool(_URL_RE.match(content))


class EmailRule(CategoryRule):
    priority = 3
    category = ContentCategory.EMAIL

    def matches(self, content: str) -> bool:
        return "\n" not in content and bool(_EMAIL_RE.match(content))


class CodeRule(CategoryRule):
    priority = 4
    category = ContentCategory.CODE

    def matches(self, content: str) -> bool:
        for keyword in _CODE_KEYWORDS:
            if keyword in content:
                return True
        if _CODE_SYMBOLS_RE.search(content):
            return True
        lines = content.splitlines()
        if len(lines) >= 3:
            indented = sum(1 for line in lines if line.startswith(("    ", "\t")))
            if indented >= 3:
                return True
        return False


class PathRule(CategoryRule):
    priority = 5
    category = ContentCategory.PATH

    def matches(self, content: str) -> bool:
        return "\n" not in content and bool(_PATH_RE.match(content))


class TextRule(CategoryRule):
    """Catch-all — always matches. Must be last in the priority order."""

    priority = 99
    category = ContentCategory.TEXT

    def matches(self, content: str) -> bool:
        return True


# ---------------------------------------------------------------------------
# Categorizer — orchestrates rules in priority order
# ---------------------------------------------------------------------------

class RuleBasedCategorizer:
    """
    Applies a sorted list of CategoryRules and returns the category of the
    first matching rule.

    Usage::

        categorizer = RuleBasedCategorizer([SensitiveRule(), ErrorRule(), ...])
        category = categorizer.categorize("KeyError: 'x'\\n  File app.py")
        # → ContentCategory.ERROR
    """

    def __init__(self, rules: Sequence[CategoryRule]) -> None:
        # Sort once at construction time; rules are immutable afterwards.
        self._rules: list[CategoryRule] = sorted(rules, key=lambda r: r.priority)

    def categorize(self, content: str) -> ContentCategory:
        """
        Classify *content* into a category.

        Strips leading/trailing whitespace before matching so rules don't
        need to handle surrounding whitespace themselves.
        """
        stripped = content.strip()
        for rule in self._rules:
            if rule.matches(stripped):
                return rule.category
        # Should never reach here if TextRule is registered, but be safe.
        return ContentCategory.TEXT

    @property
    def rules(self) -> list[CategoryRule]:
        """Read-only view of registered rules in priority order."""
        return list(self._rules)


# ---------------------------------------------------------------------------
# Default categorizer instance + module-level convenience function
# ---------------------------------------------------------------------------

_DEFAULT_RULES: list[CategoryRule] = [
    SensitiveRule(),
    ErrorRule(),
    UrlRule(),
    EmailRule(),
    CodeRule(),
    PathRule(),
    TextRule(),
]

_default_categorizer = RuleBasedCategorizer(_DEFAULT_RULES)


def categorize(content: str) -> str:
    """
    Classify clipboard content into a category string.

    Returns one of: 'sensitive' | 'error' | 'url' | 'email' |
                    'code' | 'path' | 'text'

    Delegates to the default ``RuleBasedCategorizer``.  Replace
    ``_default_categorizer`` to customise behaviour globally.
    """
    return _default_categorizer.categorize(content)
