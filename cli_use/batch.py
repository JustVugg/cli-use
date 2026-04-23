"""Execute multiple cli-use calls with {{out:N}} substitution."""
from __future__ import annotations

import json
import re
import sys
from typing import Any

from cli_use.cli import _call_alias_raw
from cli_use.mcp_client import extract_text_content

_OUT_RE = re.compile(r"\{\{\s*out:(\d+)\s*\}\}")


def _resolve_args(arguments: dict[str, Any], outputs: list[str]) -> dict[str, Any]:
    def repl(v):
        if isinstance(v, str):
            for m in _OUT_RE.finditer(v):
                idx = int(m.group(1))
                v = v.replace(m.group(0), outputs[idx])
            return v
        if isinstance(v, dict):
            return {k: repl(x) for k, x in v.items()}
        if isinstance(v, list):
            return [repl(x) for x in v]
        return v
    return repl(arguments)


def run(spec_path: str | None, *, continue_on_error: bool = False, format: str = "text") -> int:
    raw = sys.stdin.read() if spec_path in (None, "-") else open(spec_path, "r", encoding="utf-8").read()
    jobs = json.loads(raw)
    if not isinstance(jobs, list):
        raise SystemExit("batch spec must be a JSON array")

    outputs: list[str] = []
    results: list[dict[str, Any]] = []

    for i, job in enumerate(jobs):
        alias = job["alias"]
        tool = job["tool"]
        arguments = _resolve_args(job.get("arguments", {}), outputs)

        try:
            result = _call_alias_raw(alias, tool, arguments)
        except Exception as e:
            print(f"batch [{i}] {alias}/{tool}: {e}", file=sys.stderr)
            if not continue_on_error:
                return 1
            outputs.append("")
            results.append({"error": str(e)})
            continue

        text = extract_text_content(result) if not result.get("isError") else ""
        if result.get("isError"):
            print(f"batch [{i}] {alias}/{tool}: {text}", file=sys.stderr)
            if not continue_on_error:
                return 1

        outputs.append(text)
        results.append({"alias": alias, "tool": tool, "output": text, "raw": result})

    if format == "json":
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        # text mode: print last non-empty output by default, or all with separators
        for r in results:
            if r.get("output"):
                print(r["output"])
    return 0