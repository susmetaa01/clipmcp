"""
server.py — MCP server and tool definitions for ClipMCP.

Starts the clipboard monitor on launch and exposes 9 MCP tools:
  - get_recent_clips
  - search_clips
  - semantic_search
  - get_debug_context
  - pin_clip
  - unpin_clip
  - delete_clip
  - get_clip_stats
  - clear_history

Sensitive clip behaviour:
  - is_sensitive=True clips show a ⚠️ warning and truncated preview by default
  - Full content only returned when full_content=True is explicitly passed
"""

from __future__ import annotations

import json
import logging
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool, ImageContent

from . import storage
from .embeddings import embed, is_available as embeddings_available
from .embeddings import embed_batch, text_for_clip
from .html_handler import strip_html
from .image_handler import load_image_b64
from .monitor import monitor

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Server setup
# ---------------------------------------------------------------------------

app = Server("clipmcp")


# ---------------------------------------------------------------------------
# Tool: list_tools
# ---------------------------------------------------------------------------

@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="get_recent_clips",
            description=(
                "Get the most recent clipboard entries. "
                "ALWAYS call this tool FIRST — before asking the user to provide content — whenever: "
                "(1) the user says 'analyse', 'summarise', 'explain', 'review', or 'help me with' "
                "something without pasting the content into the message; "
                "(2) they say 'I copied', 'I just copied', 'from what I copied', or 'the X I copied'; "
                "(3) they refer to a document, error, conversation, or case without attaching it; "
                "(4) they ask to debug something, mention an error, exception, or stack trace — "
                "use category='error' to find error clips instantly. "
                "Do NOT ask the user to paste or upload — check clipboard first. "
                "Always use full_content=true when reading or analysing content."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "description": "Number of recent clips to return (default 10, max 50)",
                        "default": 10,
                    },
                    "category": {
                        "type": "string",
                        "description": "Filter by category: text, url, email, code, path, sensitive",
                        "enum": ["text", "url", "email", "code", "error", "path", "sensitive", "image", "html"],
                    },
                    "full_content": {
                        "type": "boolean",
                        "description": "Return full content instead of preview (default false)",
                        "default": False,
                    },
                },
            },
        ),
        Tool(
            name="search_clips",
            description=(
                "Search clipboard history by exact keyword or partial text match. "
                "ALWAYS call this tool when the user asks you to analyse, review, or summarise "
                "a specific named thing — an 'AWS case', 'the error', 'the Slack thread', "
                "'the document', 'the logs' — without pasting the content. "
                "Search for the topic keywords and use full_content=true to retrieve the full text. "
                "Never ask the user to paste or re-share something they may have already copied."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Text to search for in clipboard history",
                    },
                    "category": {
                        "type": "string",
                        "description": "Optionally filter by category: text, url, email, code, path, sensitive",
                        "enum": ["text", "url", "email", "code", "error", "path", "sensitive", "image", "html"],
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
        ),
        Tool(
            name="pin_clip",
            description=(
                "Pin a clipboard entry so it won't be automatically pruned. "
                "Use when the user wants to keep a specific clip permanently. "
                "The clip id comes from a previous get_recent_clips or search_clips call."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {
                        "type": "integer",
                        "description": "The clip id to pin",
                    },
                },
                "required": ["id"],
            },
        ),
        Tool(
            name="unpin_clip",
            description="Unpin a previously pinned clipboard entry, allowing it to be pruned normally.",
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {
                        "type": "integer",
                        "description": "The clip id to unpin",
                    },
                },
                "required": ["id"],
            },
        ),
        Tool(
            name="delete_clip",
            description=(
                "Permanently delete a specific clipboard entry. "
                "Use when the user wants to remove a specific clip from history."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {
                        "type": "integer",
                        "description": "The clip id to delete",
                    },
                },
                "required": ["id"],
            },
        ),
        Tool(
            name="get_clip_stats",
            description=(
                "Get usage statistics for clipboard history — total clips, clips today, "
                "top categories, most used apps, and database size. "
                "Use when the user asks about their clipboard usage."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="clear_history",
            description=(
                "Delete all clipboard history. This is destructive and cannot be undone. "
                "Always confirm with the user before calling this. "
                "By default keeps pinned clips."
            ),
            inputSchema={
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
        ),
        Tool(
            name="semantic_search",
            description=(
                "Search clipboard history by meaning, not just exact keywords. "
                "ALWAYS call this tool when the user asks you to analyse, summarise, explain, or act on "
                "something they haven't pasted into the message — even if they don't mention 'clipboard'. "
                "Trigger examples: 'analyse the AWS case', 'summarise the errors', "
                "'what does the report say', 'help me understand the issue', "
                "'the Slack thread about Grafana', 'what Mike said about the data'. "
                "Use the topic as the query. Always use full_content=true to get the actual content. "
                "Requires sentence-transformers (pip install clipmcp[semantic])."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language description of what you're looking for",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results to return (default 10)",
                        "default": 10,
                    },
                    "threshold": {
                        "type": "number",
                        "description": "Minimum similarity score 0–1 (default 0.3). Lower = more results but less relevant.",
                        "default": 0.3,
                    },
                    "category": {
                        "type": "string",
                        "description": "Optionally filter by category",
                        "enum": ["text", "url", "email", "code", "error", "path", "sensitive", "html"],
                    },
                    "full_content": {
                        "type": "boolean",
                        "description": "Return full content instead of preview (default false)",
                        "default": False,
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="get_debug_context",
            description=(
                "Fetch recent clipboard items bundled together as debug context. "
                "ALWAYS call this tool when the user says 'debug', 'fix this error', "
                "'what's wrong', 'help me understand this issue', 'I'm getting an error', "
                "'it's not working', or any debugging/troubleshooting request — "
                "even if they haven't pasted anything. "
                "Returns the last N clipboard items with full content, errors and code first, "
                "so Claude has the complete picture (error + stack trace + relevant code + logs) "
                "without the user needing to paste each item individually."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Number of recent clips to bundle (default 10, max 20)",
                        "default": 10,
                    },
                },
            },
        ),
    ]


