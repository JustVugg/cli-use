"""Convert an MCP server into a standalone CLI.

Strategy: at "build" time, spawn the MCP server, call `tools/list`, and emit a
self-contained Python file that:
1. Defines an argparse parser with one subcommand per MCP tool
2. Opens a fresh MCPClient on each invocation, calls the requested tool, prints
   the flattened text output, and exits.

The generated file depends only on `cli_use.mcp_client`, so the resulting CLI
is as portable as cli-use itself.
"""
from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

from cli_use.mcp_client import MCPClient, Tool, parse_command


_FILE_HEADER = '''#!/usr/bin/env python3
"""Auto-generated CLI from an MCP server by cli-use.

Command wrapped: {command_repr}
"""
from __future__ import annotations

import argparse
import json
import sys

from cli_use.mcp_client import MCPClient, extract_text_content

_MCP_COMMAND = {command_literal}
_MCP_ENV = {env_literal}
'''


def _sanitize_flag(name: str) -> str:
    return name.replace("_", "-")


def _schema_type_to_argparse(prop: dict[str, Any]) -> dict[str, Any]:
    t = prop.get("type")
    if t == "string":
        return {"type": "str"}
    if t == "integer":
        return {"type": "int"}
    if t == "number":
        return {"type": "float"}
    if t == "boolean":
        return {"type": "bool"}
    if t == "array":
        item_type = (prop.get("items") or {}).get("type", "string")
        if item_type in {"string", "integer", "number"}:
            primitive = {"string": "str", "integer": "int", "number": "float"}[item_type]
            return {"type": primitive, "nargs": "*"}
        # array of objects (or anything non-primitive) → single JSON blob
        return {"type": "json"}
    # object or unknown → accept JSON string
    return {"type": "json"}


def _render_subcommand(tool: Tool) -> str:
    schema = tool.input_schema or {}
    props: dict[str, Any] = schema.get("properties", {}) or {}
    required: list[str] = schema.get("required", []) or []

    first_line = (tool.description or "").strip().split("\n", 1)[0]
    help_text = json.dumps(first_line)

    lines: list[str] = []
    lines.append(
        f"    sp = subs.add_parser({tool.name!r}, help={help_text}, description={help_text})"
    )

    for pname, pschema in props.items():
        flag = "--" + _sanitize_flag(pname)
        cfg = _schema_type_to_argparse(pschema)
        is_required = pname in required
        desc = (pschema.get("description") or "").strip().replace("\n", " ")
        help_json = json.dumps(desc[:120])
        kind = cfg["type"]
        if kind == "bool":
            lines.append(
                "    sp.add_argument("
                f"{flag!r}, action=argparse.BooleanOptionalAction, required={is_required}, "
                f"default={repr(pschema.get('default'))}, help={help_json})"
            )
        elif kind == "json":
            lines.append(
                f"    sp.add_argument({flag!r}, type=_json_arg, required={is_required}, help={help_json}, metavar='JSON')"
            )
        else:
            extra = ""
            if "nargs" in cfg:
                extra = f", nargs={cfg['nargs']!r}"
            type_ref = {"str": "str", "int": "int", "float": "float"}[kind]
            default_expr = "None" if is_required else repr(pschema.get("default"))
            lines.append(
                f"    sp.add_argument({flag!r}, type={type_ref}, required={is_required}, default={default_expr}{extra}, help={help_json})"
            )
    return "\n".join(lines)


def _render_argument_collection(tool: Tool) -> str:
    schema = tool.input_schema or {}
    props: dict[str, Any] = schema.get("properties", {}) or {}
    if not props:
        return "        args_dict = {}"
    entries = []
    for pname in props:
        attr = pname.replace("-", "_")
        entries.append(f"{pname!r}: getattr(args, {attr!r}, None)")
    body = ", ".join(entries)
    return (
        "        args_dict = {k: v for k, v in {"
        + body
        + "}.items() if v is not None}"
    )


