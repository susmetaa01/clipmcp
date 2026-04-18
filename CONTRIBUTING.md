# Contributing to ClipMCP

Thanks for your interest in contributing. This guide covers architecture, design patterns, database internals, and how to add new features.

---

## Getting started

```bash
git clone https://github.com/susmetaa01/clipmcp.git
cd clipmcp
pip install -e ".[dev]"
pytest tests/ -v   # all 96 tests should pass
```

---

## Architecture

Each module has a single responsibility and a named design pattern. New code should follow the same conventions.

| Module | Pattern | Responsibility |
|---|---|---|
| `models.py` | Value Object | `ContentType` / `ContentCategory` enums, frozen `Clip` dataclass |
| `config.py` | Singleton | Loads `~/.clipmcp/config.json`, exposes a single `config` object |
| `sensitive.py` | — | Regex-based credential detection |
| `categorizer.py` | Strategy | `CategoryRule` ABC + `RuleBasedCategorizer`; rules evaluated in priority order |
| `html_handler.py` | — | HTML → plain text stripping (stdlib only) |
| `image_handler.py` | — | PNG save/load, TIFF→PNG conversion via AppKit |
| `embeddings.py` | Service | `EmbeddingService` wraps lazy model loading; module-level functions delegate to a default instance |
| `storage.py` | Repository | `ClipRepository` owns all SQLite operations; module-level wrappers preserve the old API |
| `monitor.py` | Abstract Factory | `ClipboardReader` ABC with `MacOSClipboardReader` / `FallbackClipboardReader`; `ClipboardMonitor` holds a reader |
| `server.py` | Registry | `ToolDefinition` pairs schema + handler; `ToolRegistry` dispatches; `list_tools` / `call_tool` never need editing |

---

## Adding a new MCP tool

1. Write the async handler in `server.py`:
```python
async def _my_tool(args: dict) -> _ContentList:
    ...
    return [TextContent(type="text", text="result")]
```

2. Register it with the registry (one block, no other files need changing):
```python
registry.register(ToolDefinition(
    name="my_tool",
    description="...",
    input_schema={
        "type": "object",
        "properties": { ... },
        "required": [...],
    },
    handler=_my_tool,
))
```

---

## Adding a new content category

1. Add the value to `ContentCategory` in `models.py`:
```python
class ContentCategory(str, Enum):
    ...
    MY_CATEGORY = "my_category"
```

2. Add a rule class in `categorizer.py`:
```python
class MyRule(CategoryRule):
    priority = 3   # lower = higher priority; shift existing rules if needed
    category = ContentCategory.MY_CATEGORY

    def matches(self, content: str) -> bool:
        return "some_pattern" in content
```

3. Add it to `_DEFAULT_RULES` at the bottom of `categorizer.py`.

4. Add tests in `tests/test_categorizer.py`.

---

## Database schema

All history is stored at `~/.clipmcp/history.db`.

```bash
sqlite3 ~/.clipmcp/history.db
```

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER | Auto-increment primary key |
| `content` | TEXT | Full clipboard content (raw HTML for html clips) |
| `content_preview` | TEXT | First 100 chars — always plain text |
| `content_hash` | TEXT | SHA-256 hash used for consecutive-duplicate detection |
| `category` | TEXT | `error` · `code` · `url` · `email` · `path` · `sensitive` · `text` · `image` · `html` |
| `source_app` | TEXT | Frontmost app at copy time (best-effort) |
| `char_count` | INTEGER | Length of `content` |
| `is_pinned` | BOOLEAN | 1 = exempt from auto-pruning |
| `is_sensitive` | BOOLEAN | 1 = matched a credential pattern |
| `content_type` | TEXT | `text` · `html` · `image` |
| `file_path` | TEXT | Disk path for image clips (`~/.clipmcp/images/`) |
| `embedding` | BLOB | Float32 vector (384-dim) for semantic search; null if not installed |
| `created_at` | DATETIME | UTC timestamp |

Useful queries:

```sql
-- Recent clips
SELECT id, content_preview, category, source_app, created_at
FROM clipboard_history ORDER BY created_at DESC LIMIT 20;

-- Errors only
SELECT id, content_preview, created_at
FROM clipboard_history WHERE category = 'error' ORDER BY created_at DESC;

-- Pinned clips
SELECT id, content_preview, category FROM clipboard_history WHERE is_pinned = 1;

-- Count by category
SELECT category, COUNT(*) as n FROM clipboard_history GROUP BY category ORDER BY n DESC;

.quit
```

### Adding a schema migration

Add a new SQL string to `_MIGRATIONS` in `storage.py`. Migrations are applied in order; `OperationalError` means already applied and is silently skipped:

```python
_MIGRATIONS = [
    ...
    "ALTER TABLE clipboard_history ADD COLUMN my_new_column TEXT",
]
```

---

## Running tests

```bash
pytest tests/ -v          # all tests
pytest tests/ -v -k error # only tests matching "error"
```

Tests use `tmp_path` + `monkeypatch` to redirect the database to a temp file — no test ever touches `~/.clipmcp/history.db`.

---

## Branch protection & code ownership

All PRs require approval from `@susmetaa01` before merge (enforced via `.github/CODEOWNERS` and branch protection rules). Security-sensitive modules (`sensitive.py`, `storage.py`, `server.py`) have explicit ownership entries.
