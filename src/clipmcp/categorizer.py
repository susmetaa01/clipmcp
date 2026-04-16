"""
categorizer.py — Rule-based clipboard content classification.

Applies rules in priority order. Returns one of:
  'sensitive' | 'error' | 'url' | 'email' | 'code' | 'path' | 'text'

v1.3: Added 'error' category — detects stack traces, exception messages,
      log errors, and HTTP error responses so Claude can find them instantly
      when asked to help debug.
"""

from __future__ import annotations

import re
from .sensitive import is_sensitive

# ---------------------------------------------------------------------------
# Error detection patterns
# ---------------------------------------------------------------------------

# Exception / error headline patterns (language-agnostic)
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
        ORA-\d{5}                                      |    # Oracle error codes
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

# Stack frame line patterns — multiple of these in a clip = definitely a stack trace
_STACK_FRAME_RE = re.compile(
    r"""
    (
        ^\s+at\s+[\w.$<>]+       |   # Java/JS: "    at com.example.Foo(Bar.java:42)"
        ^\s+File\s+"[^"]+",\s+line\s+\d+  |   # Python: '  File "foo.py", line 42'
        ^\s+in\s+\w+\s+at\s+     |   # Rust/Go frame
        ^\s+\w+\.\w+\(           |   # Kotlin/Scala style
        ^\[?\d{4}-\d{2}-\d{2}.*\b(ERROR|FATAL|WARN)\b  # Timestamped log line
    )
    """,
    re.VERBOSE | re.MULTILINE,
)

_MIN_STACK_FRAMES = 2  # at least 2 frame lines to confirm it's a trace


def _looks_like_error(content: str) -> bool:
    """
    Returns True if content looks like an error message, stack trace,
    exception, or log error line.
    """
    # Fast path: headline pattern match
    if _ERROR_HEADLINE_RE.search(content):
        return True

    # Stack trace: multiple frame lines
    frames = _STACK_FRAME_RE.findall(content)
    if len(frames) >= _MIN_STACK_FRAMES:
        return True

    return False


# ---------------------------------------------------------------------------
# URL / Email / Path / Code patterns (unchanged)
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

_CODE_KEYWORDS = [
    "def ", "import ", "class ", "function ", "const ", "let ", "var ",
    "=>", "return ", "if (", "for (", "while (", "async ", "await ",
    "#include", "public static", "fn ", "mod ", "use ",
]

_CODE_SYMBOLS_RE = re.compile(r'[{}]')


def _looks_like_code(content: str) -> bool:
    """Heuristic: does this look like a code snippet?"""
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


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def categorize(content: str) -> str:
    """
    Classify clipboard content into a category.

    Priority order:
      1. sensitive  — checked first; overrides all others (security)
      2. error      — stack traces, exceptions, log errors (debug workflow)
      3. url
      4. email
      5. code
      6. path
      7. text       — default fallback
    """
    stripped = content.strip()

    # 1. Sensitive
    if is_sensitive(stripped):
        return "sensitive"

    # 2. Error — before code, since stack traces look like code
    if _looks_like_error(stripped):
        return "error"

    # 3. URL
    if "\n" not in stripped and _URL_RE.match(stripped):
        return "url"

    # 4. Email
    if "\n" not in stripped and _EMAIL_RE.match(stripped):
        return "email"

    # 5. Code
    if _looks_like_code(stripped):
        return "code"

    # 6. Path
    if "\n" not in stripped and _PATH_RE.match(stripped):
        return "path"

    # 7. Default
    return "text"
