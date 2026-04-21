"""Framework to write agent-friendly CLIs with a decorator.

    from cli_use import agent_tool, run_cli

    @agent_tool
    def greet(name: str, shout: bool = False) -> str:
        "Greet someone."
        msg = f"hello {name}"
        return msg.upper() if shout else msg

    if __name__ == "__main__":
        run_cli()

Design goals:
- `--help` is terse (~1 line per tool) so an agent spends <50 tokens learning it.
- Arguments are inferred from type hints; no boilerplate.
- Output goes to stdout, logs to stderr, exit codes are meaningful.
"""
from __future__ import annotations

import argparse
import inspect
import json
import sys
from dataclasses import dataclass
from typing import Any, Callable, get_args, get_origin


@dataclass
class _ToolSpec:
    name: str
    func: Callable[..., Any]
    description: str
    signature: inspect.Signature


_REGISTRY: dict[str, _ToolSpec] = {}


def agent_tool(func: Callable[..., Any] | None = None, *, name: str | None = None):
    """Register a function as a CLI subcommand.

    Args:
        name: override the subcommand name (default: function name with `_`→`-`).
    """
    def decorate(f: Callable[..., Any]) -> Callable[..., Any]:
        tool_name = (name or f.__name__).replace("_", "-")
        doc = inspect.getdoc(f) or ""
        # collapse to first line for terse --help
        first_line = doc.strip().split("\n", 1)[0] if doc else ""
        sig = inspect.signature(f)
        _REGISTRY[tool_name] = _ToolSpec(
            name=tool_name,
            func=f,
            description=first_line,
            signature=sig,
        )
        return f

    if func is not None:
        return decorate(func)
    return decorate


def _py_type_to_argparse(annotation: Any) -> dict[str, Any]:
    """Map a Python annotation to argparse kwargs.

    Supports: str, int, float, bool (as --flag), list[str]/list[int] (nargs="*").
    Unknown types fall through as str with a JSON-decode hint.
    """
    if annotation is inspect.Parameter.empty or annotation is str:
        return {"type": str}
    if annotation is int:
        return {"type": int}
    if annotation is float:
        return {"type": float}
    if annotation is bool:
        # handled specially at parser build time (store_true)
        return {"_bool": True}
    origin = get_origin(annotation)
    if origin is list:
        inner = get_args(annotation)
        item_type = inner[0] if inner else str
        if item_type in (str, int, float):
            return {"type": item_type, "nargs": "*", "default": []}
    # fallback: accept a JSON string
    return {"type": _json_value, "metavar": "JSON"}


def _json_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _format_output(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    # structured → compact JSON (token-efficient, still pipeable to jq)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _build_parser(prog: str | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Agent-friendly CLI (cli-use).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subs = parser.add_subparsers(dest="_tool", metavar="TOOL")
    for spec in _REGISTRY.values():
        sp = subs.add_parser(spec.name, help=spec.description, description=spec.description)
        for pname, param in spec.signature.parameters.items():
            flag = "--" + pname.replace("_", "-")
            kwargs = _py_type_to_argparse(param.annotation)
            required = param.default is inspect.Parameter.empty
            if kwargs.pop("_bool", False):
                sp.add_argument(flag, action="store_true", default=bool(param.default) if not required else False)
            else:
                sp.add_argument(
                    flag,
                    required=required,
                    default=None if required else param.default,
                    **kwargs,
                )
    return parser


def _registry_to_toolspecs():
    """Render the registered @agent_tool functions as ToolSpec for skill.py."""
    from cli_use.skill import ToolSpec

    specs: list[ToolSpec] = []
    for name, spec in _REGISTRY.items():
        props: dict[str, dict] = {}
        required: list[str] = []
        for pname, param in spec.signature.parameters.items():
            ann = param.annotation
            if ann in (str, inspect.Parameter.empty):
                ptype = "string"
            elif ann is int:
                ptype = "integer"
            elif ann is float:
                ptype = "number"
            elif ann is bool:
                ptype = "boolean"
            elif get_origin(ann) is list:
                ptype = "array"
            else:
                ptype = "object"
            props[pname] = {"type": ptype}
            if param.default is inspect.Parameter.empty:
                required.append(pname)
        specs.append(ToolSpec(
            name=name,
            description=spec.description,
            input_schema={"type": "object", "properties": props, "required": required},
        ))
    return specs


def emit_skill_from_registry(
    alias: str,
    description: str = "",
    binary: str | None = None,
    skills_root: str = "skills",
    agents_file: str = "AGENTS.md",
) -> str:
    """Emit SKILL.md + AGENTS.md from the current @agent_tool registry."""
    from cli_use.skill import emit_skill, update_agents_md

    specs = _registry_to_toolspecs()
    desc = description or f"Agent CLI '{alias}' generated by cli-use."
    skill_dir = emit_skill(alias=alias, description=desc, tools=specs, skills_root=skills_root, binary=binary)
    update_agents_md(alias=alias, description=desc, tools=specs, binary=binary, agents_path=agents_file)
    return str(skill_dir)


def run_cli(
    argv: list[str] | None = None,
    prog: str | None = None,
    *,
    emit_skill: bool = False,
    alias: str | None = None,
    skill_description: str = "",
    skills_root: str = "skills",
    agents_file: str = "AGENTS.md",
) -> int:
    """Parse argv and dispatch to the registered tool. Returns exit code.

    If `emit_skill=True`, also writes SKILL.md + AGENTS.md on first run so
    agents immediately know how to invoke the CLI.
    """
    parser = _build_parser(prog=prog)
    args = parser.parse_args(argv)
    if emit_skill:
        import os as _os, sys as _sys
        _alias = alias or (prog or _os.path.basename(_sys.argv[0])).replace(".py", "")
        try:
            skill_dir = emit_skill_from_registry(
                alias=_alias,
                description=skill_description,
                binary=prog or _sys.argv[0],
                skills_root=skills_root,
                agents_file=agents_file,
            )
            print(f"cli-use: emitted skill → {skill_dir}", file=_sys.stderr)
        except Exception as e:
            print(f"cli-use: skill emit failed: {e}", file=_sys.stderr)
    if not args._tool:
        parser.print_help(sys.stderr)
        return 2
    spec = _REGISTRY[args._tool]
    kwargs = {
        p: getattr(args, p.replace("-", "_"), None)
        for p in spec.signature.parameters
    }
    try:
        result = spec.func(**kwargs)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    rendered = _format_output(result)
    if rendered:
        print(rendered)
    return 0


def _clear_registry_for_tests() -> None:
    _REGISTRY.clear()
