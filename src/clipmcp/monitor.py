"""
monitor.py — Background clipboard monitoring daemon for ClipMCP.

Design: Abstract Factory + Template Method patterns.

``ClipboardReader`` is an ABC that abstracts over the platform clipboard API.
``MacOSClipboardReader``  implements all three read methods using pyobjc.
``FallbackClipboardReader`` implements only text via pyperclip.
``create_clipboard_reader()`` is a factory that picks the right implementation.

``ClipboardMonitor`` holds a ``ClipboardReader`` instance and a poll loop.
The poll cycle follows a fixed priority:
  1. HTML  — richer than plain text; captures links/formatting (v1.2)
  2. Text  — plain text fallback
  3. Image — only when no text or HTML is present (v1.1)

Known limitations (documented):
  - Only runs while the MCP server is running (gaps when Claude is closed)
  - 500 ms polling means very fast consecutive copies may miss intermediates
  - source_app is the frontmost app at poll time, not necessarily the copier
"""

from __future__ import annotations

import logging
import platform
import threading
import time
from abc import ABC, abstractmethod
from typing import Optional

from .categorizer import categorize
from .config import config
from .embeddings import embed, is_available as embeddings_available, text_for_clip
from .html_handler import is_meaningful_html, strip_html
from .image_handler import MAX_IMAGE_SIZE_BYTES, image_hash, save_image, _tiff_to_png
from .models import ContentType
from .sensitive import is_sensitive
from .storage import insert_clip, store_embedding

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ClipboardReader — abstract interface
# ---------------------------------------------------------------------------

class ClipboardReader(ABC):
    """
    Abstract base class for platform clipboard access.

    Each concrete implementation reads from the OS clipboard using the
    most appropriate API for that platform.  Methods return None when the
    clipboard is empty, of an incompatible type, or on error — never raise.
    """

    @abstractmethod
    def read_text(self) -> Optional[str]:
        """Return the current plain-text content of the clipboard, or None."""
        ...

    @abstractmethod
    def read_html(self) -> Optional[str]:
        """Return raw HTML from the clipboard (public.html), or None."""
        ...

    @abstractmethod
    def read_image(self) -> Optional[bytes]:
        """Return clipboard image as PNG bytes, or None."""
        ...

    def supports_html(self) -> bool:
        """Whether this reader can return HTML content."""
        return True

    def supports_images(self) -> bool:
        """Whether this reader can return image content."""
        return True


# ---------------------------------------------------------------------------
# macOS implementation
# ---------------------------------------------------------------------------

class MacOSClipboardReader(ClipboardReader):
    """
    Reads from the macOS system pasteboard via pyobjc (AppKit / NSPasteboard).
    Falls back to pyperclip for plain text if pyobjc is not installed.
    """

    def read_text(self) -> Optional[str]:
        """Read plain text via NSStringPboardType."""
        try:
            from AppKit import NSPasteboard, NSStringPboardType  # type: ignore
            pb = NSPasteboard.generalPasteboard()
            content = pb.stringForType_(NSStringPboardType)
            return content if content else None
        except ImportError:
            logger.warning(
                "pyobjc-framework-Cocoa not installed — falling back to pyperclip. "
                "Install with: pip install pyobjc-framework-Cocoa"
            )
            return FallbackClipboardReader().read_text()
        except Exception as exc:
            logger.debug(f"Error reading macOS text clipboard: {exc}")
            return None

    def read_html(self) -> Optional[str]:
        """Read HTML via the public.html pasteboard type."""
        try:
            from AppKit import NSPasteboard  # type: ignore
            pb = NSPasteboard.generalPasteboard()
            html = pb.stringForType_("public.html")
            return html if html else None
        except Exception as exc:
            logger.debug(f"Error reading HTML from macOS clipboard: {exc}")
            return None

    def read_image(self) -> Optional[bytes]:
        """
        Read image as PNG bytes.
        Prefers public.png; falls back to public.tiff (converted to PNG).
        """
        try:
            from AppKit import NSPasteboard  # type: ignore
            pb = NSPasteboard.generalPasteboard()

            png_data = pb.dataForType_("public.png")
            if png_data and len(png_data) > 0:
                return bytes(png_data)

            tiff_data = pb.dataForType_("public.tiff")
            if tiff_data and len(tiff_data) > 0:
                return _tiff_to_png(bytes(tiff_data))

            return None
        except Exception as exc:
            logger.debug(f"Error reading image from macOS clipboard: {exc}")
            return None


