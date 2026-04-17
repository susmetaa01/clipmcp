"""
models.py — Core domain models and enumerations for ClipMCP.

Contains:
  - ContentType   : what format the clipboard content is in (text / html / image)
  - ContentCategory : what the content semantically represents (error / code / url / …)
  - Clip          : immutable value object returned by all storage queries

All other modules import from here rather than using bare string literals for
content types and categories, so typos fail loudly at import time instead of
silently at runtime.

Both enums inherit from str so they are JSON-serialisable and compare equal to
their string counterparts:  ContentCategory.ERROR == "error"  → True
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class ContentType(str, Enum):
    """Physical format of a clipboard entry."""
    TEXT  = "text"
    HTML  = "html"
    IMAGE = "image"


class ContentCategory(str, Enum):
    """
    Semantic category of a clipboard entry.

    Priority order (for debug context bundling and categorisation):
      SENSITIVE > ERROR > URL > EMAIL > CODE > PATH > TEXT
    IMAGE and HTML are assigned by content type, not text analysis.
    """
    SENSITIVE = "sensitive"
    ERROR     = "error"
    URL       = "url"
    EMAIL     = "email"
    CODE      = "code"
    PATH      = "path"
    TEXT      = "text"
    IMAGE     = "image"
    HTML      = "html"

    # Convenience: ordering for debug context (lower = higher priority)
    _priority_map = {}  # populated below

    def debug_priority(self) -> int:
        return _CATEGORY_DEBUG_PRIORITY.get(self, 99)


_CATEGORY_DEBUG_PRIORITY: dict[ContentCategory, int] = {
    ContentCategory.ERROR:     0,
    ContentCategory.CODE:      1,
    ContentCategory.SENSITIVE: 2,
    ContentCategory.HTML:      3,
    ContentCategory.TEXT:      4,
    ContentCategory.URL:       5,
    ContentCategory.EMAIL:     6,
    ContentCategory.PATH:      7,
    ContentCategory.IMAGE:     8,
}


# ---------------------------------------------------------------------------
# Value object
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Clip:
    """
    Immutable value object representing a single clipboard entry.

    ``content`` holds either the full raw content or a preview depending
    on the ``full_content`` flag passed to the storage query.  Callers
    should rely on ``content_preview`` for display and ``content`` for
    analysis.
    """

    id:              int
    content:         str              # full content or preview (see full_content flag)
    content_preview: str              # always the 100-char preview
    category:        str              # ContentCategory value (str for DB compat)
    source_app:      Optional[str]
    char_count:      int
    is_pinned:       bool
    is_sensitive:    bool
    content_type:    str              # ContentType value (str for DB compat)
    file_path:       Optional[str]    # only set for image clips
    created_at:      str

    # ------------------------------------------------------------------
    # Convenience properties
    # ------------------------------------------------------------------

    @property
    def is_image(self) -> bool:
        return self.content_type == ContentType.IMAGE

    @property
    def is_html(self) -> bool:
        return self.content_type == ContentType.HTML

    @property
    def typed_category(self) -> ContentCategory:
        """Return the category as a typed enum value."""
        try:
            return ContentCategory(self.category)
        except ValueError:
            return ContentCategory.TEXT

    @property
    def typed_content_type(self) -> ContentType:
        """Return the content_type as a typed enum value."""
        try:
            return ContentType(self.content_type)
        except ValueError:
            return ContentType.TEXT

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "id":              self.id,
            "content":         self.content,
            "content_preview": self.content_preview,
            "category":        self.category,
            "source_app":      self.source_app,
            "char_count":      self.char_count,
            "is_pinned":       self.is_pinned,
            "is_sensitive":    self.is_sensitive,
            "content_type":    self.content_type,
            "file_path":       self.file_path,
            "created_at":      self.created_at,
        }
