#!/usr/bin/env python3
"""Tiny stdio MCP server used in cli-use tests & demos.

Exposes three tools:
- greet(name, shout=False) → "hello <name>"
- add(a, b) → a + b
- search_notes(query, limit=5) → mock search results

Implements just enough JSON-RPC 2.0 / MCP 2024-11-05 to be usable by any
MCP-compliant client.
"""
from __future__ import annotations

import json
import sys


TOOLS = [
    {
        "name": "greet",
        "description": "Greet someone by name.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Person to greet."},
                "shout": {"type": "boolean", "description": "Uppercase output."},
            },
            "required": ["name"],
        },
    },
    {
        "name": "add",
        "description": "Add two numbers.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "a": {"type": "number"},
                "b": {"type": "number"},
            },
            "required": ["a", "b"],
        },
    },
    {
        "name": "search_notes",
        "description": "Search mock notes by query string.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
    },
]


def _text(s: str) -> dict:
    return {"content": [{"type": "text", "text": s}]}


def _call(name: str, args: dict) -> dict:
    if name == "greet":
        msg = f"hello {args['name']}"
        return _text(msg.upper() if args.get("shout") else msg)
    if name == "add":
        return _text(str(args["a"] + args["b"]))
    if name == "search_notes":
        q = args["query"]
        limit = int(args.get("limit", 5))
        hits = [f"note {i}: mentions '{q}'" for i in range(1, limit + 1)]
        return _text("\n".join(hits))
    return {"content": [{"type": "text", "text": f"unknown tool {name}"}], "isError": True}


def _respond(id_, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": id_}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = req.get("method")
        rid = req.get("id")
        params = req.get("params") or {}

        if method == "initialize":
            _respond(rid, {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mock-mcp", "version": "0.0.1"},
            })
        elif method == "notifications/initialized":
            # notification; no response
            continue
        elif method == "tools/list":
            _respond(rid, {"tools": TOOLS})
        elif method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            try:
                result = _call(name, args)
                _respond(rid, result)
            except Exception as e:
                _respond(rid, error={"code": -32000, "message": str(e)})
        elif rid is not None:
            _respond(rid, error={"code": -32601, "message": f"method not found: {method}"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
