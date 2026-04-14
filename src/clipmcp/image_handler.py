"""
image_handler.py — Image storage and retrieval for ClipMCP v1.1.

Handles saving clipboard images to disk and loading them back for MCP responses.
Images are stored as PNG files at ~/.clipmcp/images/<timestamp>_<hash>.png

No third-party dependencies — uses only stdlib + pyobjc (already required).
"""

from __future__ import annotations

import base64
import hashlib
import logging
import struct
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import config

logger = logging.getLogger(__name__)

# Max image size in bytes (5MB)
MAX_IMAGE_SIZE_BYTES = 5 * 1024 * 1024

# Images directory alongside the DB
def _images_dir() -> Path:
    return config.db_path_resolved.parent / "images"


# ---------------------------------------------------------------------------
# PNG utilities (no PIL needed)
# ---------------------------------------------------------------------------

def _get_png_dimensions(png_bytes: bytes) -> Optional[tuple[int, int]]:
    """
    Parse width and height from a PNG file header.
    PNG spec: bytes 16-20 = width, bytes 20-24 = height (big-endian uint32).
    Returns (width, height) or None if not a valid PNG.
    """
    if len(png_bytes) < 24:
        return None
    if png_bytes[:8] != b'\x89PNG\r\n\x1a\n':
        return None
    try:
        width = struct.unpack('>I', png_bytes[16:20])[0]
        height = struct.unpack('>I', png_bytes[20:24])[0]
        return width, height
    except struct.error:
        return None


def _tiff_to_png(tiff_bytes: bytes) -> Optional[bytes]:
    """
    Convert TIFF bytes to PNG bytes using macOS AppKit.
    Returns None on failure.
    """
    try:
        from AppKit import NSBitmapImageRep, NSPNGFileType  # type: ignore
        import objc

        # Load TIFF into NSBitmapImageRep
        ns_data = objc.lookUpClass('NSData').dataWithBytes_length_(tiff_bytes, len(tiff_bytes))
        rep = NSBitmapImageRep.imageRepWithData_(ns_data)
        if rep is None:
            return None

        # Convert to PNG
        png_data = rep.representationUsingType_properties_(NSPNGFileType, {})
        if png_data is None:
            return None

        return bytes(png_data)
    except Exception as e:
        logger.debug(f"TIFF to PNG conversion failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def save_image(png_bytes: bytes) -> Optional[tuple[str, str]]:
    """
    Save PNG bytes to disk.

    Returns (file_path, content_preview) or None if saving failed.

    content_preview format: "[image: 1024×768 PNG, 245KB]"
    """
    # Ensure images directory exists
    images_dir = _images_dir()
    images_dir.mkdir(parents=True, exist_ok=True)

    # Generate filename: timestamp + first 8 chars of hash
    content_hash = hashlib.sha256(png_bytes).hexdigest()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    filename = f"{timestamp}_{content_hash[:8]}.png"
    file_path = images_dir / filename

    try:
        file_path.write_bytes(png_bytes)
    except OSError as e:
        logger.error(f"Failed to save image to {file_path}: {e}")
        return None

    # Build content preview
    size_kb = round(len(png_bytes) / 1024, 1)
    dims = _get_png_dimensions(png_bytes)
    dim_str = f"{dims[0]}×{dims[1]} " if dims else ""
    preview = f"[image: {dim_str}PNG, {size_kb}KB]"

    return str(file_path), preview


def load_image_b64(file_path: str) -> Optional[str]:
    """
    Load an image from disk and return as base64-encoded string.
    Returns None if the file doesn't exist or can't be read.
    """
    path = Path(file_path)
    if not path.exists():
        logger.warning(f"Image file not found: {file_path}")
        return None
    try:
        return base64.standard_b64encode(path.read_bytes()).decode("utf-8")
    except OSError as e:
        logger.error(f"Failed to read image {file_path}: {e}")
        return None


def delete_image_file(file_path: str) -> None:
    """Delete an image file from disk. Silent if already gone."""
    try:
        Path(file_path).unlink(missing_ok=True)
    except OSError as e:
        logger.warning(f"Failed to delete image file {file_path}: {e}")


def image_hash(png_bytes: bytes) -> str:
    """SHA256 hash of image bytes — used for deduplication."""
    return hashlib.sha256(png_bytes).hexdigest()
