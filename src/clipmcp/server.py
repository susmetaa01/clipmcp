"""
server.py — MCP server and tool definitions for ClipMCP.

Design: Registry pattern for tools.

``ToolDefinition`` pairs a Tool's schema with its async handler in a single
dataclass.  ``ToolRegistry`` holds all definitions and dispatches calls.
Adding a new tool = instantiate one ``ToolDefinition`` and call
``registry.register()``.  ``list_tools()`` and ``call_tool()`` never need
to be edited again.

Exposes 9 MCP tools:
  get_recent_clips · search_clips · semantic_search · get_debug_context
  pin_clip · unpin_clip · delete_clip · get_clip_stats · clear_history

Sensitive clip behaviour:
  - is_sensitive=True clips show a warning and truncated preview by default
  - Full content only returned when full_content=True is explicitly passed
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import ImageContent, TextContent, Tool

from . import storage
from .embeddings import embed, embed_batch, is_available as embeddings_available, text_for_clip
from .html_handler import strip_html
from .image_handler import load_image_b64
from .models import ContentCategory, ContentType
from .monitor import monitor

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Type aliases
_ContentList = list[TextContent | ImageContent]
_ToolHandler = Callable[[dict[str, Any]], Awaitable[_ContentList]]

# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

app = Server("clipmcp")


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

@dataclass
class ToolDefinition:
    """
    Pairs an MCP tool's metadata (name, description, schema) with its
    async handler function so list_tools and call_tool can never fall out of sync.
    """

    name:         str
    description:  str
    input_schema: dict
    handler:      _ToolHandler

    def to_mcp_tool(self) -> Tool:
        return Tool(
            name=self.name,
            description=self.description,
            inputSchema=self.input_schema,
        )


class ToolRegistry:
    """
    Central registry of all ClipMCP tools.

    Usage::

        registry = ToolRegistry()
        registry.register(ToolDefinition(name="my_tool", ...))

        @app.list_tools()
        async def list_tools():
            return registry.list_tools()

        @app.call_tool()
        async def call_tool(name, arguments):
            return await registry.call(name, arguments)
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, tool: ToolDefinition) -> None:
        """Register a tool. Raises ValueError if the name is already taken."""
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' is already registered.")
        self._tools[tool.name] = tool

    def list_tools(self) -> list[Tool]:
        return [t.to_mcp_tool() for t in self._tools.values()]

    async def call(self, name: str, args: dict[str, Any]) -> _ContentList:
        if name not in self._tools:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]
        return await self._tools[name].handler(args)


registry = ToolRegistry()


# ---------------------------------------------------------------------------
# Shared formatting helpers
# ---------------------------------------------------------------------------

def _format_clip(clip: storage.Clip, full_content: bool = False) -> dict:
    """Format a Clip as a dict for JSON responses."""
    data = clip.to_dict()

    if clip.content_type == ContentType.HTML:
        data["content"] = strip_html(clip.content) if full_content else clip.content_preview

    if clip.is_sensitive:
        data["warning"] = "This clip contains potentially sensitive content."
        if not full_content:
            data["content"] = (
                    clip.content_preview[:50] +
                    "... [sensitive - request full_content=true to view]"
            )

    return data


def _build_clip_response(
        clips: list[storage.Clip],
        full_content: bool = False,
) -> _ContentList:
    """
    Build an MCP content list for a set of clips.
    Image clips are emitted as ImageContent; text/HTML clips are batched into JSON.
    """
    response: _ContentList = []
    text_clips: list[dict] = []

    def _flush_text() -> None:
        nonlocal text_clips
        if text_clips:
            response.append(TextContent(
                type="text",
                text=json.dumps({"clips": text_clips}, indent=2, default=str),
            ))
            text_clips = []

    for clip in clips:
        if clip.is_image and clip.file_path:
            _flush_text()
            img_b64 = load_image_b64(clip.file_path)
            if img_b64:
                response.append(TextContent(
                    type="text",
                    text=json.dumps({
                        "id":              clip.id,
                        "content_type":    "image",
                        "content_preview": clip.content_preview,
                        "source_app":      clip.source_app,
                        "created_at":      clip.created_at,
                    }, indent=2),
                ))
                response.append(ImageContent(type="image", data=img_b64, mimeType="image/png"))
            else:
                text_clips.append({**clip.to_dict(), "warning": "Image file not found on disk."})
        else:
            text_clips.append(_format_clip(clip, full_content=full_content))

    _flush_text()
    return response


