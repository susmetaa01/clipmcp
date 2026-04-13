"""
config.py — Settings & defaults for ClipMCP.

Loads from ~/.clipmcp/config.json if it exists.
Creates the config file and data directory on first run.
All other modules import the singleton `config` object from here.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path


# Default data directory
DEFAULT_DIR = Path.home() / ".clipmcp"
DEFAULT_CONFIG_PATH = DEFAULT_DIR / "config.json"
DEFAULT_DB_PATH = DEFAULT_DIR / "history.db"


@dataclass
class Config:
    poll_interval_ms: int = 500          # How often to poll the clipboard (ms)
    max_history_size: int = 10_000       # Max number of clips to keep
    auto_prune: bool = True              # Automatically prune old clips
    prune_after_days: int = 30           # Delete clips older than N days
    detect_sensitive: bool = True        # Run sensitive data detection
    categories_enabled: bool = True      # Run auto-categorization
    max_clip_size_bytes: int = 50_000    # Skip clips larger than this (50KB)
    db_path: str = str(DEFAULT_DB_PATH)  # SQLite database location

    @property
    def db_path_resolved(self) -> Path:
        """Return the DB path with ~ expanded."""
        return Path(self.db_path).expanduser()

    @property
    def poll_interval_seconds(self) -> float:
        return self.poll_interval_ms / 1000.0


def _load_config() -> Config:
    """Load config from disk, creating defaults if missing."""
    # Ensure the data directory exists
    DEFAULT_DIR.mkdir(parents=True, exist_ok=True)

    if DEFAULT_CONFIG_PATH.exists():
        try:
            with open(DEFAULT_CONFIG_PATH, "r") as f:
                data = json.load(f)
            # Merge with defaults so new fields are always present
            defaults = asdict(Config())
            defaults.update(data)
            return Config(**{k: v for k, v in defaults.items() if k in Config.__dataclass_fields__})
        except (json.JSONDecodeError, TypeError):
            # Corrupt config — fall back to defaults
            pass

    # Write defaults to disk on first run
    cfg = Config()
    with open(DEFAULT_CONFIG_PATH, "w") as f:
        json.dump(asdict(cfg), f, indent=2)

    return cfg


# Singleton — all modules import this
config = _load_config()