# ---------------------------------------------------------------------------
# Fallback implementation (cross-platform, text only)
# ---------------------------------------------------------------------------

class FallbackClipboardReader(ClipboardReader):
    """
    Cross-platform reader using pyperclip.
    HTML and image reads always return None.
    """

    def read_text(self) -> Optional[str]:
        try:
            import pyperclip  # type: ignore
            content = pyperclip.paste()
            return content if content else None
        except ImportError:
            logger.error(
                "No clipboard library available. "
                "Install pyperclip: pip install pyperclip"
            )
            return None
        except Exception as exc:
            logger.debug(f"Error reading clipboard via pyperclip: {exc}")
            return None

    def read_html(self) -> Optional[str]:
        return None  # pyperclip does not support HTML

    def read_image(self) -> Optional[bytes]:
        return None  # pyperclip does not support images

    def supports_html(self) -> bool:
        return False

    def supports_images(self) -> bool:
        return False


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_clipboard_reader() -> ClipboardReader:
    """
    Return the most capable ClipboardReader for the current platform.

    macOS → MacOSClipboardReader (text + HTML + images)
    other → FallbackClipboardReader (text only)
    """
    if platform.system() == "Darwin":
        return MacOSClipboardReader()
    return FallbackClipboardReader()


# ---------------------------------------------------------------------------
# Source-app detection (macOS only, best-effort)
# ---------------------------------------------------------------------------

def _get_frontmost_app() -> Optional[str]:
    """
    Return the name of the frontmost application at poll time.

    Note: this is the app *in focus* when polled, not necessarily the app
    that triggered the copy operation.  Returns None on failure or non-macOS.
    """
    if platform.system() != "Darwin":
        return None
    try:
        from AppKit import NSWorkspace  # type: ignore
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        return app.localizedName() if app else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Truncation helper
# ---------------------------------------------------------------------------

