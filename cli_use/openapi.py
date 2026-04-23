"""Export cached MCP schemas to OpenAPI 3.0."""
from __future__ import annotations

import json
from typing import Any

from cli_use import config
from cli_use.registry import merged_registry


def build_spec(aliases: list[str] | None = None) -> dict[str, Any]:
    reg = merged_registry(config.load_aliases())
    if aliases:
        reg = {a: reg[a] for a in aliases if a in reg}

    paths: dict[str, Any] = {}
    for alias, entry in reg.items():
        tools = config.read_cached_tools(alias) or []
        for raw in tools:
            name = raw.get("name", "unknown")
            schema = raw.get("inputSchema", {})
            paths[f"/{alias}/{name}"] = {
                "post": {
                    "summary": raw.get("description", f"{alias} — {name}"),
                    "operationId": f"{alias}_{name}",
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": schema}}
                    },
                    "responses": {
                        "200": {
                            "description": "Tool result",
                            "content": {"text/plain": {"schema": {"type": "string"}}}
                        }
                    }
                }
            }

    return {
        "openapi": "3.0.0",
        "info": {"title": "cli-use generated API", "version": "0.3.0"},
        "paths": paths,
    }