# ---------------------------------------------------------------------------
# Tool: call_tool
# ---------------------------------------------------------------------------

@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    try:
        if name == "get_recent_clips":
            return await _get_recent_clips(arguments)
        elif name == "search_clips":
            return await _search_clips(arguments)
        elif name == "pin_clip":
            return await _pin_clip(arguments)
        elif name == "unpin_clip":
            return await _unpin_clip(arguments)
        elif name == "delete_clip":
            return await _delete_clip(arguments)
        elif name == "get_clip_stats":
            return await _get_clip_stats()
        elif name == "clear_history":
            return await _clear_history(arguments)
        elif name == "semantic_search":
            return await _semantic_search(arguments)
        elif name == "get_debug_context":
            return await _get_debug_context(arguments)
        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]
    except Exception as e:
        logger.error(f"Error calling tool {name}: {e}", exc_info=True)
        return [TextContent(type="text", text=f"Error: {str(e)}")]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _format_clip(clip: storage.Clip, full_content: bool = False) -> dict:
    """Format a Clip as a dict for JSON responses (text and HTML clips)."""
    data = clip.to_dict()

    # HTML clips: always return stripped plain text — never raw HTML.
    # - Default (full_content=False): return the 100-char content_preview
    # - full_content=True: strip the full raw HTML so Claude gets the entire conversation
    if clip.content_type == "html":
        if full_content:
            data["content"] = strip_html(clip.content)
        else:
            data["content"] = clip.content_preview
        data["content_type"] = "html"

    if clip.is_sensitive:
        data["warning"] = "⚠️ This clip contains potentially sensitive content."
        if not full_content:
            data["content"] = clip.content_preview[:50] + "… [sensitive — request full_content=true to view]"

    return data