# ---------------------------------------------------------------------------
# Tool handler implementations
# ---------------------------------------------------------------------------

async def _get_recent_clips(args: dict) -> _ContentList:
    count        = min(int(args.get("count", 10)), 50)
    category     = args.get("category")
    full_content = bool(args.get("full_content", False))

    clips = storage.get_recent(count=count, category=category, full_content=full_content)
    if not clips:
        return [TextContent(type="text", text="No clipboard history found.")]

    return [TextContent(type="text", text=f"Found {len(clips)} clip(s):")] + \
        _build_clip_response(clips, full_content=full_content)


async def _search_clips(args: dict) -> _ContentList:
    query = args.get("query", "")
    if not query:
        return [TextContent(type="text", text="Error: query is required.")]

    full_content = bool(args.get("full_content", False))
    clips = storage.search(
        query=query,
        category=args.get("category"),
        date_from=args.get("date_from"),
        date_to=args.get("date_to"),
        limit=int(args.get("limit", 20)),
        full_content=full_content,
    )
    if not clips:
        return [TextContent(type="text", text=f"No clips found matching '{query}'.")]

    return [TextContent(type="text", text=f"Found {len(clips)} clip(s) matching '{query}':")] + \
        _build_clip_response(clips, full_content=full_content)


async def _pin_clip(args: dict) -> _ContentList:
    clip_id = int(args["id"])
    if storage.pin_clip(clip_id):
        return [TextContent(type="text", text=f"Clip #{clip_id} pinned.")]
    return [TextContent(type="text", text=f"Clip #{clip_id} not found.")]


async def _unpin_clip(args: dict) -> _ContentList:
    clip_id = int(args["id"])
    if storage.unpin_clip(clip_id):
        return [TextContent(type="text", text=f"Clip #{clip_id} unpinned.")]
    return [TextContent(type="text", text=f"Clip #{clip_id} not found.")]


async def _delete_clip(args: dict) -> _ContentList:
    clip_id = int(args["id"])
    if storage.delete_clip(clip_id):
        return [TextContent(type="text", text=f"Clip #{clip_id} deleted.")]
    return [TextContent(type="text", text=f"Clip #{clip_id} not found.")]


async def _get_clip_stats(args: dict) -> _ContentList:
    stats = storage.get_stats()
    return [TextContent(type="text", text=json.dumps(stats, indent=2))]


async def _clear_history(args: dict) -> _ContentList:
    if not args.get("confirm"):
        return [TextContent(
            type="text",
            text="clear_history requires confirm=true. This will permanently delete clipboard history.",
        )]
    keep_pinned = bool(args.get("keep_pinned", True))
    deleted     = storage.clear_history(keep_pinned=keep_pinned)
    pinned_note = " Pinned clips were kept." if keep_pinned else ""
    return [TextContent(type="text", text=f"Deleted {deleted} clips.{pinned_note}")]


async def _semantic_search(args: dict) -> _ContentList:
    query = args.get("query", "").strip()
    if not query:
        return [TextContent(type="text", text="Error: query is required.")]

    if not embeddings_available():
        return [TextContent(
            type="text",
            text=(
                "Semantic search requires sentence-transformers.\n"
                "Install: pip install clipmcp[semantic]\n"
                "Then restart ClipMCP."
            ),
        )]

    limit        = int(args.get("limit", 10))
    threshold    = float(args.get("threshold", 0.3))
    category     = args.get("category")
    full_content = bool(args.get("full_content", False))

    # Backfill embeddings for clips added before v2.5
    pending = storage.get_clips_without_embeddings(limit=500)
    if pending:
        logger.info(f"Backfilling embeddings for {len(pending)} clips...")

        def _best_text(content: str, ctype: str, preview: str) -> str:
            if ctype == ContentType.HTML:
                return strip_html(content) or preview
            return text_for_clip(content, ctype, preview) or ""

        texts = [_best_text(c, ct, p) for _, c, ct, p in pending]
        vecs  = embed_batch(texts)
        for (clip_id, _, _, _), vec in zip(pending, vecs):
            if vec is not None:
                storage.store_embedding(clip_id, vec)
        logger.info("Backfill complete.")

    query_vec = embed(query)
    if query_vec is None:
        return [TextContent(type="text", text="Error: failed to embed query. Check logs.")]

    results = storage.semantic_search_by_vector(
        query_vec=query_vec,
        limit=limit,
        category=category,
        threshold=threshold,
        full_content=full_content,
    )

    if not results:
        return [TextContent(
            type="text",
            text=f"No clips found semantically matching '{query}' (threshold={threshold}).",
        )]

    formatted = []
    for clip, score in results:
        clip_dict = _format_clip(clip, full_content=full_content)
        clip_dict["similarity"] = round(score, 3)
        formatted.append(clip_dict)

    return [
        TextContent(type="text", text=f"Found {len(results)} clip(s) semantically matching '{query}':"),
        TextContent(type="text", text=json.dumps({"clips": formatted}, indent=2, default=str)),
    ]


