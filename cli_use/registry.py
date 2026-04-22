"""Registry of known MCP servers + source abstractions.

A `Source` knows how to install and spawn an MCP server. A `RegistryEntry`
binds an alias to a Source, a human description, and (at install time) the
positional args the server needs.

The built-in registry is a Python list — no YAML dependency. User-added
aliases live in ~/.cli-use/aliases.json.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any


BUILTIN: list[dict[str, Any]] = [
    {
        "alias": "fs",
        "name": "Filesystem",
        "description": "Read, write, list, and search files on disk.",
        "source": {"type": "npm", "package": "@modelcontextprotocol/server-filesystem", "binary": "mcp-server-filesystem"},
        "args_hint": "<allowed-directory>",
        "needs_args": True,
    },
    {
        "alias": "memory",
        "name": "Memory",
        "description": "Knowledge-graph memory: entities, relations, observations.",
        "source": {"type": "npm", "package": "@modelcontextprotocol/server-memory", "binary": "mcp-server-memory"},
        "needs_args": False,
    },
    {
        "alias": "gh",
        "name": "GitHub",
        "description": "Interact with GitHub: issues, PRs, repos, file contents.",
        "source": {"type": "npm", "package": "@modelcontextprotocol/server-github", "binary": "mcp-server-github"},
        "env_required": ["GITHUB_PERSONAL_ACCESS_TOKEN"],
        "needs_args": False,
    },
    {
        "alias": "git",
        "name": "Git",
        "description": "Git log, diff, status, blame. Requires a repository path.",
        "source": {"type": "pip", "package": "mcp-server-git", "binary": "mcp-server-git"},
        "args_hint": "--repository <path>",
        "needs_args": True,
    },
    {
        "alias": "sqlite",
        "name": "SQLite",
        "description": "Query and inspect SQLite databases.",
        "source": {"type": "pip", "package": "mcp-server-sqlite", "binary": "mcp-server-sqlite"},
        "args_hint": "--db-path <sqlite-file>",
        "needs_args": True,
    },
    {
        "alias": "time",
        "name": "Time",
        "description": "Current time and timezone conversions.",
        "source": {"type": "pip", "package": "mcp-server-time", "binary": "mcp-server-time"},
        "needs_args": False,
    },
    {
        "alias": "fetch",
        "name": "Fetch",
        "description": "Fetch a URL and return its HTML/markdown content.",
        "source": {"type": "pip", "package": "mcp-server-fetch", "binary": "mcp-server-fetch"},
        "needs_args": False,
    },
    {
        "alias": "puppeteer",
        "name": "Puppeteer",
        "description": "Headless-browser automation: navigate, screenshot, scrape.",
        "source": {"type": "npm", "package": "@modelcontextprotocol/server-puppeteer", "binary": "mcp-server-puppeteer"},
        "needs_args": False,
    },
    {
        "alias": "brave",
        "name": "Brave Search",
        "description": "Web search via Brave Search API.",
        "source": {"type": "npm", "package": "@modelcontextprotocol/server-brave-search", "binary": "mcp-server-brave-search"},
        "env_required": ["BRAVE_API_KEY"],
        "needs_args": False,
    },
    {
        "alias": "slack",
        "name": "Slack",
        "description": "Send messages, list channels, read history in Slack.",
        "source": {"type": "npm", "package": "@modelcontextprotocol/server-slack", "binary": "mcp-server-slack"},
        "env_required": ["SLACK_BOT_TOKEN", "SLACK_TEAM_ID"],
        "needs_args": False,
    },
]


@dataclass
class Source:
    """How to install and run an MCP server."""
    type: str                      # "npm" | "pip" | "local" | "git"
    package: str = ""              # for npm/pip
    binary: str = ""               # for npm/pip, expected on PATH after install
    command: str = ""              # for local: literal shell command
    url: str = ""                  # for git
    subdir: str = ""               # for git

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Source":
        return cls(**{k: d.get(k, "") for k in ("type", "package", "binary", "command", "url", "subdir")})

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in ("type", "package", "binary", "command", "url", "subdir") if getattr(self, k)}

    def is_installed(self) -> bool:
        if self.type == "local":
            return True
        if self.binary:
            return shutil.which(self.binary) is not None
        return False

    def install(self) -> None:
        if self.type == "npm":
            _run(["npm", "install", "-g", self.package])
        elif self.type == "pip":
            if shutil.which("pipx"):
                _run(["pipx", "install", self.package])
            else:
                _run([sys.executable, "-m", "pip", "install", "--user", self.package])
        elif self.type == "local":
            return
        else:
            raise RuntimeError(f"install not implemented for source type {self.type!r}")

    def run_argv(self, extra_args: list[str]) -> list[str]:
        """Return the argv to spawn this MCP server."""
        if self.type == "local":
            return shlex.split(self.command) + extra_args
        if not self.binary:
            raise RuntimeError(f"source of type {self.type} has no binary set")
        # Windows: risolve .cmd installati da npm
        binary = shutil.which(self.binary) or self.binary
        return [binary] + extra_args


def _run(cmd: list[str]) -> None:
    print(f"cli-use: running {' '.join(cmd)}", file=sys.stderr)
    # Windows: subprocess non trova .cmd senza path assoluto
    resolved = list(cmd)
    if resolved:
        found = shutil.which(resolved[0])
        if found:
            resolved[0] = found
    subprocess.run(resolved, check=True)


@dataclass
class RegistryEntry:
    alias: str
    name: str
    description: str
    source: Source
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    args_hint: str = ""
    needs_args: bool = False
    env_required: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RegistryEntry":
        return cls(
            alias=d["alias"],
            name=d.get("name", d["alias"]),
            description=d.get("description", ""),
            source=Source.from_dict(d["source"]),
            args=list(d.get("args", [])),
            env=dict(d.get("env", {})),
            args_hint=d.get("args_hint", ""),
            needs_args=bool(d.get("needs_args", False)),
            env_required=list(d.get("env_required", [])),
        )

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "alias": self.alias,
            "name": self.name,
            "description": self.description,
            "source": self.source.to_dict(),
        }
        if self.args:
            d["args"] = self.args
        if self.env:
            d["env"] = self.env
        if self.args_hint:
            d["args_hint"] = self.args_hint
        if self.needs_args:
            d["needs_args"] = True
        if self.env_required:
            d["env_required"] = self.env_required
        return d


def builtin_registry() -> dict[str, RegistryEntry]:
    return {e["alias"]: RegistryEntry.from_dict(e) for e in BUILTIN}


def merged_registry(user_entries: list[dict[str, Any]]) -> dict[str, RegistryEntry]:
    """Merge built-in with user-added aliases (user wins on alias collision)."""
    reg = builtin_registry()
    for e in user_entries:
        reg[e["alias"]] = RegistryEntry.from_dict(e)
    return reg


def parse_source_spec(spec: str) -> Source:
    """Parse a user-supplied spec like 'npm:@foo/bar', 'pip:mypkg', 'local:python server.py'."""
    if ":" not in spec:
        raise ValueError(f"invalid source spec {spec!r}; expected '<type>:<value>'")
    type_, value = spec.split(":", 1)
    type_ = type_.strip().lower()
    value = value.strip()
    if type_ in {"npm", "pip"}:
        # binary defaults to package name's last segment, user can override
        bin_guess = value.split("/")[-1]
        return Source(type=type_, package=value, binary=bin_guess)
    if type_ == "local":
        return Source(type="local", command=value)
    if type_ == "git":
        return Source(type="git", url=value)
    raise ValueError(f"unknown source type {type_!r}")
