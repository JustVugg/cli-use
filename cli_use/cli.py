"""`cli-use` command-line entrypoint.

Two usage styles:

  1) High-level (preferred):
       cli-use add fs /tmp              # install + register + emit skill
       cli-use fs list_directory --path /tmp
       cli-use list
       cli-use remove fs

  2) Low-level (explicit MCP command):
       cli-use convert "<mcp-cmd>" --out ./foo-cli.py
       cli-use run "<mcp-cmd>" <tool> --arguments '{...}'
       cli-use mcp-list "<mcp-cmd>"
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from cli_use import config
from cli_use.convert import convert_mcp_to_cli
from cli_use.mcp_client import MCPClient, Tool, extract_text_content, parse_command
from cli_use.registry import (
    RegistryEntry,
    Source,
    builtin_registry,
    merged_registry,
    parse_source_spec,
)
from cli_use.skill import ToolSpec, emit_skill, update_agents_md
from cli_use import daemon


META_SUBCOMMANDS = {"add", "remove", "list", "search", "convert", "run", "mcp-list", "help", "--help", "-h", "batch", "openapi", "completions"}

def _parse_env_flags(pairs: list[str] | None) -> dict[str, str]:
    env: dict[str, str] = {}
    for p in pairs or []:
        if "=" not in p:
            raise SystemExit(f"invalid --env value {p!r}, expected KEY=VALUE")
        k, v = p.split("=", 1)
        env[k] = v
    return env


def _resolve_alias(alias: str) -> RegistryEntry | None:
    return merged_registry(config.load_aliases()).get(alias)


def _collect_env(entry: RegistryEntry) -> dict[str, str]:
    env = dict(entry.env)
    for k in entry.env_required:
        if k not in os.environ and k not in env:
            print(
                f"cli-use: warning: {alias_name(entry)} needs env var {k}; set it before invoking.",
                file=sys.stderr,
            )
    return env


def alias_name(entry: RegistryEntry) -> str:
    return entry.alias


def _fetch_tools(cmd: list[str], env: dict[str, str]) -> list[Tool]:
    with MCPClient(cmd, env=env) as c:
        return c.list_tools()


def _tool_to_dict(tool: Tool) -> dict[str, object]:
    return {
        "name": tool.name,
        "description": tool.description,
        "inputSchema": tool.input_schema,
    }


def _tool_from_dict(raw: dict[str, object]) -> Tool:
    return Tool(
        name=str(raw["name"]),
        description=str(raw.get("description", "")),
        input_schema=raw.get("inputSchema", {}) or {},
    )


def _read_cached_tools(alias: str) -> list[Tool] | None:
    cached = config.read_cached_tools(alias)
    if cached is None:
        return None
    return [_tool_from_dict(item) for item in cached if isinstance(item, dict) and item.get("name")]


def _write_cached_tools(alias: str, tools: list[Tool]) -> None:
    config.write_cached_tools(alias, [_tool_to_dict(tool) for tool in tools])


def _get_tools(
    entry: RegistryEntry,
    mcp_cmd: list[str],
    env: dict[str, str],
    *,
    prefer_cache: bool = True,
    refresh: bool = False,
) -> list[Tool]:
    if prefer_cache and not refresh:
        cached = _read_cached_tools(entry.alias)
        if cached is not None:
            return cached

    tools = _fetch_tools(mcp_cmd, env)
    _write_cached_tools(entry.alias, tools)
    return tools


# --------------------------------------------------------------------------
# High-level: alias dispatch
# --------------------------------------------------------------------------

def _call_alias_raw(alias: str, tool_name: str, arguments: dict) -> dict:
    """Chiamata grezza riusabile da batch.py e _dispatch_alias."""
    entry = _resolve_alias(alias)
    if entry is None:
        raise ValueError(f"unknown alias {alias!r}")
    if not entry.source.is_installed():
        raise RuntimeError(f"{alias!r} is not installed")
    mcp_cmd = entry.source.run_argv(entry.args)
    env = _collect_env(entry)
    try:
        result = _call_alias_raw(alias, tool_name, arguments)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def _dispatch_alias(alias: str, rest: list[str]) -> int:
    entry = _resolve_alias(alias)
    if entry is None:
        print(
            f"cli-use: unknown alias {alias!r}. Try `cli-use list`, "
            f"`cli-use search {alias}`, or `cli-use add {alias}`.",
            file=sys.stderr,
        )
        return 1

    if not entry.source.is_installed():
        print(
            f"cli-use: {alias!r} is not installed. Run `cli-use add {alias}` first.",
            file=sys.stderr,
        )
        return 1

    mcp_cmd = entry.source.run_argv(entry.args)
    env = _collect_env(entry)

    # No sub-tool given: show a compact help from cached schemas if possible.
    if not rest or rest[0] in ("-h", "--help"):
        return _show_alias_help(entry, mcp_cmd, env)

    if rest[0] == "--list-tools":
        tools = _get_tools(entry, mcp_cmd, env, prefer_cache=True)
        print(json.dumps(
            [{"name": t.name, "description": t.description} for t in tools],
            ensure_ascii=False,
        ))
        return 0

    tool_name = rest[0]
    tool_args = rest[1:]

    tools = _get_tools(entry, mcp_cmd, env, prefer_cache=True)
    tool = next((t for t in tools if t.name == tool_name), None)
    if tool is None:
        tools = _get_tools(entry, mcp_cmd, env, prefer_cache=False, refresh=True)
        tool = next((t for t in tools if t.name == tool_name), None)
    if tool is None:
        available = ", ".join(t.name for t in tools) or "(none)"
        print(
            f"cli-use: tool {tool_name!r} not found in {alias!r}. Available: {available}",
            file=sys.stderr,
        )
        return 1

    # Build an argparse parser for this tool on the fly.
    parser = _parser_for_tool(alias, tool)
    if tool_args and tool_args[0] in ("-h", "--help"):
        parser.print_help()
        return 0
    ns = parser.parse_args(tool_args)
    arguments = {k: v for k, v in vars(ns).items() if v is not None}

    if daemon.is_running(alias):
        result = daemon.call_tool(alias, tool_name, arguments)
    else:
        with MCPClient(mcp_cmd, env=env) as client:
            try:
                result = client.call_tool(tool_name, arguments)
            except Exception as e:
                print(f"error: {e}", file=sys.stderr)
                return 1

    # Processa risultato (condiviso)
    if result.get("isError"):
        print(extract_text_content(result), file=sys.stderr)
        return 1
    text = extract_text_content(result)
    if text:
        print(text)
    return 0


def _show_alias_help(entry: RegistryEntry, mcp_cmd: list[str], env: dict[str, str]) -> int:
    try:
        tools = _get_tools(entry, mcp_cmd, env, prefer_cache=True)
    except Exception as e:
        print(f"{entry.alias}: failed to list tools ({e})", file=sys.stderr)
        return 1
    print(f"usage: cli-use {entry.alias} <tool> [args]   ({len(tools)} tools)")
    if tools:
        width = max(len(t.name) for t in tools)
        print()
        for t in tools:
            first = (t.description or "").strip().split("\n", 1)[0]
            if len(first) > 70:
                first = first[:69] + "…"
            print(f"  {t.name:<{width}}  {first}")
        print()
        print(f"  cli-use {entry.alias} <tool> --help    flags for a specific tool")
        print(f"  cli-use {entry.alias} --list-tools     machine-readable JSON")
    return 0


def _parser_for_tool(alias: str, tool: Tool) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"cli-use {alias} {tool.name}",
        description=(tool.description or "").strip().split("\n", 1)[0] or None,
    )
    schema = tool.input_schema or {}
    props = schema.get("properties", {}) or {}
    required = set(schema.get("required", []) or [])
    for pname, pschema in props.items():
        t = pschema.get("type")
        desc = (pschema.get("description") or "").strip().split("\n", 1)[0]
        flag = "--" + pname
        is_req = pname in required
        if t == "boolean":
            parser.add_argument(
                flag,
                action=argparse.BooleanOptionalAction,
                required=is_req,
                default=pschema.get("default"),
                help=desc or None,
            )
        elif t == "integer":
            parser.add_argument(flag, type=int, required=is_req, help=desc or None,
                                default=pschema.get("default"))
        elif t == "number":
            parser.add_argument(flag, type=float, required=is_req, help=desc or None,
                                default=pschema.get("default"))
        elif t == "array":
            item_t = (pschema.get("items") or {}).get("type", "string")
            if item_t in {"string", "integer", "number"}:
                prim = {"string": str, "integer": int, "number": float}[item_t]
                parser.add_argument(flag, type=prim, nargs="*", required=is_req,
                                    default=pschema.get("default"), help=desc or None)
            else:
                parser.add_argument(flag, type=_json_arg, required=is_req,
                                    default=pschema.get("default"), metavar="JSON",
                                    help=desc or None)
        elif t == "object" or t is None:
            parser.add_argument(flag, type=_json_arg, required=is_req,
                                default=pschema.get("default"), metavar="JSON",
                                help=desc or None)
        else:
            parser.add_argument(flag, type=str, required=is_req,
                                default=pschema.get("default"), help=desc or None)
    return parser


def _json_arg(raw: str):
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise argparse.ArgumentTypeError(f"invalid JSON: {e}")


# --------------------------------------------------------------------------
# Sub-commands: add, remove, list, search, convert, run, mcp-list
# --------------------------------------------------------------------------


def _cmd_add(args: argparse.Namespace) -> int:
    alias = args.alias
    registry = builtin_registry()
    entry: RegistryEntry | None = registry.get(alias)

    if args.source:
        src = parse_source_spec(args.source)
        entry = RegistryEntry(
            alias=alias,
            name=args.name or alias,
            description=args.description or f"User-added {alias} MCP server",
            source=src,
            args=list(args.args or []),
            env={},
        )
    elif entry is None:
        print(
            f"cli-use: {alias!r} is not in the built-in registry. Provide --from <type:value>.",
            file=sys.stderr,
        )
        return 1
    else:
        entry.args = list(args.args or entry.args)
        if entry.needs_args and not entry.args:
            print(
                f"cli-use: {alias!r} needs positional args ({entry.args_hint or '<args>'}). "
                f"Example: cli-use add {alias} /some/path",
                file=sys.stderr,
            )
            return 1

    # Install if not present
    if not entry.source.is_installed():
        try:
            entry.source.install()
        except Exception as e:
            print(f"cli-use: install failed for {alias}: {e}", file=sys.stderr)
            return 1

    # Probe tools for skill generation
    env = _collect_env(entry)
    try:
        tools = _fetch_tools(entry.source.run_argv(entry.args), env)
    except Exception as e:
        print(f"cli-use: warning: could not probe {alias} tools ({e}); skill not emitted.", file=sys.stderr)
        tools = []

    # Persist alias
    config.upsert_alias(entry.to_dict())
    if tools:
        _write_cached_tools(alias, tools)

    # Emit skill + AGENTS.md unless suppressed
    if not args.no_skill and tools:
        tool_specs = [ToolSpec(name=t.name, description=t.description, input_schema=t.input_schema) for t in tools]
        skill_dir = emit_skill(
            alias=alias,
            description=entry.description,
            tools=tool_specs,
            skills_root=args.skills_dir,
            binary=f"cli-use {alias}",
        )
        update_agents_md(
            alias=alias,
            description=entry.description,
            tools=tool_specs,
            binary=f"cli-use {alias}",
            agents_path=args.agents_file,
        )
        print(f"cli-use: emitted skill → {skill_dir}", file=sys.stderr)

    print(f"cli-use: {alias!r} installed ({len(tools)} tools)", file=sys.stderr)
    return 0


def _cmd_remove(args: argparse.Namespace) -> int:
    if config.remove_alias(args.alias):
        print(f"cli-use: removed alias {args.alias!r}", file=sys.stderr)
        return 0
    print(f"cli-use: alias {args.alias!r} not found", file=sys.stderr)
    return 1


def _cmd_list(args: argparse.Namespace) -> int:
    registry = merged_registry(config.load_aliases())
    if not registry:
        print("(no aliases)")
        return 0
    if args.format == "json":
        out = [
            {**e.to_dict(), "installed": e.source.is_installed()}
            for e in registry.values()
        ]
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0
    # Table
    width = max(len(a) for a in registry) if registry else 4
    for alias in sorted(registry):
        e = registry[alias]
        marker = "✓" if e.source.is_installed() else " "
        first = (e.description or "").strip().split("\n", 1)[0]
        if len(first) > 60:
            first = first[:59] + "…"
        print(f"  {marker}  {alias:<{width}}  {first}")
    return 0


def _cmd_search(args: argparse.Namespace) -> int:
    q = args.query.lower()
    registry = merged_registry(config.load_aliases())
    hits = [
        e for e in registry.values()
        if q in e.alias.lower() or q in (e.name or "").lower() or q in (e.description or "").lower()
    ]
    if not hits:
        print(f"(no matches for {args.query!r})")
        return 0
    for e in hits:
        first = (e.description or "").strip().split("\n", 1)[0]
        print(f"  {e.alias}  —  {first}")
    return 0


# ---- Low-level legacy commands (kept for power users) -------------------


def _cmd_convert(args: argparse.Namespace) -> int:
    cmd = parse_command(args.mcp_command)
    env = _parse_env_flags(args.env)
    out = Path(args.out)
    tools = convert_mcp_to_cli(cmd, out, env=env)
    names = ", ".join(t.name for t in tools) or "(none)"
    print(f"cli-use: wrote {out} with {len(tools)} tool(s): {names}", file=sys.stderr)

    if args.emit_skill and tools:
        alias = args.alias or out.stem.replace("_cli", "").replace("-cli", "") or "mcp"
        tool_specs = [ToolSpec(t.name, t.description, t.input_schema) for t in tools]
        skill_dir = emit_skill(
            alias=alias,
            description=args.description or f"Generated CLI for {args.mcp_command}",
            tools=tool_specs,
            skills_root=args.skills_dir,
            binary=str(out),
        )
        update_agents_md(
            alias=alias,
            description=args.description or f"Generated CLI for {args.mcp_command}",
            tools=tool_specs,
            binary=str(out),
            agents_path=args.agents_file,
        )
        print(f"cli-use: emitted skill → {skill_dir}", file=sys.stderr)
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    cmd = parse_command(args.mcp_command)
    env = _parse_env_flags(args.env)
    try:
        arguments = json.loads(args.arguments) if args.arguments else {}
    except json.JSONDecodeError as e:
        print(f"error: --arguments must be valid JSON ({e})", file=sys.stderr)
        return 2
    with MCPClient(cmd, env=env) as client:
        result = client.call_tool(args.tool, arguments)
    if result.get("isError"):
        print(extract_text_content(result), file=sys.stderr)
        return 1
    text = extract_text_content(result)
    if text:
        print(text)
    return 0


def _cmd_mcp_list(args: argparse.Namespace) -> int:
    cmd = parse_command(args.mcp_command)
    env = _parse_env_flags(args.env)
    with MCPClient(cmd, env=env) as client:
        tools = client.list_tools()
    if args.format == "json":
        print(json.dumps(
            [{"name": t.name, "description": t.description, "inputSchema": t.input_schema} for t in tools],
            ensure_ascii=False,
        ))
    else:
        for t in tools:
            first = (t.description or "").strip().split("\n", 1)[0]
            print(f"{t.name}\t{first}")
    return 0


# --------------------------------------------------------------------------
# Entry
# --------------------------------------------------------------------------


def _build_subparser(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="_cmd", metavar="COMMAND")

    p = sub.add_parser("add", help="Install and register an MCP server as an alias.")
    p.add_argument("alias", help="Short alias (e.g. fs, gh).")
    p.add_argument("args", nargs="*", help="Positional args for the MCP server (e.g. a root path).")
    p.add_argument("--from", dest="source", default=None,
                   help="Custom source: npm:<pkg>, pip:<pkg>, local:'<command>', git:<url>")
    p.add_argument("--name", default=None, help="Human name for custom sources.")
    p.add_argument("--description", default=None, help="Description for custom sources.")
    p.add_argument("--no-skill", action="store_true", help="Do not emit SKILL.md / AGENTS.md.")
    p.add_argument("--skills-dir", default="skills", help="Where to write skills (default: ./skills)")
    p.add_argument("--agents-file", default="AGENTS.md", help="Path to AGENTS.md (default: ./AGENTS.md)")
    p.set_defaults(func=_cmd_add)

    p = sub.add_parser("remove", help="Remove a user-added alias.")
    p.add_argument("alias")
    p.set_defaults(func=_cmd_remove)

    p = sub.add_parser("list", help="List known aliases (built-in + user-added).")
    p.add_argument("--format", choices=["table", "json"], default="table")
    p.set_defaults(func=_cmd_list)

    p = sub.add_parser("search", help="Search the registry by text.")
    p.add_argument("query")
    p.set_defaults(func=_cmd_search)

    p = sub.add_parser("convert", help="[low-level] Generate a CLI from an MCP command.")
    p.add_argument("mcp_command")
    p.add_argument("--out", required=True)
    p.add_argument("--env", action="append", default=[])
    p.add_argument("--emit-skill", action="store_true", help="Also emit SKILL.md / AGENTS.md.")
    p.add_argument("--alias", default=None, help="Alias for the skill (default: derived from --out).")
    p.add_argument("--description", default=None)
    p.add_argument("--skills-dir", default="skills")
    p.add_argument("--agents-file", default="AGENTS.md")
    p.set_defaults(func=_cmd_convert)

    p = sub.add_parser("run", help="[low-level] Call one tool on an MCP command one-shot.")
    p.add_argument("mcp_command")
    p.add_argument("tool")
    p.add_argument("--arguments", default="")
    p.add_argument("--env", action="append", default=[])
    p.set_defaults(func=_cmd_run)

    p = sub.add_parser("mcp-list", help="[low-level] List tools of an MCP command.")
    p.add_argument("mcp_command")
    p.add_argument("--format", choices=["table", "json"], default="table")
    p.add_argument("--env", action="append", default=[])
    p.set_defaults(func=_cmd_mcp_list)

    # batch
    p = sub.add_parser("batch", help="Run multiple tool calls from a JSON spec.")
    p.add_argument("file", nargs="?", default="-", help="JSON spec file (default: stdin)")
    p.add_argument("--continue-on-error", action="store_true")
    p.add_argument("--format", choices=["text", "json"], default="text")
    p.set_defaults(func=lambda a: _cmd_batch(a))

    # openapi
    p = sub.add_parser("openapi", help="Export OpenAPI 3.0 spec per alias.")
    p.add_argument("aliases", nargs="*", help="Alias da esportare (default: tutti)")
    p.add_argument("--out", default=None)
    p.set_defaults(func=lambda a: _cmd_openapi(a))

    # completions
    p = sub.add_parser("completions", help="Emetti script di completamento shell.")
    p.add_argument("--shell", choices=["bash", "zsh"], required=True)
    p.set_defaults(func=lambda a: _cmd_completions(a))

    p = sub.add_parser("daemon", help="Manage background MCP daemons.")
    dsub = p.add_subparsers(dest="daemon_cmd", metavar="DAEMON_CMD")

    dp = dsub.add_parser("start", help="Start a daemon for an alias.")
    dp.add_argument("alias")
    dp.set_defaults(func=lambda a: (_daemon_start(a), 0)[1])

    dp = dsub.add_parser("stop", help="Stop a daemon for an alias.")
    dp.add_argument("alias")
    dp.set_defaults(func=lambda a: (_daemon_stop(a), 0)[1])

    dp = dsub.add_parser("list", help="List running daemons.")
    dp.set_defaults(func=_cmd_daemon_list)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # High-level alias dispatch: if first token is an alias (not a subcommand), route there.
    if argv:
        first = argv[0]
        if first not in META_SUBCOMMANDS and not first.startswith("-"):
            registry = merged_registry(config.load_aliases())
            if first in registry:
                return _dispatch_alias(first, argv[1:])
            # else fall through to subparser — will produce a sensible error.

    parser = argparse.ArgumentParser(
        prog="cli-use",
        description=(
            "Create agent-friendly CLIs and convert MCP servers into CLIs. "
            "Cut AI-agent token costs dramatically."
        ),
        epilog=(
            "High-level: `cli-use add fs /tmp` then `cli-use fs list_directory --path /tmp`. "
            "Run `cli-use list` to see known aliases."
        ),
    )
    _build_subparser(parser)
    args = parser.parse_args(argv)
    if not getattr(args, "_cmd", None):
        parser.print_help(sys.stderr)
        return 2
    return args.func(args)

def _cmd_batch(args: argparse.Namespace) -> int:
    from cli_use import batch
    return batch.run(
        args.file,
        continue_on_error=args.continue_on_error,
        format=args.format,
    )


def _cmd_openapi(args: argparse.Namespace) -> int:
    from cli_use import openapi
    spec = openapi.build_spec(args.aliases or None)
    out = json.dumps(spec, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(out, encoding="utf-8")
        print(f"OpenAPI spec scritto in {args.out}")
    else:
        print(out)
    return 0


def _cmd_completions(args: argparse.Namespace) -> int:
    from cli_use import completions
    if args.shell == "bash":
        print(completions.bash())
    else:
        print("# zsh support coming soon", file=sys.stderr)
    return 0


def _daemon_start(args: argparse.Namespace) -> None:
    daemon.start(args.alias)


def _daemon_stop(args: argparse.Namespace) -> None:
    daemon.stop(args.alias)


def _cmd_daemon_list(args: argparse.Namespace) -> int:
    running = daemon.list_running()
    if not running:
        print("(no daemons running)")
        return 0
    for alias, port in running:
        print(f"  {alias:<15}  127.0.0.1:{port}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