def generate_cli_source(
    command: list[str],
    tools: list[Tool],
    env: dict[str, str] | None = None,
) -> str:
    """Produce the Python source of the generated CLI as a string."""
    command_repr = " ".join(command)
    header = _FILE_HEADER.format(
        command_repr=command_repr,
        command_literal=repr(command),
        env_literal=repr(env or {}),
    )

    sub_blocks = "\n\n".join(_render_subcommand(t) for t in tools)

    # Build the dispatcher `if/elif` chain
    dispatch_lines: list[str] = []
    first = True
    for t in tools:
        kw = "if" if first else "elif"
        first = False
        dispatch_lines.append(f"    {kw} args._tool == {t.name!r}:")
        dispatch_lines.append(_render_argument_collection(t))
        dispatch_lines.append(f"        name = {t.name!r}")
    dispatch_body = "\n".join(dispatch_lines) if dispatch_lines else (
        "    args_dict = {}\n    name = ''"
    )

    tool_summary = json.dumps(
        [{"name": t.name, "description": (t.description or "").strip()} for t in tools],
        ensure_ascii=False,
    )

    body = textwrap.dedent(
        '''
        _HELP_DESC_MAX = 70  # truncate tool descriptions in --help to stay agent-cheap


        def _json_arg(raw):
            try:
                return json.loads(raw)
            except json.JSONDecodeError as e:
                raise argparse.ArgumentTypeError(f"invalid JSON: {e}")


        def _compact_help(prog, tools):
            lines = [f"usage: {prog} <tool> [args]   ({len(tools)} tools)"]
            if not tools:
                return "\\n".join(lines) + "\\n"
            width = max(len(t["name"]) for t in tools)
            lines.append("")
            for t in tools:
                desc = (t.get("description") or "").strip().split("\\n", 1)[0]
                if len(desc) > _HELP_DESC_MAX:
                    desc = desc[: _HELP_DESC_MAX - 1] + "…"
                lines.append(f"  {t['name']:<{width}}  {desc}")
            lines.append("")
            lines.append(f"  {prog} <tool> --help       flags for a specific tool")
            lines.append(f"  {prog} --list-tools       machine-readable JSON")
            lines.append(f"  {prog} --completion bash  shell completion script")
            return "\\n".join(lines) + "\\n"


        def _completion_script(prog, tools):
            names = " ".join(t["name"] for t in tools)
            fn = "_" + "".join(c if c.isalnum() else "_" for c in prog)
            return (
                f"{fn}() {{\\n"
                f"  local cur=\\"${{COMP_WORDS[COMP_CWORD]}}\\"\\n"
                f"  if [ \\"$COMP_CWORD\\" -eq 1 ]; then\\n"
                f"    COMPREPLY=( $(compgen -W \\"{names} --list-tools --completion --help\\" -- \\"$cur\\") )\\n"
                f"  fi\\n"
                f"}}\\n"
                f"complete -F {fn} {prog}\\n"
            )


        def _build_parser():
            parser = argparse.ArgumentParser(
                prog=__PROG__,
                description="Auto-generated CLI wrapping an MCP server (via cli-use).",
                add_help=False,
            )
            parser.add_argument("-h", "--help", action="store_true", help="Show help and exit.")
            parser.add_argument("--list-tools", action="store_true", help="Print tools as JSON and exit.")
            parser.add_argument("--completion", choices=["bash"], help="Print shell completion script and exit.")
            subs = parser.add_subparsers(dest="_tool", metavar="TOOL")
        __SUBCOMMANDS__
            return parser


        _TOOLS = __TOOLS_JSON__


        def main(argv=None):
            parser = _build_parser()
            args, _ = parser.parse_known_args(argv)
            if args.help:
                print(_compact_help(parser.prog, _TOOLS))
                return 0
            if args.list_tools:
                print(json.dumps(_TOOLS, ensure_ascii=False))
                return 0
            if args.completion:
                print(_completion_script(parser.prog, _TOOLS), end="")
                return 0
            # re-parse strictly now that meta-flags are handled
            args = parser.parse_args(argv)
            if not args._tool:
                print(_compact_help(parser.prog, _TOOLS), file=sys.stderr)
                return 2
        __DISPATCH__
            with MCPClient(_MCP_COMMAND, env=_MCP_ENV) as client:
                try:
                    result = client.call_tool(name, args_dict)
                except Exception as e:
                    print(f"error: {e}", file=sys.stderr)
                    return 1
            if result.get("isError"):
                print(extract_text_content(result), file=sys.stderr)
                return 1
            text = extract_text_content(result)
            if text:
                print(text)
            return 0


        if __name__ == "__main__":
            sys.exit(main())
        '''
    )
    body = body.replace("__SUBCOMMANDS__", sub_blocks or "    pass")
    body = body.replace("__DISPATCH__", dispatch_body)
    body = body.replace("__TOOLS_JSON__", tool_summary)
    # Use sys.argv[0] basename as prog so the help shows the actual invocation
    body = body.replace("__PROG__", "__import__('os').path.basename(__import__('sys').argv[0])")

    return header + body


def convert_mcp_to_cli(
    command: str | list[str],
    out_path: str | Path,
    env: dict[str, str] | None = None,
) -> list[Tool]:
    """Introspect an MCP server and write a CLI to `out_path`.

    Returns the list of tools discovered.
    """
    cmd = parse_command(command)
    with MCPClient(cmd, env=env) as client:
        tools = client.list_tools()
    source = generate_cli_source(cmd, tools, env=env)
    out = Path(out_path)
    out.write_text(source, encoding="utf-8")
    try:
        out.chmod(0o755)
    except Exception:
        pass
    return tools