async def _get_debug_context(args: dict) -> _ContentList:
    limit = min(int(args.get("limit", 10)), 20)
    clips = storage.get_recent(count=limit, full_content=True)

    if not clips:
        return [TextContent(type="text", text="No clipboard history found.")]

    errors  = [c for c in clips if c.category == ContentCategory.ERROR]
    code    = [c for c in clips if c.category == ContentCategory.CODE]
    rest    = [c for c in clips if c.category not in (ContentCategory.ERROR, ContentCategory.CODE)]
    ordered = errors + code + rest

    lines = [f"=== Debug Context - last {len(ordered)} clipboard item(s) ===\n"]

    for i, clip in enumerate(ordered, 1):
        label = f"[{i}] {clip.category.upper()}"
        if clip.source_app:
            label += f" - from {clip.source_app}"
        label += f" - {clip.created_at}"
        if clip.is_sensitive:
            label += " - sensitive"

        lines.append(label)
        lines.append("-" * len(label))

        if clip.content_type == ContentType.HTML:
            content = strip_html(clip.content)
        elif clip.is_sensitive:
            content = f"[sensitive content - {clip.char_count} chars]"
        else:
            content = clip.content

        lines.append(content.strip())
        lines.append("")

    return [TextContent(type="text", text="\n".join(lines))]


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

_CATEGORY_ENUM = [c.value for c in ContentCategory]

registry.register(ToolDefinition(
    name="get_recent_clips",
    description=(
        "Get the most recent clipboard entries. "
        "ALWAYS call this tool FIRST - before asking the user to provide content - whenever: "
        "(1) the user says 'analyse', 'summarise', 'explain', 'review', or 'help me with' "
        "something without pasting the content into the message; "
        "(2) they say 'I copied', 'I just copied', 'from what I copied', or 'the X I copied'; "
        "(3) they refer to a document, error, conversation, or case without attaching it; "
        "(4) they ask to debug something, mention an error, exception, or stack trace - "
        "use category='error' to find error clips instantly. "
        "Do NOT ask the user to paste or upload - check clipboard first. "
        "Always use full_content=true when reading or analysing content."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "count": {
                "type": "integer",
                "description": "Number of recent clips to return (default 10, max 50)",
                "default": 10,
            },
            "category": {
                "type": "string",
                "description": "Filter by category",
                "enum": _CATEGORY_ENUM,
            },
            "full_content": {
                "type": "boolean",
                "description": "Return full content instead of preview (default false)",
                "default": False,
            },
        },
    },
    handler=_get_recent_clips,
))

registry.register(ToolDefinition(
    name="search_clips",
    description=(
        "Search clipboard history by exact keyword or partial text match. "
        "ALWAYS call this tool when the user asks you to analyse, review, or summarise "
        "a specific named thing - an 'AWS case', 'the error', 'the Slack thread', "
        "'the document', 'the logs' - without pasting the content. "
        "Search for the topic keywords and use full_content=true to retrieve the full text. "
        "Never ask the user to paste or re-share something they may have already copied."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Text to search for in clipboard history",
            },
            "category": {
                "type": "string",
                "description": "Optionally filter by category",
                "enum": _CATEGORY_ENUM,
            },
            "date_from": {
                "type": "string",
                "description": "ISO date string to filter clips from (e.g. 2024-01-01)",
            },
            "date_to": {
                "type": "string",
                "description": "ISO date string to filter clips until (e.g. 2024-12-31)",
            },
            "limit": {
                "type": "integer",
                "description": "Max results to return (default 20)",
                "default": 20,
            },
            "full_content": {
                "type": "boolean",
                "description": "Return full content instead of preview (default false)",
                "default": False,
            },
        },
        "required": ["query"],
    },
    handler=_search_clips,
))

