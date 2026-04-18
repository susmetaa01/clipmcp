# ClipMCP

A standalone clipboard history MCP server for macOS. Monitors your system clipboard in real-time, stores history locally in SQLite, and exposes it to Claude and other MCP-compatible AI assistants — no third-party clipboard app required. All data stays on your machine.

---

## What it does

- Captures **text, HTML, and images** from your clipboard automatically in the background
- **Categorises** each clip: `error`, `code`, `url`, `email`, `path`, `sensitive`, `text`
- **Detects sensitive content** (API keys, tokens, passwords) and flags it before Claude sees it
- **Semantic search** — find clips by meaning, not just keywords (optional, local embeddings)
- **Debug context bundling** — when you say "fix this error", Claude automatically pulls your recent error clips, code snippets, and logs together into one context block
- Exposes 9 MCP tools Claude calls automatically based on conversation context

---

## Requirements

- macOS (Linux/Windows support planned)
- Python 3.11+
- Claude Desktop

---

## Installation

**1. Clone the repo**
```bash
git clone https://github.com/susmetaa01/clipmcp.git
cd clipmcp
```

**2. Install the package**
```bash
pip install -e .
```

**3. (Optional) Semantic search**

Adds meaning-based search using a local `all-MiniLM-L6-v2` model (~80 MB, runs fully offline):
```bash
pip install -e ".[semantic]"
```

**4. Verify it starts**
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

Open (or create) this file:
```
~/Library/Application Support/Claude/claude_desktop_config.json
```

Add the `mcpServers` block:
```json
{
  "mcpServers": {
    "clipmcp": {
      "command": "/Users/<your-username>/.pyenv/versions/3.12.1/bin/python",
      "args": ["-m", "clipmcp"],
      "cwd": "/path/to/clipmcp/src"
    }
  }
}
```

To find your Python path:
```bash
which python3
```

**Fully quit and restart Claude Desktop** (Cmd+Q, not just close the window).

---

## Verifying it works

1. Copy any text to your clipboard
2. Open Claude Desktop
3. Ask: *"What did I just copy?"*

Claude will call `get_recent_clips` and show your clipboard history.

For debug workflows:
1. Copy an error or stack trace from your terminal/IDE
2. Ask: *"Fix this error"* or *"What's wrong?"*

Claude will call `get_debug_context`, which surfaces error clips first, then code, then everything else — no pasting required.

---

## Available MCP Tools

Claude calls these automatically based on context. You never need to mention them.

| Tool | Triggered when you say… |
|---|---|
| `get_recent_clips` | "what did I copy", "analyse this", "I just copied…" |
| `search_clips` | "find the AWS case", "look up the error from earlier" |
| `semantic_search` | "what was that Slack thread about Grafana" |
| `get_debug_context` | "fix this error", "what's wrong", "debug this" |
| `pin_clip` | "keep this clip", "pin that" |
| `unpin_clip` | "unpin clip 5" |
| `delete_clip` | "delete clip 3" |
| `get_clip_stats` | "how many things have I copied today" |
| `clear_history` | "clear my clipboard history" |

---

## Configuration

Settings are created automatically at `~/.clipmcp/config.json` on first run.

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
| `poll_interval_ms` | `500` | How often to check the clipboard (ms) |
| `max_history_size` | `10000` | Max clips before pruning oldest |
| `auto_prune` | `true` | Automatically delete old clips |
| `prune_after_days` | `30` | Delete clips older than N days |
| `detect_sensitive` | `true` | Flag API keys, tokens, passwords |
| `categories_enabled` | `true` | Auto-categorise each clip |
| `max_clip_size_bytes` | `50000` | Truncate clips larger than 50 KB |
| `db_path` | `~/.clipmcp/history.db` | SQLite database location |

---

## Project structure

```
clipmcp/
├── pyproject.toml
├── README.md
├── CONTRIBUTING.md
├── src/
│   └── clipmcp/
│       ├── models.py
│       ├── config.py
│       ├── sensitive.py
│       ├── categorizer.py
│       ├── html_handler.py
│       ├── image_handler.py
│       ├── embeddings.py
│       ├── storage.py
│       ├── monitor.py
│       └── server.py
└── tests/
```

---

## Known limitations

- **Gaps when Claude is closed** — the monitor only runs while Claude Desktop is open
- **500 ms polling** — copying two things in very fast succession may miss the first
- **Source app is best-effort** — reflects the frontmost app at poll time, not the app that triggered the copy
- **Screenshot errors not yet categorised** — a screenshot of a stack trace is stored as `image`, not `error`. OCR support is on the roadmap

---

## Roadmap

| Version | Feature | Status |
|---|---|---|
| v1.0 | Text clipboard capture, SQLite storage, MCP tools | ✅ Done |
| v1.1 | Image clipboard capture (PNG/TIFF) | ✅ Done |
| v1.2 | HTML/rich text capture (web pages, Notion, Slack) | ✅ Done |
| v1.3 | Error-aware tagging (`error` category) | ✅ Done |
| v2.5 | Semantic search (local embeddings) | ✅ Done |
| v2.6 | Debug context bundling | ✅ Done |
| v2.7 | Selective encryption for sensitive clips | Planned |
| v2.8 | Menu bar UI | Planned |
| v2.9 | OCR on screenshot clips | Planned |
| v3.0 | MCP Resources | Planned |
| v3.1 | Windows / Linux support | Planned |

---

## Data privacy

- All data is stored locally in `~/.clipmcp/` — nothing leaves your machine
- Embeddings are computed locally using a bundled model
- Sensitive content is flagged and never shown to Claude in full unless you explicitly request it

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for architecture, design patterns, database schema, and how to add new features.

---

## License

MIT