def _build_clip_response(clips: list[storage.Clip], full_content: bool = False) -> list[TextContent | ImageContent]:
    """
    Build an MCP content list for a set of clips.
    Image clips are returned as ImageContent so Claude can see them visually.
    Text clips are returned as JSON in a single TextContent block.
    """
    response: list[TextContent | ImageContent] = []
    text_clips = []

    for clip in clips:
        if clip.is_image and clip.file_path:
            # First flush any accumulated text clips
            if text_clips:
                response.append(TextContent(
                    type="text",
                    text=json.dumps({"clips": text_clips}, indent=2, default=str)
                ))
                text_clips = []

            # Add image
            img_b64 = load_image_b64(clip.file_path)
            if img_b64:
                response.append(TextContent(
                    type="text",
                    text=json.dumps({
                        "id": clip.id,
                        "content_type": "image",
                        "content_preview": clip.content_preview,
                        "source_app": clip.source_app,
                        "created_at": clip.created_at,
                    }, indent=2)
                ))
                response.append(ImageContent(
                    type="image",
                    data=img_b64,
                    mimeType="image/png",
                ))
            else:
                text_clips.append({**clip.to_dict(), "warning": "⚠️ Image file not found on disk."})
        else:
            text_clips.append(_format_clip(clip, full_content=full_content))

    # Flush remaining text clips
    if text_clips:
        response.append(TextContent(
            type="text",
            text=json.dumps({"clips": text_clips}, indent=2, default=str)
        ))

    return response


async def _get_recent_clips(args: dict) -> list[TextContent | ImageContent]:
    count = min(int(args.get("count", 10)), 50)
    category = args.get("category")
    full_content = bool(args.get("full_content", False))

    clips = storage.get_recent(count=count, category=category, full_content=full_content)

    if not clips:
        return [TextContent(type="text", text="No clipboard history found.")]

    header = TextContent(type="text", text=f"Found {len(clips)} clip(s):")
    return [header] + _build_clip_response(clips, full_content=full_content)


async def _search_clips(args: dict) -> list[TextContent | ImageContent]:
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

    header = TextContent(type="text", text=f"Found {len(clips)} clip(s) matching '{query}':")
    return [header] + _build_clip_response(clips, full_content=full_content)


async def _pin_clip(args: dict) -> list[TextContent]:
    clip_id = int(args["id"])
    success = storage.pin_clip(clip_id)
    if success:
        return [TextContent(type="text", text=f"✅ Clip #{clip_id} pinned successfully.")]
    return [TextContent(type="text", text=f"❌ Clip #{clip_id} not found.")]


async def _unpin_clip(args: dict) -> list[TextContent]:
    clip_id = int(args["id"])
    success = storage.unpin_clip(clip_id)
    if success:
        return [TextContent(type="text", text=f"✅ Clip #{clip_id} unpinned successfully.")]
    return [TextContent(type="text", text=f"❌ Clip #{clip_id} not found.")]


async def _delete_clip(args: dict) -> list[TextContent]:
    clip_id = int(args["id"])
    success = storage.delete_clip(clip_id)
    if success:
        return [TextContent(type="text", text=f"✅ Clip #{clip_id} deleted.")]
    return [TextContent(type="text", text=f"❌ Clip #{clip_id} not found.")]


async def _get_clip_stats() -> list[TextContent]:
    stats = storage.get_stats()
    return [TextContent(type="text", text=json.dumps(stats, indent=2))]


async def _clear_history(args: dict) -> list[TextContent]:
    if not args.get("confirm"):
        return [TextContent(
            type="text",
            text="⚠️ clear_history requires confirm=true. This will permanently delete clipboard history."
        )]

    keep_pinned = bool(args.get("keep_pinned", True))
    deleted = storage.clear_history(keep_pinned=keep_pinned)
    pinned_note = " Pinned clips were kept." if keep_pinned else ""
    return [TextContent(
        type="text",
        text=f"✅ Deleted {deleted} clips.{pinned_note}"
    )]


