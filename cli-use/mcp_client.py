"""Minimal MCP (Model Context Protocol) stdio client.

Speaks JSON-RPC 2.0 over the child process's stdin/stdout. Enough to:
- initialize the session
- list tools (`tools/list`)
- call a tool (`tools/call`)

Intentionally dependency-free so cli-use stays a single lightweight install.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from queue import Queue, Empty
from typing import Any


PROTOCOL_VERSION = "2024-11-05"


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)


class MCPError(RuntimeError):
    pass


class MCPClient:
    """Stdio MCP client. Spawns the server as a subprocess and talks JSON-RPC."""

    def __init__(self, command: list[str], env: dict[str, str] | None = None, timeout: float = 30.0):
        self.command = command
        self.env = {**os.environ, **(env or {})}
        self.timeout = timeout
        self._proc: subprocess.Popen | None = None
        self._next_id = 1
        self._responses: Queue = Queue()
        self._stderr_buf: list[str] = []
        self._reader: threading.Thread | None = None
        self._err_reader: threading.Thread | None = None

    # ---- lifecycle ----

    def __enter__(self) -> "MCPClient":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def start(self) -> None:
        self._proc = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env,
            text=True,
            bufsize=1,
        )
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()
        self._err_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self._err_reader.start()
        self._initialize()

    def close(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.stdin.close()
            except Exception:
                pass
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2)
            except Exception:
                self._proc.kill()

    # ---- public API ----

    def list_tools(self) -> list[Tool]:
        result = self._request("tools/list", {})
        tools_raw = result.get("tools", [])
        return [
            Tool(
                name=t["name"],
                description=t.get("description", ""),
                input_schema=t.get("inputSchema", {}) or {},
            )
            for t in tools_raw
        ]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._request("tools/call", {"name": name, "arguments": arguments})

    # ---- internals ----

    def _initialize(self) -> None:
        self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "cli-use", "version": "0.0.1"},
            },
        )
        # MCP spec: notify initialized (no response expected)
        self._notify("notifications/initialized", {})

    def _send(self, payload: dict[str, Any]) -> None:
        assert self._proc and self._proc.stdin
        line = json.dumps(payload) + "\n"
        self._proc.stdin.write(line)
        self._proc.stdin.flush()

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        req_id = self._next_id
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})

        # drain until we see matching id
        deadline_reached = False
        while True:
            try:
                msg = self._responses.get(timeout=self.timeout)
            except Empty:
                deadline_reached = True
                break
            if msg.get("id") == req_id:
                if "error" in msg:
                    err = msg["error"]
                    raise MCPError(f"{method} failed: {err.get('message', err)}")
                return msg.get("result", {})
            # non-matching (notifications or other responses) — ignore

        if deadline_reached:
            stderr_tail = "".join(self._stderr_buf[-20:])
            raise MCPError(
                f"Timeout waiting for {method}. Server stderr:\n{stderr_tail}"
            )
        raise MCPError(f"Unexpected protocol state for {method}")

    def _read_stdout(self) -> None:
        assert self._proc and self._proc.stdout
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                self._stderr_buf.append(f"[stdout-non-json] {line}\n")
                continue
            # only put responses (have id); drop server-originated notifications
            if "id" in msg:
                self._responses.put(msg)

    def _read_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        for line in self._proc.stderr:
            self._stderr_buf.append(line)


def parse_command(cmd: str | list[str]) -> list[str]:
    if isinstance(cmd, list):
        return cmd
    return shlex.split(cmd)


def extract_text_content(call_result: dict[str, Any]) -> str:
    """Pull plain text out of an MCP tool-call result.

    MCP returns `content` as a list of typed blocks; for CLI use we flatten to
    the text blocks joined by newline. Non-text blocks are summarized so
    nothing is silently lost.
    """
    content = call_result.get("content", [])
    if not content:
        return ""
    parts: list[str] = []
    for block in content:
        btype = block.get("type")
        if btype == "text":
            parts.append(block.get("text", ""))
        elif btype == "image":
            parts.append(f"[image mime={block.get('mimeType', '?')} omitted]")
        elif btype == "resource":
            res = block.get("resource", {})
            parts.append(f"[resource uri={res.get('uri', '?')} omitted]")
        else:
            parts.append(f"[{btype or 'unknown'} block omitted]")
    return "\n".join(parts)