registry.register(ToolDefinition(
    name="semantic_search",
    description=(
        "Search clipboard history by meaning, not just exact keywords. "
        "ALWAYS call this tool when the user asks you to analyse, summarise, explain, or act on "
        "something they have not pasted into the message - even if they do not mention 'clipboard'. "
        "Trigger examples: 'analyse the AWS case', 'summarise the errors', "
        "'what does the report say', 'help me understand the issue', "
        "'the Slack thread about Grafana', 'what Mike said about the data'. "
        "Use the topic as the query. Always use full_content=true to get the actual content. "
        "Requires sentence-transformers (pip install clipmcp[semantic])."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural language description of what you are looking for",
            },
            "limit": {
                "type": "integer",
                "description": "Max results to return (default 10)",
                "default": 10,
            },
            "threshold": {
                "type": "number",
                "description": "Minimum similarity score 0-1 (default 0.3). Lower = more results.",
                "default": 0.3,
            },
            "category": {
                "type": "string",
                "description": "Optionally filter by category",
                "enum": [c for c in _CATEGORY_ENUM if c != ContentCategory.IMAGE],
            },
            "full_content": {
                "type": "boolean",
                "description": "Return full content instead of preview (default false)",
                "default": False,
            },
        },
        "required": ["query"],
    },
    handler=_semantic_search,
))

registry.register(ToolDefinition(
    name="get_debug_context",
    description=(
        "Fetch recent clipboard items bundled together as debug context. "
        "ALWAYS call this tool when the user says 'debug', 'fix this error', "
        "'what is wrong', 'help me understand this issue', 'I am getting an error', "
        "'it is not working', or any debugging/troubleshooting request - "
        "even if they have not pasted anything. "
        "Returns the last N clipboard items with full content, errors and code first, "
        "so Claude has the complete picture without the user needing to paste each item individually."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Number of recent clips to bundle (default 10, max 20)",
                "default": 10,
            },
        },
    },
    handler=_get_debug_context,
))

registry.register(ToolDefinition(
    name="pin_clip",
    description=(
        "Pin a clipboard entry so it will not be automatically pruned. "
        "The clip id comes from a previous get_recent_clips or search_clips call."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "id": {"type": "integer", "description": "The clip id to pin"},
        },
        "required": ["id"],
    },
    handler=_pin_clip,
))

registry.register(ToolDefinition(
    name="unpin_clip",
    description="Unpin a previously pinned clipboard entry, allowing it to be pruned normally.",
    input_schema={
        "type": "object",
        "properties": {
            "id": {"type": "integer", "description": "The clip id to unpin"},
        },
        "required": ["id"],
    },
    handler=_unpin_clip,
))

registry.register(ToolDefinition(
    name="delete_clip",
    description="Permanently delete a specific clipboard entry by id.",
    input_schema={
        "type": "object",
        "properties": {
            "id": {"type": "integer", "description": "The clip id to delete"},
        },
        "required": ["id"],
    },
    handler=_delete_clip,
))

registry.register(ToolDefinition(
    name="get_clip_stats",
    description=(
        "Get usage statistics - total clips, clips today, top categories, "
        "most used apps, and database size."
    ),
    input_schema={"type": "object", "properties": {}},
    handler=_get_clip_stats,
))

registry.register(ToolDefinition(
    name="clear_history",
    description=(
        "Delete all clipboard history. This is destructive and cannot be undone. "
        "Always confirm with the user before calling this. By default keeps pinned clips."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "confirm": {
                "type": "boolean",
                "description": "Must be explicitly set to true to proceed",
            },
            "keep_pinned": {
                "type": "boolean",
                "description": "Keep pinned clips (default true)",
                "default": True,
            },
        },
        "required": ["confirm"],
    },
    handler=_clear_history,
))


# ---------------------------------------------------------------------------
# MCP decorators - delegate entirely to the registry
# ---------------------------------------------------------------------------

@app.list_tools()
async def list_tools() -> list[Tool]:
    return registry.list_tools()


@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> _ContentList:
    try:
        return await registry.call(name, arguments)
    except Exception as exc:
        logger.error(f"Error calling tool '{name}': {exc}", exc_info=True)
        return [TextContent(type="text", text=f"Error: {exc}")]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def serve() -> None:
    """Start the clipboard monitor and run the MCP server over stdio."""
    monitor.start()
    logger.info("ClipMCP server started.")
    try:
        async with stdio_server() as (read_stream, write_stream):
            await app.run(read_stream, write_stream, app.create_initialization_options())
    finally:
        monitor.stop()
        logger.info("ClipMCP server stopped.")


def main() -> None:
    import asyncio
    asyncio.run(serve())


if __name__ == "__main__":
    main()
