#!/usr/bin/env python3
"""Rough token-cost comparison: MCP tool-use vs cli-use CLI.

We count tokens as characters / 4 (close to OpenAI/Anthropic BPE average).
This is deliberately approximate — the point is order of magnitude, not
accounting.

What we compare per-task (one agent turn):

    MCP path:
      context_in  = full tool schemas (what the agent must load to know about the tool)
                  + JSON tool-call request
      context_out = JSON tool-call result

    cli-use path:
      context_in  = one-line --help summary
                  + terse bash invocation
      context_out = plain stdout text
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from cli_use.mcp_client import MCPClient, extract_text_content


ROOT = Path(__file__).resolve().parent.parent
MOCK = ["python3", str(ROOT / "examples" / "mock_mcp_server.py")]


def tokens(s: str) -> int:
    return max(1, len(s) // 4)


def bench_mcp(tool: str, args: dict) -> tuple[int, int, str]:
    with MCPClient(MOCK) as c:
        schemas = c.list_tools()
        # the agent must load schemas of available tools into context
        schemas_blob = json.dumps([{
            "name": t.name,
            "description": t.description,
            "inputSchema": t.input_schema,
        } for t in schemas])
        request_blob = json.dumps({"name": tool, "arguments": args})
        result = c.call_tool(tool, args)
        result_blob = json.dumps(result)
    t_in = tokens(schemas_blob) + tokens(request_blob)
    t_out = tokens(result_blob)
    return t_in, t_out, extract_text_content(result)


def bench_cliuse(tool: str, args: dict) -> tuple[int, int, str]:
    # simulate: agent reads `--help` once to learn the tool, then issues the
    # shell invocation. help_blob is the terse summary our generated CLI shows.
    help_proc = subprocess.run(
        [sys.executable, "/tmp/mock_cli.py", "--help"],
        capture_output=True, text=True, check=True,
        env={**__import__("os").environ, "PYTHONPATH": str(ROOT)},
    )
    help_blob = help_proc.stdout
    flag_args = " ".join(f"--{k} {json.dumps(v) if not isinstance(v, bool) else ''}".strip()
                         for k, v in args.items() if not (isinstance(v, bool) and not v))
    shell_cmd = f"./mock_cli {tool} {flag_args}"
    out_proc = subprocess.run(
        [sys.executable, "/tmp/mock_cli.py", tool]
        + [arg for k, v in args.items()
           for arg in ([f"--{k}"] + ([] if isinstance(v, bool) else [str(v)]))
           if not (isinstance(v, bool) and not v)],
        capture_output=True, text=True,
        env={**__import__("os").environ, "PYTHONPATH": str(ROOT)},
    )
    stdout_blob = out_proc.stdout
    t_in = tokens(help_blob) + tokens(shell_cmd)
    t_out = tokens(stdout_blob)
    return t_in, t_out, stdout_blob.strip()


TASKS = [
    ("greet", {"name": "YC"}),
    ("add", {"a": 40, "b": 2}),
    ("search_notes", {"query": "mcp", "limit": 3}),
]


def main() -> None:
    print(f"{'task':<18}  {'mcp_in':>7} {'mcp_out':>8} {'mcp_tot':>8}  {'cli_in':>7} {'cli_out':>8} {'cli_tot':>8}  {'savings':>8}")
    print("-" * 92)
    tot_mcp = tot_cli = 0
    for tool, args in TASKS:
        mi, mo, _ = bench_mcp(tool, args)
        ci, co, _ = bench_cliuse(tool, args)
        mt, ct = mi + mo, ci + co
        tot_mcp += mt
        tot_cli += ct
        pct = 100 * (1 - ct / mt) if mt else 0
        label = f"{tool}({','.join(f'{k}={v}' for k,v in args.items())})"
        print(f"{label:<18}  {mi:>7} {mo:>8} {mt:>8}  {ci:>7} {co:>8} {ct:>8}  {pct:>7.1f}%")
    pct = 100 * (1 - tot_cli / tot_mcp) if tot_mcp else 0
    print("-" * 92)
    print(f"{'TOTAL':<18}  {'':>7} {'':>8} {tot_mcp:>8}  {'':>7} {'':>8} {tot_cli:>8}  {pct:>7.1f}%")

    # Amortized scenario: 10 calls per session, schemas/help loaded once.
    print()
    print("=== Amortized over 10 calls/session (schema + help loaded once) ===")
    # recompute: fixed context cost + per-call I/O cost
    with MCPClient(MOCK) as c:
        schemas = c.list_tools()
        schemas_blob = json.dumps([{
            "name": t.name, "description": t.description, "inputSchema": t.input_schema,
        } for t in schemas])
    help_proc = subprocess.run(
        [sys.executable, "/tmp/mock_cli.py", "--help"],
        capture_output=True, text=True, check=True,
        env={**__import__("os").environ, "PYTHONPATH": str(ROOT)},
    )
    fixed_mcp = tokens(schemas_blob)
    fixed_cli = tokens(help_proc.stdout)
    per_call_mcp = per_call_cli = 0
    for tool, args in TASKS:
        mi, mo, _ = bench_mcp(tool, args)
        ci, co, _ = bench_cliuse(tool, args)
        # subtract fixed cost to get per-call cost
        per_call_mcp += (mi - fixed_mcp) + mo
        per_call_cli += (ci - fixed_cli) + co
    avg_mcp_call = per_call_mcp / len(TASKS)
    avg_cli_call = per_call_cli / len(TASKS)
    for n in (1, 10, 50):
        tot_m = fixed_mcp + n * avg_mcp_call
        tot_c = fixed_cli + n * avg_cli_call
        print(f"  {n:>3} calls: mcp={tot_m:>7.0f} tok, cli-use={tot_c:>7.0f} tok, savings={100*(1-tot_c/tot_m):>5.1f}%")


if __name__ == "__main__":
    main()
