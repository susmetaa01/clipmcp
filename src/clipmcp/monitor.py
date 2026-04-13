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
    """Read clipboard via native macOS AppKit pasteboard."""
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
        """Single poll cycle: read clipboard, check for changes, persist if new."""
        content = _read_clipboard()

        # Nothing on clipboard or non-text content
        if not content:
            return

        # In-memory dedup: skip if same as last seen (fast path before DB hit)
        if content == self._last_content:
            return

        # Size guard: skip oversized clips
        if len(content.encode("utf-8")) > config.max_clip_size_bytes:
            logger.debug(
                f"Skipping oversized clip ({len(content.encode('utf-8'))} bytes > "
                f"{config.max_clip_size_bytes} bytes limit)"
            )
            self._last_content = content  # still update last seen to avoid log spam
            return

        # Detect sensitive content
        sensitive = is_sensitive(content) if config.detect_sensitive else False

        # Categorize
        category = categorize(content) if config.categories_enabled else "text"

        # Source app (best-effort)
        source_app = _get_frontmost_app()

        # Persist — storage.insert_clip handles DB-level dedup
        clip_id = insert_clip(
            content=content,
            category=category,
            source_app=source_app,
            is_sensitive=sensitive,
        )

        if clip_id is not None:
            logger.debug(
                f"Saved clip #{clip_id} | category={category} | "
                f"sensitive={sensitive} | app={source_app} | "
                f"length={len(content)}"
            )

        # Always update last seen, even if insert was a DB-level dedup
        self._last_content = content


# ---------------------------------------------------------------------------
# Singleton — imported and started by server.py
# ---------------------------------------------------------------------------

monitor = ClipboardMonitor()
