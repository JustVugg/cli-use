"""Keep MCP servers hot in the background."""
from __future__ import annotations

import json
import os
import signal
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from cli_use import config
from cli_use.mcp_client import MCPClient, extract_text_content
from cli_use.registry import RegistryEntry, merged_registry


DAEMON_DIR = Path.home() / ".cli-use" / "daemons"


def _info_path(alias: str) -> Path:
    DAEMON_DIR.mkdir(parents=True, exist_ok=True)
    return DAEMON_DIR / f"{alias}.json"


def _resolve(alias: str) -> RegistryEntry | None:
    return merged_registry(config.load_aliases()).get(alias)


def _env_for(entry: RegistryEntry) -> dict[str, str]:
    env = dict(entry.env)
    for k in entry.env_required:
        if k not in os.environ and k not in env:
            # Nel daemon questo andrà spesso nel vuoto, ma è accettabile
            print(f"cli-use: warning: {entry.alias} needs env var {k}", file=sys.stderr)
    return env


# ------------------------------------------------------------------
# Client-side helpers (used by cli.py)
# ------------------------------------------------------------------

def is_running(alias: str) -> bool:
    p = _info_path(alias)
    if not p.exists():
        return False
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        port = data.get("port")
        # Health check: la porta risponde?
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except Exception:
        p.unlink(missing_ok=True)
        return False


def call_tool(alias: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    data = json.loads(_info_path(alias).read_text(encoding="utf-8"))
    port = data["port"]
    import urllib.request
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/call",
        data=json.dumps({"tool": tool, "arguments": arguments}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def start(alias: str) -> None:
    entry = _resolve(alias)
    if entry is None:
        raise SystemExit(f"Unknown alias {alias!r}")
    if is_running(alias):
        print(f"Daemon for {alias} already running.", file=sys.stderr)
        return

    # Spawn del processo daemon
    env = os.environ.copy()
    env["_CLI_USE_DAEMON_ALIAS"] = alias
    import subprocess
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    subprocess.Popen(
        [sys.executable, "-m", "cli_use.daemon", alias],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        creationflags=flags,
        env=env,
    )
    # Attendi che scriva il file info
    import time
    for _ in range(50):
        time.sleep(0.1)
        if is_running(alias):
            port = json.loads(_info_path(alias).read_text())["port"]
            print(f"Daemon for {alias} started on 127.0.0.1:{port}.")
            return
    print("Daemon failed to start.", file=sys.stderr)


def stop(alias: str) -> None:
    p = _info_path(alias)
    if not p.exists():
        print(f"No daemon for {alias}.", file=sys.stderr)
        return
    data = json.loads(p.read_text(encoding="utf-8"))
    pid = data["pid"]
    try:
        if sys.platform == "win32":
            os.kill(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    p.unlink(missing_ok=True)
    print(f"Daemon for {alias} stopped.")


def list_running() -> list[tuple[str, int]]:
    out = []
    for p in DAEMON_DIR.glob("*.json"):
        alias = p.stem
        if is_running(alias):
            data = json.loads(p.read_text(encoding="utf-8"))
            out.append((alias, data["port"]))
    return out


# ------------------------------------------------------------------
# Server side (runs inside the background process)
# ------------------------------------------------------------------

_daemon_client: MCPClient | None = None


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        if self.path != "/call":
            self.send_error(404)
            return
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n).decode())
        result = _daemon_call_tool(body["tool"], body.get("arguments", {}))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())


def _daemon_call_tool(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    global _daemon_client
    if _daemon_client is None:
        raise RuntimeError("MCP client not initialized")
    try:
        return _daemon_client.call_tool(tool, arguments)
    except Exception:
        # Tenta reconnect una volta
        _daemon_client.__exit__(None, None, None)
        entry = _resolve(_daemon_alias)
        cmd = entry.source.run_argv(entry.args) if entry else []
        env = _env_for(entry) if entry else {}
        _daemon_client = MCPClient(cmd, env=env)
        _daemon_client.__enter__()
        return _daemon_client.call_tool(tool, arguments)


_daemon_alias: str = ""


def _run_server(alias: str) -> None:
    global _daemon_alias, _daemon_client
    _daemon_alias = alias
    entry = _resolve(alias)
    if entry is None:
        sys.exit(1)

    cmd = entry.source.run_argv(entry.args)
    env = _env_for(entry)

    _daemon_client = MCPClient(cmd, env=env)
    _daemon_client.__enter__()

    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]

    info = {"pid": os.getpid(), "port": port, "alias": alias}
    _info_path(alias).write_text(json.dumps(info), encoding="utf-8")

    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    try:
        while True:
            threading.Event().wait(1)
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()
        if _daemon_client is not None:
            _daemon_client.__exit__(None, None, None)
        _info_path(alias).unlink(missing_ok=True)


if __name__ == "__main__":
    _run_server(sys.argv[1])