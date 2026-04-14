"""
monitor.py — Background clipboard monitoring daemon for ClipMCP.

Runs as a daemon thread alongside the MCP server.
Polls the macOS system pasteboard every poll_interval_ms milliseconds.
On each new clipboard entry:
  1. Checks size limit
  2. Detects sensitive content
  3. Categorizes content
  4. Detects source app (best-effort)
  5. Persists to SQLite via storage.py

Poll priority order:
  1. HTML  (v1.2) — richer than plain text, captures links/formatting
  2. Text         — plain text fallback
  3. Image (v1.1) — only when no text/HTML on clipboard

Known limitations (documented):
  - Only runs while the MCP server is running (gaps when Claude is closed)
  - 500ms polling means very fast consecutive copies may miss intermediate items
  - source_app is the frontmost app at poll time, not necessarily the copying app
"""

from __future__ import annotations

import logging
import platform
import threading
import time
from typing import Optional

from .categorizer import categorize
from .config import config
from .html_handler import is_meaningful_html, strip_html
from .image_handler import (
    MAX_IMAGE_SIZE_BYTES,
    image_hash,
    save_image,
    _tiff_to_png,
)
from .sensitive import is_sensitive
from .storage import insert_clip

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Platform clipboard reader
# ---------------------------------------------------------------------------

def _read_clipboard() -> Optional[str]:
    """
    Read the current clipboard text content.
    Returns None if the clipboard is empty, contains non-text, or on error.
    Uses pyobjc on macOS (native pasteboard), falls back to pyperclip on other platforms.
    """
    if platform.system() == "Darwin":
        return _read_clipboard_macos()
    else:
        return _read_clipboard_fallback()


def _read_clipboard_macos() -> Optional[str]:
    """Read clipboard text via native macOS AppKit pasteboard."""
    try:
        from AppKit import NSPasteboard, NSStringPboardType  # type: ignore
        pb = NSPasteboard.generalPasteboard()
        content = pb.stringForType_(NSStringPboardType)
        return content if content else None
    except ImportError:
        logger.warning(
            "pyobjc-framework-Cocoa not installed. "
            "Falling back to pyperclip. Install with: pip install pyobjc-framework-Cocoa"
        )
        return _read_clipboard_fallback()
    except Exception as e:
        logger.debug(f"Error reading macOS clipboard: {e}")
        return None


def _read_clipboard_html_macos() -> Optional[str]:
    """
    Read HTML content from macOS clipboard (public.html).
    Returns raw HTML string, or None if no HTML on clipboard.
    """
    try:
        from AppKit import NSPasteboard  # type: ignore
        pb = NSPasteboard.generalPasteboard()
        html = pb.stringForType_("public.html")
        return html if html else None
    except Exception as e:
        logger.debug(f"Error reading HTML from macOS clipboard: {e}")
        return None


def _read_clipboard_image_macos() -> Optional[bytes]:
    """
    Read image from macOS clipboard as PNG bytes.
    Prefers PNG, falls back to TIFF (converted to PNG).
    Returns None if no image on clipboard.
    """
    try:
        from AppKit import NSPasteboard  # type: ignore
        pb = NSPasteboard.generalPasteboard()

        # Try PNG first
        png_data = pb.dataForType_("public.png")
        if png_data and len(png_data) > 0:
            return bytes(png_data)

        # Fall back to TIFF and convert
        tiff_data = pb.dataForType_("public.tiff")
        if tiff_data and len(tiff_data) > 0:
            return _tiff_to_png(bytes(tiff_data))

        return None
    except Exception as e:
        logger.debug(f"Error reading image from macOS clipboard: {e}")
        return None


def _read_clipboard_fallback() -> Optional[str]:
    """Fallback clipboard reader using pyperclip (cross-platform)."""
    try:
        import pyperclip  # type: ignore
        content = pyperclip.paste()
        return content if content else None
    except ImportError:
        logger.error("No clipboard library available. Install pyperclip: pip install pyperclip")
        return None
    except Exception as e:
        logger.debug(f"Error reading clipboard via pyperclip: {e}")
        return None


# ---------------------------------------------------------------------------
# Source app detection (macOS only, best-effort)
# ---------------------------------------------------------------------------

