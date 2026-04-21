#!/usr/bin/env python3
"""Token benchmark against REAL MCP servers (not the mock).

Uses @modelcontextprotocol/server-filesystem as the reference case — 14 tools
with verbose natural-language descriptions, representative of a real
production MCP server.

Token counting stays the chars/4 approximation used in token_benchmark.py;
the interesting signal is order-of-magnitude, not exact pricing.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from cli_use.mcp_client import MCPClient, extract_text_content


ROOT = Path(__file__).resolve().parent.parent
FS_BIN = shutil.which("mcp-server-filesystem") or "mcp-server-filesystem"
FIXTURE = "/tmp/mcp-test"
MCP_CMD = [FS_BIN, FIXTURE]


def tokens(s: str) -> int:
    return max(1, len(s) // 4)


TASKS = [
    ("list_directory", {"path": FIXTURE}),
    ("read_text_file", {"path": f"{FIXTURE}/hello.txt"}),
    ("search_files", {"path": FIXTURE, "pattern": "*.txt"}),
    ("get_file_info", {"path": f"{FIXTURE}/hello.txt"}),
    ("list_allowed_directories", {}),
]


def collect_mcp_baseline():
    with MCPClient(MCP_CMD) as c:
        schemas = c.list_tools()
        schemas_blob = json.dumps([{
            "name": t.name,
            "description": t.description,
            "inputSchema": t.input_schema,
        } for t in schemas])
        results = []
        for name, args in TASKS:
            req_blob = json.dumps({"name": name, "arguments": args})
            res = c.call_tool(name, args)
            res_blob = json.dumps(res)
            results.append((name, req_blob, res_blob))
    return tokens(schemas_blob), results, schemas_blob


def collect_cliuse(out_cli: Path):
    # 1) build once
    subprocess.run(
        [sys.executable, "-m", "cli_use.cli", "convert", " ".join(MCP_CMD), "--out", str(out_cli)],
        check=True, capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    # 2) --help is what an agent loads once to learn the interface
    help_proc = subprocess.run(
        [sys.executable, str(out_cli), "--help"],
        capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    # --help may print to stdout or stderr depending on argparse path; take whichever we got
    help_blob = help_proc.stdout or help_proc.stderr
    # 3) per-task: shell command length + stdout
    per_task = []
    for name, args in TASKS:
        argv = [sys.executable, str(out_cli), name]
        for k, v in args.items():
            argv += [f"--{k}", str(v)]
        out = subprocess.run(
            argv, capture_output=True, text=True,
            env={**os.environ, "PYTHONPATH": str(ROOT)},
        )
        flag_str = " ".join(f"--{k} {v}" for k, v in args.items())
        shell_repr = f"./fs-cli {name} {flag_str}".strip()
        per_task.append((name, shell_repr, out.stdout))
    return tokens(help_blob), per_task, help_blob


def main():
    print("Benchmarking cli-use vs MCP against real @modelcontextprotocol/server-filesystem")
    print("=" * 88)

    fixed_mcp, mcp_results, mcp_schemas = collect_mcp_baseline()
    fixed_cli, cli_results, cli_help = collect_cliuse(Path("/tmp/fs_cli_bench.py"))

    print(f"Fixed context cost (loaded once per session):")
    print(f"  MCP tool schemas:    {fixed_mcp:>6} tokens  ({len(mcp_schemas):,} chars)")
    print(f"  cli-use --help:      {fixed_cli:>6} tokens  ({len(cli_help):,} chars)")
    print(f"  Per-session savings: {100*(1-fixed_cli/fixed_mcp):>5.1f}%")
    print()
    print(f"{'task':<28} {'mcp_req':>8} {'mcp_res':>8} {'cli_req':>8} {'cli_res':>8}  {'per-call savings':>18}")
    print("-" * 88)
    call_tot_mcp = call_tot_cli = 0
    for (n1, req_m, res_m), (n2, req_c, res_c) in zip(mcp_results, cli_results):
        assert n1 == n2
        mr, mo = tokens(req_m), tokens(res_m)
        cr, co = tokens(req_c), tokens(res_c)
        mt, ct = mr + mo, cr + co
        call_tot_mcp += mt
        call_tot_cli += ct
        pct = 100 * (1 - ct / mt) if mt else 0
        print(f"{n1:<28} {mr:>8} {mo:>8} {cr:>8} {co:>8}  {pct:>16.1f}%")
    print("-" * 88)
    print(f"{'per-call TOTAL':<28} {'':>8} {call_tot_mcp:>8} {'':>8} {call_tot_cli:>8}  {100*(1-call_tot_cli/call_tot_mcp):>16.1f}%")

    print()
    print("=== End-to-end session (fixed + N calls averaged) ===")
    avg_m = call_tot_mcp / len(TASKS)
    avg_c = call_tot_cli / len(TASKS)
    for n in (1, 5, 20, 100):
        tm = fixed_mcp + n * avg_m
        tc = fixed_cli + n * avg_c
        print(f"  {n:>3} calls: mcp={tm:>8.0f} tok, cli-use={tc:>8.0f} tok, savings={100*(1-tc/tm):>5.1f}%")


if __name__ == "__main__":
    main()
