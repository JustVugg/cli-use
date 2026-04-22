"""Persistent config for cli-use.

Stored under `~/.cli-use/` (overridable via `CLI_USE_HOME`):

    ~/.cli-use/
      aliases.json      # user-added alias definitions (RegistryEntry dicts)
      cache/<alias>.json  # cached tools/list per alias (optional)
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def config_dir() -> Path:
    override = os.environ.get("CLI_USE_HOME")
    if override:
        return Path(override)
    return Path.home() / ".cli-use"


def ensure_dir() -> Path:
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def aliases_path() -> Path:
    return config_dir() / "aliases.json"


def load_aliases() -> list[dict]:
    path = aliases_path()
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return []


def save_aliases(aliases: list[dict]) -> None:
    ensure_dir()
    aliases_path().write_text(json.dumps(aliases, indent=2, ensure_ascii=False) + "\n")


def upsert_alias(entry: dict) -> None:
    aliases = load_aliases()
    aliases = [a for a in aliases if a.get("alias") != entry["alias"]]
    aliases.append(entry)
    save_aliases(aliases)


def remove_alias(alias: str) -> bool:
    aliases = load_aliases()
    new = [a for a in aliases if a.get("alias") != alias]
    if len(new) == len(aliases):
        return False
    save_aliases(new)
    return True


def cache_dir() -> Path:
    d = config_dir() / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cached_tools_path(alias: str) -> Path:
    return cache_dir() / f"{alias}.json"


def read_cached_tools(alias: str) -> list[dict] | None:
    path = cached_tools_path(alias)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def write_cached_tools(alias: str, tools: list[dict]) -> None:
    cached_tools_path(alias).write_text(
        json.dumps(tools, indent=2, ensure_ascii=False) + "\n"
    )