def _get_frontmost_app() -> Optional[str]:
    """
    Returns the name of the frontmost application.
    Best-effort: this is the app in focus at poll time, not necessarily the copying app.
    Returns None on failure or non-macOS.
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
# Monitor thread
# ---------------------------------------------------------------------------

class ClipboardMonitor:
    """
    Background daemon that watches the clipboard and persists new entries.

    Usage:
        monitor = ClipboardMonitor()
        monitor.start()
        # ... server runs ...
        monitor.stop()
    """

    def __init__(self) -> None:
        self._last_content: Optional[str] = None
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="clipmcp-monitor",
            daemon=True,  # dies automatically when main process exits
        )

    def start(self) -> None:
        """Start the background monitoring thread."""
        logger.info(
            f"ClipMCP monitor starting "
            f"(poll interval: {config.poll_interval_ms}ms, "
            f"max clip size: {config.max_clip_size_bytes} bytes)"
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the monitor to stop and wait for it to exit."""
        logger.info("ClipMCP monitor stopping...")
        self._stop_event.set()
        self._thread.join(timeout=2.0)

    def is_running(self) -> bool:
        return self._thread.is_alive()

    def _run(self) -> None:
        """Main polling loop — runs in the background thread."""
        interval = config.poll_interval_seconds

        while not self._stop_event.is_set():
            try:
                self._poll()
            except Exception as e:
                # Never let an exception kill the monitor thread
                logger.error(f"Unexpected error in clipboard monitor: {e}", exc_info=True)

            self._stop_event.wait(timeout=interval)

    def _poll(self) -> None:
        """
        Single poll cycle: read clipboard, check for changes, persist if new.

        Priority order:
          1. HTML  — richer representation when copying from web/Notion/Docs
          2. Text  — plain text fallback
          3. Image — only when no text/HTML present
        """
        # --- HTML path (v1.2) — check before plain text ---
        if platform.system() == "Darwin":
            html = _read_clipboard_html_macos()
            if html and is_meaningful_html(html):
                if html == self._last_content:
                    return

                if len(html.encode("utf-8")) > config.max_clip_size_bytes:
                    logger.debug(f"Skipping oversized HTML clip ({len(html.encode('utf-8'))} bytes)")
                    self._last_content = html
                    return

                stripped = strip_html(html)
                sensitive = is_sensitive(stripped) if config.detect_sensitive else False
                category = categorize(stripped) if config.categories_enabled else "text"
                source_app = _get_frontmost_app()

                clip_id = insert_clip(
                    content=html,        # raw HTML stored for fidelity
                    category=category,
                    source_app=source_app,
                    is_sensitive=sensitive,
                    content_type="html",
                    stripped_text=stripped,
                )

                if clip_id is not None:
                    logger.debug(
                        f"Saved HTML clip #{clip_id} | category={category} | "
                        f"sensitive={sensitive} | app={source_app} | length={len(html)}"
                    )

                self._last_content = html
                return

        # --- Text path ---
        text = _read_clipboard()
        if text:
            if text == self._last_content:
                return

            if len(text.encode("utf-8")) > config.max_clip_size_bytes:
                logger.debug(f"Skipping oversized text clip ({len(text.encode('utf-8'))} bytes)")
                self._last_content = text
                return

            sensitive = is_sensitive(text) if config.detect_sensitive else False
            category = categorize(text) if config.categories_enabled else "text"
            source_app = _get_frontmost_app()

            clip_id = insert_clip(
                content=text,
                category=category,
                source_app=source_app,
                is_sensitive=sensitive,
                content_type="text",
            )

            if clip_id is not None:
                logger.debug(
                    f"Saved text clip #{clip_id} | category={category} | "
                    f"sensitive={sensitive} | app={source_app} | length={len(text)}"
                )

            self._last_content = text
            return

        # --- Image path (v1.1) ---
        if platform.system() == "Darwin":
            self._poll_image()

    def _poll_image(self) -> None:
        """Check for a new image on the clipboard and persist it if found."""
        image_bytes = _read_clipboard_image_macos()
        if not image_bytes:
            return

        # In-memory dedup via hash
        img_hash = image_hash(image_bytes)
        if img_hash == self._last_content:
            return

        # Size guard: 5MB max
        if len(image_bytes) > MAX_IMAGE_SIZE_BYTES:
            logger.debug(f"Skipping oversized image ({len(image_bytes)} bytes > {MAX_IMAGE_SIZE_BYTES})")
            self._last_content = img_hash
            return

        source_app = _get_frontmost_app()

        # Save image to disk
        result = save_image(image_bytes)
        if result is None:
            logger.error("Failed to save image to disk, skipping")
            return

        file_path, preview = result

        clip_id = insert_clip(
            content=preview,           # e.g. "[image: 1024×768 PNG, 245KB]"
            category="image",
            source_app=source_app,
            is_sensitive=False,
            content_type="image",
            file_path=file_path,
            content_hash=img_hash,
        )

        if clip_id is not None:
            logger.debug(
                f"Saved image clip #{clip_id} | {preview} | app={source_app}"
            )

        self._last_content = img_hash


# ---------------------------------------------------------------------------
# Singleton — imported and started by server.py
# ---------------------------------------------------------------------------

monitor = ClipboardMonitor()
