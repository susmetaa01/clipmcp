# ClipMCP — Developer Guide

A standalone clipboard history MCP server for macOS. Monitors your system clipboard in real-time, stores history locally in SQLite, and exposes it to Claude and other AI assistants — no third-party clipboard app required.

---

## Prerequisites

- macOS (v1 only)
- Python 3.11+
- [pyenv](https://github.com/pyenv/pyenv) recommended for Python version management
- Claude Desktop installed

---

## Installation

**1. Clone the repo**
```bash
git clone <your-repo-url>
cd clipmcpxx
```

**2. Install the package**
```bash
pip install -e .
```

This installs all dependencies including:
- `mcp` — official MCP Python SDK
- `pyobjc-framework-Cocoa` — native macOS clipboard access

**3. Verify it starts**
```bash
python -m clipmcp
```

You should see:
```
ClipMCP monitor starting (poll interval: 500ms, max clip size: 50000 bytes)
ClipMCP server started.
```

Press `Ctrl+C` to stop.

---

## Connecting to Claude Desktop

Open this file in a text editor:
```
~/Library/Application Support/Claude/claude_desktop_config.json
```

Add the `mcpServers` block (keep any existing content):
```json
{
  "mcpServers": {
    "clipmcp": {
      "command": "/Users/<your-username>/.pyenv/versions/3.12.1/bin/python",
      "args": ["-m", "clipmcp"],
      "cwd": "/path/to/clipmcpxx"
    }
  }
}
```

Replace `<your-username>` and `/path/to/clipmcpxx` with your actual values. To find your Python path:
```bash
which python
```

**Fully quit and restart Claude Desktop** (Cmd+Q, not just close the window).

---

## Verifying it works

1. Copy any text to your clipboard
2. Open Claude Desktop
3. Ask: *"What did I just copy?"*

Claude will call `get_recent_clips` and tell you what's in your clipboard history.

---

## Available MCP Tools

Claude can call these tools automatically based on your conversation:

| Tool | What it does |
|---|---|
| `get_recent_clips` | Get the N most recent clipboard entries |
| `search_clips` | Search clipboard history by content |
| `pin_clip` | Pin a clip so it won't be auto-deleted |
| `unpin_clip` | Unpin a clip |
| `delete_clip` | Delete a specific clip |
| `get_clip_stats` | Usage stats — total clips, categories, DB size |
| `clear_history` | Wipe history (asks for confirmation) |

---

## Inspecting the Database

All clipboard history is stored at:
```
~/.clipmcp/history.db
```

**Open the SQLite shell:**
```bash
sqlite3 ~/.clipmcp/history.db
```

**Useful queries:**

```sql
-- View recent clips
SELECT id, content_preview, category, source_app, created_at
FROM clipboard_history
ORDER BY created_at DESC
LIMIT 20;

-- Filter by category
SELECT id, content_preview, created_at
FROM clipboard_history
WHERE category = 'url'
ORDER BY created_at DESC;

-- View sensitive clips
SELECT id, content_preview, created_at
FROM clipboard_history
WHERE is_sensitive = 1;

-- View pinned clips
SELECT id, content_preview, category, created_at
FROM clipboard_history
WHERE is_pinned = 1;

-- Stats by category
SELECT category, COUNT(*) as count
FROM clipboard_history
GROUP BY category
ORDER BY count DESC;

-- Stats by source app
SELECT source_app, COUNT(*) as count
FROM clipboard_history
WHERE source_app IS NOT NULL
GROUP BY source_app
ORDER BY count DESC;

-- Clips from today
SELECT id, content_preview, category, source_app
FROM clipboard_history
WHERE DATE(created_at) = DATE('now')
ORDER BY created_at DESC;

-- Exit
.quit
```

**Schema reference:**

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER | Auto-increment primary key |
| `content` | TEXT | Full clipboard content |
| `content_preview` | TEXT | First 100 characters |
| `content_hash` | TEXT | SHA256 hash (used for deduplication) |
| `category` | TEXT | `text`, `url`, `email`, `code`, `path`, `sensitive` |
| `source_app` | TEXT | App that was in focus when copied (best-effort) |
| `char_count` | INTEGER | Length of content |
| `is_pinned` | BOOLEAN | 1 if pinned, 0 if not |
| `is_sensitive` | BOOLEAN | 1 if sensitive content detected |
| `created_at` | DATETIME | Timestamp of copy |

---

## Configuration

Settings are stored at `~/.clipmcp/config.json` and created automatically on first run.

```json
{
  "poll_interval_ms": 500,
  "max_history_size": 10000,
  "auto_prune": true,
  "prune_after_days": 30,
  "detect_sensitive": true,
  "categories_enabled": true,
  "max_clip_size_bytes": 50000,
  "db_path": "~/.clipmcp/history.db"
}
```

| Setting | Default | Description |
|---|---|---|
| `poll_interval_ms` | `500` | How often to check the clipboard (milliseconds) |
| `max_history_size` | `10000` | Max clips to keep before pruning oldest |
| `auto_prune` | `true` | Automatically delete old clips |
| `prune_after_days` | `30` | Delete clips older than N days |
| `detect_sensitive` | `true` | Flag API keys, tokens, passwords |
| `categories_enabled` | `true` | Auto-categorize each clip |
| `max_clip_size_bytes` | `50000` | Skip clips larger than 50KB |
| `db_path` | `~/.clipmcp/history.db` | Location of the SQLite database |

---

## Project Structure

```
clipmcpxx/
├── pyproject.toml              # Package config and dependencies
├── README.md                   # This file
├── src/
│   └── clipmcp/
│       ├── __init__.py
│       ├── __main__.py         # Entry point for python -m clipmcp
│       ├── config.py           # Settings loader
│       ├── storage.py          # SQLite read/write operations
│       ├── sensitive.py        # Sensitive data detection (regex)
│       ├── categorizer.py      # Content classification
│       ├── monitor.py          # Background clipboard polling thread
│       └── server.py           # MCP server and tool definitions
└── tests/
    ├── test_storage.py
    ├── test_categorizer.py
    └── test_sensitive.py
```

---

## Running Tests

```bash
pip install pytest
pytest tests/ -v
```

All 53 tests should pass.

---

## Known Limitations

- **Runs only while Claude is open** — clipboard history pauses when Claude Desktop is closed. For always-on monitoring, run as a `launchd` service (see roadmap).
- **500ms polling** — copying two things in very quick succession may miss the first one.
- **Source app is best-effort** — `source_app` reflects the frontmost app at poll time, not necessarily the app that did the copy.
- **Text only in v1** — images and rich text are not captured yet.

---

## Roadmap

| Version | Feature |
|---|---|
| v1.1 | Image clipboard support |
| v1.2 | Rich text / HTML clipboard content |
| v2.0 | Windows support |
| v2.1 | Linux support |
| v2.5 | Semantic search (local embeddings) |
| v2.6 | Selective encryption for sensitive clips |
| v2.7 | Clipboard manager UI (Cmd+Shift+V popup) |
| v3.0 | MCP Resources |

---

## License

MIT