def _truncate_to_limit(text: str, max_bytes: int) -> tuple[str, bool]:
    """
    Truncate *text* so its UTF-8 encoding fits within *max_bytes*.

    Uses ``errors='ignore'`` when decoding the truncated slice to avoid
    splitting a multi-byte character mid-sequence.

    Returns ``(possibly_truncated_text, was_truncated)``.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return truncated + " … [truncated]", True


# ---------------------------------------------------------------------------
# Embedding helper
# ---------------------------------------------------------------------------

def _embed_and_store(
    clip_id: int,
    content: str,
    content_type: str,
    content_preview: str,
) -> None:
    """
    Generate and persist an embedding for a newly inserted clip.
    No-op if sentence-transformers is not installed.
    Runs in the monitor thread (~5–15 ms, negligible vs 500 ms poll interval).
    """
    if not embeddings_available():
        return
    text = text_for_clip(content, content_type, content_preview)
    if not text:
        return
    vec = embed(text)
    if vec is not None:
        store_embedding(clip_id, vec)


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------

class ClipboardMonitor:
    """
    Background daemon that watches the clipboard and persists new entries.

    Usage::

        monitor = ClipboardMonitor()
        monitor.start()
        # … server runs …
        monitor.stop()

    The reader is injected at construction time (defaults to the platform
    reader) to make unit-testing straightforward.
    """

    def __init__(self, reader: Optional[ClipboardReader] = None) -> None:
        self._reader: ClipboardReader = reader or create_clipboard_reader()
        self._last_content: Optional[str] = None
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="clipmcp-monitor",
            daemon=True,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background monitoring thread."""
        logger.info(
            f"ClipMCP monitor starting "
            f"(poll interval: {config.poll_interval_ms} ms, "
            f"max clip size: {config.max_clip_size_bytes} bytes, "
            f"reader: {type(self._reader).__name__})"
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the monitor to stop and wait for it to exit."""
        logger.info("ClipMCP monitor stopping…")
        self._stop_event.set()
        self._thread.join(timeout=2.0)

    def is_running(self) -> bool:
        return self._thread.is_alive()

    # ------------------------------------------------------------------
    # Poll loop
    # ------------------------------------------------------------------

    def _run(self) -> None:
        """Main poll loop — runs in the daemon thread."""
        interval = config.poll_interval_seconds
        while not self._stop_event.is_set():
            try:
                self._poll()
            except Exception as exc:
                logger.error(f"Unexpected error in clipboard monitor: {exc}", exc_info=True)
            self._stop_event.wait(timeout=interval)

    def _poll(self) -> None:
        """
        Single poll cycle.

        Priority:
          1. HTML  — richer representation when copying from web/Notion/Docs
          2. Text  — plain text fallback
          3. Image — only when no text or HTML is present
        """
        if self._reader.supports_html():
            if self._poll_html():
                return

        if self._poll_text():
            return

        if self._reader.supports_images():
            self._poll_image()

    def _poll_html(self) -> bool:
        """
        Check for HTML on the clipboard.
        Returns True if a new HTML clip was handled (caller should short-circuit).
        """
        html = self._reader.read_html()
        if not html or not is_meaningful_html(html):
            return False
        if html == self._last_content:
            return True  # already handled — still short-circuit text path

        html, was_truncated = _truncate_to_limit(html, config.max_clip_size_bytes)
        if was_truncated:
            logger.debug(f"HTML clip truncated to {config.max_clip_size_bytes} bytes")

        stripped    = strip_html(html)
        sensitive   = is_sensitive(stripped) if config.detect_sensitive else False
        category    = categorize(stripped)   if config.categories_enabled else ContentType.TEXT
        source_app  = _get_frontmost_app()

        clip_id = insert_clip(
            content=html,
            category=category,
            source_app=source_app,
            is_sensitive=sensitive,
            content_type=ContentType.HTML,
            stripped_text=stripped,
        )

        if clip_id is not None:
            logger.debug(
                f"Saved HTML clip #{clip_id} | category={category} | "
                f"sensitive={sensitive} | app={source_app} | length={len(html)}"
            )
            _embed_and_store(clip_id, html, ContentType.HTML, stripped)

        self._last_content = html
        return True

    def _poll_text(self) -> bool:
        """
        Check for plain text on the clipboard.
        Returns True if a new text clip was handled.
        """
        text = self._reader.read_text()
        if not text:
            return False
        if text == self._last_content:
            return True

        text, was_truncated = _truncate_to_limit(text, config.max_clip_size_bytes)
        if was_truncated:
            logger.debug(f"Text clip truncated to {config.max_clip_size_bytes} bytes")

        sensitive   = is_sensitive(text) if config.detect_sensitive else False
        category    = categorize(text)   if config.categories_enabled else ContentType.TEXT
        source_app  = _get_frontmost_app()

        clip_id = insert_clip(
            content=text,
            category=category,
            source_app=source_app,
            is_sensitive=sensitive,
            content_type=ContentType.TEXT,
        )

        if clip_id is not None:
            logger.debug(
                f"Saved text clip #{clip_id} | category={category} | "
                f"sensitive={sensitive} | app={source_app} | length={len(text)}"
            )
            _embed_and_store(clip_id, text, ContentType.TEXT, text[:100])

        self._last_content = text
        return True

    def _poll_image(self) -> None:
        """Check for a new image on the clipboard and persist it if found."""
        image_bytes = self._reader.read_image()
        if not image_bytes:
            return

        img_hash = image_hash(image_bytes)
        if img_hash == self._last_content:
            return

        if len(image_bytes) > MAX_IMAGE_SIZE_BYTES:
            logger.debug(
                f"Skipping oversized image "
                f"({len(image_bytes)} bytes > {MAX_IMAGE_SIZE_BYTES})"
            )
            self._last_content = img_hash
            return

        source_app = _get_frontmost_app()
        result     = save_image(image_bytes)
        if result is None:
            logger.error("Failed to save image to disk — skipping")
            return

        file_path, preview = result

        clip_id = insert_clip(
            content=preview,
            category=ContentType.IMAGE,
            source_app=source_app,
            is_sensitive=False,
            content_type=ContentType.IMAGE,
            file_path=file_path,
            content_hash=img_hash,
        )

        if clip_id is not None:
            logger.debug(f"Saved image clip #{clip_id} | {preview} | app={source_app}")

        self._last_content = img_hash


# ---------------------------------------------------------------------------
# Module-level singleton — imported and started by server.py
# ---------------------------------------------------------------------------

monitor = ClipboardMonitor()
