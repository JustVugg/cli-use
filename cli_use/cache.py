"""JSON-disk cache with TTL for MCP tool results."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

_DIR = Path.home() / ".cli-use" / "cache"
_DIR.mkdir(parents=True, exist_ok=True)


def _key(alias: str, tool: str, arguments: dict) -> str:
    payload = json.dumps({"a": alias, "t": tool, "args": arguments}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def get(alias: str, tool: str, arguments: dict, ttl: int = 300) -> dict | None:
    path = _DIR / f"{_key(alias, tool, arguments)}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - data["timestamp"] > ttl:
            path.unlink(missing_ok=True)
            return None
        return data["result"]
    except Exception:
        return None


def set(alias: str, tool: str, arguments: dict, result: dict) -> None:
    path = _DIR / f"{_key(alias, tool, arguments)}.json"
    path.write_text(json.dumps({"timestamp": time.time(), "result": result}), encoding="utf-8")