async def _semantic_search(args: dict) -> list[TextContent]:
    query = args.get("query", "").strip()
    if not query:
        return [TextContent(type="text", text="Error: query is required.")]

    if not embeddings_available():
        return [TextContent(
            type="text",
            text=(
                "⚠️ Semantic search requires sentence-transformers.\n"
                "Install it with: pip install clipmcp[semantic]\n"
                "Then restart ClipMCP."
            )
        )]

    limit = int(args.get("limit", 10))
    threshold = float(args.get("threshold", 0.3))
    category = args.get("category")
    full_content = bool(args.get("full_content", False))

    # Backfill embeddings for any clips that don't have them yet
    # (existing clips added before v2.5, or while sentence-transformers was not installed)
    pending = storage.get_clips_without_embeddings(limit=500)
    if pending:
        logger.info(f"Backfilling embeddings for {len(pending)} clips...")
        # For HTML clips: strip the full raw HTML for embedding — NOT the 100-char DB preview
        def _best_text(content: str, ctype: str, preview: str) -> str:
            if ctype == "html":
                return strip_html(content) or preview
            return text_for_clip(content, ctype, preview) or ""
        texts = [_best_text(content, ctype, preview) for _, content, ctype, preview in pending]
        vecs = embed_batch(texts)
        for (clip_id, _, _, _), vec in zip(pending, vecs):
            if vec is not None:
                storage.store_embedding(clip_id, vec)
        logger.info("Backfill complete.")

    # Embed the query
    query_vec = embed(query)
    if query_vec is None:
        return [TextContent(type="text", text="Error: failed to embed query. Check logs.")]

    # Search
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
            text=f"No clips found semantically matching '{query}' (threshold={threshold})."
        )]

    # Format results — include similarity score alongside each clip
    formatted = []
    for clip, score in results:
        clip_dict = _format_clip(clip, full_content=full_content)
        clip_dict["similarity"] = round(score, 3)
        formatted.append(clip_dict)

    header = TextContent(
        type="text",
        text=f"Found {len(results)} clip(s) semantically matching '{query}':"
    )
    body = TextContent(
        type="text",
        text=json.dumps({"clips": formatted}, indent=2, default=str)
    )
    return [header, body]


async def _get_debug_context(args: dict) -> list[TextContent]:
    limit = min(int(args.get("limit", 10)), 20)

    clips = storage.get_recent(count=limit, full_content=True)
    if not clips:
        return [TextContent(type="text", text="No clipboard history found.")]

    # Prioritise: errors first, then code, then everything else (preserve recency within each group)
    _PRIORITY = {"error": 0, "code": 1}
    errors  = [c for c in clips if c.category == "error"]
    code    = [c for c in clips if c.category == "code"]
    rest    = [c for c in clips if c.category not in ("error", "code")]
    ordered = errors + code + rest

    # Build a single readable context block
    lines = [f"=== Debug Context — last {len(ordered)} clipboard item(s) ===\n"]

    for i, clip in enumerate(ordered, 1):
        # Timestamp relative label
        label = f"[{i}] {clip.category.upper()}"
        if clip.source_app:
            label += f" · from {clip.source_app}"
        label += f" · {clip.created_at}"
        if clip.is_sensitive:
            label += " · ⚠️ sensitive"

        lines.append(label)
        lines.append("-" * len(label))

        # Content: for HTML use stripped text (already done by _format_clip via full_content),
        # for sensitive show warning, for everything else show full content
        if clip.content_type == "html":
            content = strip_html(clip.content)
        elif clip.is_sensitive:
            content = f"[sensitive content — {clip.char_count} chars]"
        else:
            content = clip.content

        lines.append(content.strip())
        lines.append("")  # blank line between items

    return [TextContent(type="text", text="\n".join(lines))]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def serve() -> None:
    """Start the monitor and run the MCP server over stdio."""
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
