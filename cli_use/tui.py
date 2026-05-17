"""Interactive terminal UI for cli-use.

This module intentionally stays dependency-free. It is a line-oriented TUI
instead of a curses app, so it works on Windows and in minimal Python installs.
"""
from __future__ import annotations

import json
import os
import shlex
import sys
from typing import Any, Callable, TextIO

from cli_use import config, daemon, discovery
from cli_use.mcp_client import MCPClient, Tool, extract_text_content
from cli_use.registry import RegistryEntry, merged_registry, parse_source_spec
from cli_use.skill import ToolSpec, emit_skill, update_agents_md

try:
    import readline as _readline
except Exception:  # pragma: no cover - unavailable on some platforms.
    _readline = None


Prompt = Callable[[str], str]
_SKIP = object()
TUI_WIDTH = 96
HELP_COMMANDS = {"h", "help", "?"}


class QuitTUI(Exception):
    """Raised internally to leave the TUI from nested screens."""


def run(
    *,
    start_alias: str | None = None,
    clear_screen: bool = True,
    refresh: bool = False,
    input_func: Prompt = input,
    output: TextIO | None = None,
) -> int:
    """Run the interactive terminal UI."""
    app = TUI(
        input_func=input_func,
        output=output or sys.stdout,
        clear_screen=clear_screen,
        refresh=refresh,
    )
    return app.run(start_alias=start_alias)


def snapshot(alias: str | None = None, *, refresh: bool = False) -> str:
    """Render one TUI screen and return it as text.

    Used by smoke tests and useful in scripts where an interactive prompt is
    undesirable. By default it only uses cached tool schemas for alias screens.
    """
    if alias:
        return render_alias(alias, refresh=refresh, fetch=refresh)
    return render_home()


def _header(title: str, subtitle: str = "") -> list[str]:
    inner = TUI_WIDTH - 2
    lines = [
        "+" + "-" * inner + "+",
        "|" + _fit(title, inner) + "|",
    ]
    if subtitle:
        lines.append("|" + _fit(subtitle, inner) + "|")
    lines.append("+" + "-" * inner + "+")
    return lines


def _section(title: str, rows: list[str]) -> list[str]:
    lines = ["", title, "-" * min(TUI_WIDTH, max(8, len(title)))]
    if not rows:
        return lines + ["  (none)"]
    return lines + [f"  {row}" for row in rows]


def _fit(text: str, width: int) -> str:
    return " " + _clip(text, max(1, width - 2)).ljust(max(1, width - 2)) + " "


def render_home(search: str = "") -> str:
    entries = _filtered_entries(search)
    lines = _header(
        "cli-use TUI",
        "Manage MCP aliases, run tools, start daemons, and discover servers from Glama.",
    )
    lines += _section(
        "Status",
        [
            f"Config: {_clip(str(config.config_dir()), 78)}",
            "Legend: [+] runnable now, [ ] not installed or missing runtime",
        ],
    )

    if not entries:
        alias_rows = ["(no aliases)"]
    else:
        width = max(5, max(len(entry.alias) for entry in entries))
        alias_rows = [f"{'#':>2}  S    {'Alias':<{width}}  Source  Description"]
        for i, entry in enumerate(entries, 1):
            status = "[+]" if _is_installed(entry) else "[ ]"
            source = _source_kind(entry)
            desc = _clip(_first_line(entry.description), 72)
            alias_rows.append(f"{i:>2}. {status}  {entry.alias:<{width}}  {source:<6}  {desc}")

    lines += _section("Aliases", alias_rows)
    if search:
        lines += _section("Filter", [f"Current filter: {search}"])
    lines += _section(
        "Commands",
        [
            "number or alias   open an MCP server",
            "/text             filter aliases, for example /git",
            "g                 search and install MCP servers from Glama",
            "a                 add a custom source manually",
            "d                 manage running daemons",
            "h or ?            open help",
            "q                 quit",
        ],
    )
    return "\n".join(lines)


def render_alias(alias: str, *, refresh: bool = False, fetch: bool = True) -> str:
    entry = _registry().get(alias)
    if entry is None:
        return "\n".join(
            [
                *_header("cli-use TUI", "Alias not found."),
                f"Unknown alias: {alias}",
                "",
                "Commands: b back, h help, q quit",
            ]
        )

    installed = _is_installed(entry)
    running = daemon.is_running(alias)
    title = f"{entry.alias} - {entry.name}"
    lines = _header(title, _first_line(entry.description) or "MCP server alias")
    details = [
        f"Source: {_clip(_source_label(entry), 78)}",
        f"Args: {' '.join(entry.args) if entry.args else '(none)'}",
        f"Installed: {'yes' if installed else 'no'}",
        f"Daemon: {'running' if running else 'stopped'}",
    ]
    if entry.env_required:
        missing = [key for key in entry.env_required if key not in os.environ and key not in entry.env]
        env_status = "missing " + ", ".join(missing) if missing else "set"
        details.append(f"Env: {env_status}")
    lines += _section("Details", details)

    if not installed:
        tool_rows = ["Install this alias to inspect tools."]
    else:
        try:
            tools = _get_tools(entry, refresh=refresh, fetch=fetch)
        except Exception as exc:
            tool_rows = [f"failed to load tools: {exc}"]
            tools = []
        if tools:
            width = max(4, max(len(tool.name) for tool in tools))
            tool_rows = [f"{'#':>2}  {'Tool':<{width}}  Description"]
            for i, tool in enumerate(tools, 1):
                desc = _clip(_first_line(tool.description), 72)
                tool_rows.append(f"{i:>2}. {tool.name:<{width}}  {desc}")
        elif "tool_rows" not in locals():
            tool_rows = ["(no cached tools yet; use u to refresh after install)"]

    lines += _section("Tools", tool_rows)
    lines += _section(
        "Commands",
        [
            "number or tool    run a tool and fill its arguments",
            "i                 install or reinstall this alias",
            "u                 refresh tool schema cache",
            "d                 start or stop daemon for this alias",
            "r                 remove user alias",
            "b                 back to aliases",
            "h or ?            open help",
            "q                 quit",
        ],
    )
    return "\n".join(lines)


def render_help(context: str = "global") -> str:
    lines = _header(
        "cli-use TUI Help",
        "Type a command, then press Enter. Press Tab where supported for completion.",
    )
    lines += _section(
        "Global",
        [
            "h, help, ?        show this help screen",
            "q, quit, exit     quit the TUI",
            "b, back           return to the previous screen",
            "Tab               autocomplete aliases, tools, commands, and cached Glama refs",
        ],
    )
    lines += _section(
        "Home Screen",
        [
            "number            open the alias at that row",
            "alias             open an alias by name, for example fs",
            "/text             filter aliases, for example /database",
            "c                 clear the alias filter",
            "g                 open Glama discovery",
            "a                 add a source manually",
            "d                 manage daemon processes",
        ],
    )
    lines += _section(
        "Alias Screen",
        [
            "number or tool    run a tool",
            "i                 install or reinstall the alias",
            "u                 refresh cached tool schemas",
            "d                 toggle daemon for this alias",
            "r                 remove the alias if it is user-added",
        ],
    )
    lines += _section(
        "Glama Discovery",
        [
            "search text       when prompted, type terms like filesystem or database",
            "number            show details for a discovery result",
            "i 1               install result number 1",
            "i namespace/slug  install by Glama ref",
            "/text             run a new discovery search",
        ],
    )
    lines += _section("Current Context", [context])
    return "\n".join(lines)


class TUI:
    def __init__(
        self,
        *,
        input_func: Prompt,
        output: TextIO,
        clear_screen: bool,
        refresh: bool,
    ) -> None:
        self.input = input_func
        self.output = output
        self.clear_screen = clear_screen
        self.refresh = refresh
        self.search = ""
        self._message = ""

    def run(self, *, start_alias: str | None = None) -> int:
        current = start_alias
        try:
            while True:
                if current:
                    current = self._alias_loop(current)
                else:
                    current = self._home_loop()
        except QuitTUI:
            return 0

    def _home_loop(self) -> str | None:
        while True:
            self._draw(render_home(self.search))
            command = self._prompt("home> ", choices=self._home_choices()).strip()
            if not command:
                continue
            lower = command.lower()
            if lower in {"q", "quit", "exit"}:
                raise QuitTUI()
            if lower in HELP_COMMANDS:
                self._show_help("Home screen")
                continue
            if lower in {"a", "add"}:
                self._add_custom()
                continue
            if lower in {"g", "glama", "discover"}:
                self._discover_loop()
                continue
            if lower in {"d", "daemon", "daemons"}:
                self._daemon_loop()
                continue
            if lower in {"c", "clear"}:
                self.search = ""
                continue
            if command.startswith("/"):
                self.search = command[1:].strip()
                continue
            if lower in {"s", "search"}:
                self.search = self._prompt("Search: ").strip()
                continue

            entry = self._entry_from_command(command, self.search)
            if entry:
                return entry.alias
            self._set_message(f"Unknown command or alias: {command}")

    def _alias_loop(self, alias: str) -> str | None:
        while True:
            self._draw(render_alias(alias, refresh=self.refresh, fetch=True))
            entry = _registry().get(alias)
            command = self._prompt(f"{alias}> ", choices=self._alias_choices(entry)).strip()
            if not command:
                continue
            lower = command.lower()
            if lower in {"q", "quit", "exit"}:
                raise QuitTUI()
            if lower in HELP_COMMANDS:
                self._show_help(f"Alias screen: {alias}")
                continue
            if lower in {"b", "back"}:
                return None
            if entry is None:
                self._set_message(f"Unknown alias: {alias}")
                return None
            if lower in {"i", "install", "add"}:
                self._install_entry(entry)
                continue
            if lower in {"u", "refresh"}:
                self._refresh_tools(entry)
                continue
            if lower in {"d", "daemon"}:
                self._toggle_daemon(entry.alias)
                continue
            if lower in {"r", "remove"}:
                if self._remove_alias(entry.alias):
                    return None
                continue

            tool = self._tool_from_command(entry, command)
            if tool is None:
                self._set_message(f"Unknown tool or command: {command}")
                continue
            self._run_tool(entry, tool)

    def _daemon_loop(self) -> None:
        while True:
            lines = _header("Daemons", "Keep MCP servers warm in the background.")
            running = daemon.list_running()
            if running:
                rows = []
                for alias, port in running:
                    rows.append(f"{alias:<15}  127.0.0.1:{port}")
            else:
                rows = ["(none running)"]
            lines += _section("Running", rows)
            lines += _section(
                "Commands",
                [
                    "alias             start if stopped, stop if running",
                    "start <alias>     start daemon for alias",
                    "stop <alias>      stop daemon for alias",
                    "h or ?            open help",
                    "b                 back",
                    "q                 quit",
                ],
            )
            self._draw("\n".join(lines))
            command = self._prompt("daemons> ", choices=self._daemon_choices()).strip()
            if not command:
                continue
            lower = command.lower()
            if lower in {"q", "quit", "exit"}:
                raise QuitTUI()
            if lower in HELP_COMMANDS:
                self._show_help("Daemon screen")
                continue
            if lower in {"b", "back"}:
                return

            parts = command.split()
            if len(parts) == 2 and parts[0].lower() == "start":
                self._start_daemon(parts[1])
            elif len(parts) == 2 and parts[0].lower() == "stop":
                self._stop_daemon(parts[1])
            else:
                self._toggle_daemon(command)

    def _discover_loop(self) -> None:
        query = self._prompt("Glama search: ", choices=discovery.complete()).strip()
        if not query:
            return

        while True:
            try:
                servers = discovery.GlamaClient().search(query, first=10)
            except Exception as exc:
                self._pause(f"Glama search failed: {exc}")
                return

            while True:
                self._draw(self._render_discovery(query, servers))
                choices = (
                    ["b", "back", "q", "quit", "i", "install", "h", "help", "?"]
                    + [str(i) for i in range(1, len(servers) + 1)]
                    + [server.ref for server in servers]
                )
                command = self._prompt("discover> ", choices=choices).strip()
                if not command:
                    continue
                lower = command.lower()
                if lower in {"q", "quit", "exit"}:
                    raise QuitTUI()
                if lower in HELP_COMMANDS:
                    self._show_help("Glama discovery")
                    continue
                if lower in {"b", "back"}:
                    return
                if command.startswith("/"):
                    query = command[1:].strip()
                    if query:
                        break
                    continue

                install = False
                target = command
                if lower.startswith("i "):
                    install = True
                    target = command[2:].strip()
                elif lower in {"i", "install"}:
                    target = self._prompt("Install ref or number: ", choices=choices).strip()
                    install = True

                server = self._server_from_discovery_command(servers, target)
                if server is None:
                    self._set_message(f"Unknown discovery item: {command}")
                    continue
                if install:
                    self._install_glama_server(server)
                    continue
                self._show_glama_details(server)

    def _render_discovery(self, query: str, servers: list[discovery.GlamaServer]) -> str:
        lines = _header("Glama Discovery", f"Search: {query}")
        if not servers:
            rows = ["(no matches)"]
        else:
            width = max(len(server.ref) for server in servers)
            rows = [f"{'#':>2}  {'Ref':<{width}}  Description"]
            for i, server in enumerate(servers, 1):
                desc = _clip(_first_line(server.description), 72)
                rows.append(f"{i:>2}. {server.ref:<{width}}  {desc}")
        lines += _section("Results", rows)
        lines += _section(
            "Commands",
            [
                "number            show details",
                "i <number/ref>    install a result",
                "/text             search again",
                "h or ?            open help",
                "b                 back",
                "q                 quit",
            ],
        )
        return "\n".join(lines)

    def _server_from_discovery_command(
        self,
        servers: list[discovery.GlamaServer],
        command: str,
    ) -> discovery.GlamaServer | None:
        if command.isdigit():
            idx = int(command)
            if 1 <= idx <= len(servers):
                return servers[idx - 1]
        normalized = discovery.normalize_ref(command)
        return next((server for server in servers if server.ref == normalized or server.slug == normalized), None)

    def _show_glama_details(self, server: discovery.GlamaServer) -> None:
        details = discovery.format_details(server)
        install = self._prompt(f"\n{details}\n\nInstall this server? [y/N] ").strip().lower()
        if install in {"y", "yes"}:
            self._install_glama_server(server)

    def _install_glama_server(self, server: discovery.GlamaServer) -> None:
        alias = self._prompt(f"Alias [{server.slug}]: ").strip() or server.slug
        args_raw = self._prompt("Server args (optional): ").strip()
        try:
            server_args = shlex.split(args_raw) if args_raw else None
        except ValueError as exc:
            self._pause(f"Invalid server args: {exc}")
            return
        source_override = self._prompt(
            "Source override (optional npm:/pip:/local:): ",
            choices=["npm:", "pip:", "local:", "git:"],
        ).strip() or None

        try:
            entry = discovery.entry_from_ref(
                server.ref,
                alias=alias,
                server_args=server_args,
                source_override=source_override,
            )
        except Exception as exc:
            self._pause(f"Could not prepare install: {exc}")
            return
        self._install_entry(entry)

    def _add_custom(self) -> None:
        self._draw(
            "\n".join(
                _header("Add MCP Server", "Register a custom server source.")
                + _section(
                    "Examples",
                    [
                        "npm:@scope/server",
                        "pip:mcp-server",
                        "local:python server.py",
                        "glama:namespace/slug",
                    ],
                )
                + _section("Cancel", ["Leave alias empty to cancel."])
            )
        )
        alias = self._prompt("Alias: ").strip()
        if not alias:
            return
        if any(ch.isspace() for ch in alias):
            self._pause("Alias cannot contain whitespace.")
            return

        source_spec = self._prompt("Source: ", choices=["npm:", "pip:", "local:", "git:", "glama:"]).strip()
        if not source_spec:
            self._pause("Source is required.")
            return
        try:
            source = parse_source_spec(source_spec)
        except Exception as exc:
            self._pause(f"Invalid source: {exc}")
            return

        args_raw = self._prompt("MCP positional args (optional): ").strip()
        try:
            server_args = shlex.split(args_raw) if args_raw else []
        except ValueError as exc:
            self._pause(f"Invalid args: {exc}")
            return
        if source.type == "glama":
            try:
                entry = discovery.entry_from_ref(source.url, alias=alias, server_args=server_args or None)
            except Exception as exc:
                self._pause(f"Glama source resolution failed: {exc}")
                return
            self._install_entry(entry)
            return
        name = self._prompt("Name (optional): ").strip() or alias
        description = self._prompt("Description (optional): ").strip() or f"User-added {alias} MCP server"

        entry = RegistryEntry(
            alias=alias,
            name=name,
            description=description,
            source=source,
            args=server_args,
            env={},
        )
        self._install_entry(entry)

    def _install_entry(self, entry: RegistryEntry) -> None:
        if entry.needs_args and not entry.args:
            raw = self._prompt(f"Args for {entry.alias} ({entry.args_hint or 'required'}): ").strip()
            if not raw:
                self._pause("Install cancelled: required args missing.")
                return
            try:
                entry.args = shlex.split(raw)
            except ValueError as exc:
                self._pause(f"Invalid args: {exc}")
                return

        try:
            if not entry.source.is_installed():
                entry.source.install()
            tools = _fetch_tools(entry)
        except Exception as exc:
            self._pause(f"Install/probe failed: {exc}")
            return

        config.upsert_alias(entry.to_dict())
        _write_cached_tools(entry.alias, tools)
        if tools:
            tool_specs = [ToolSpec(t.name, t.description, t.input_schema) for t in tools]
            skill_dir = emit_skill(
                alias=entry.alias,
                description=entry.description,
                tools=tool_specs,
                skills_root="skills",
                binary=f"cli-use {entry.alias}",
            )
            update_agents_md(
                alias=entry.alias,
                description=entry.description,
                tools=tool_specs,
                binary=f"cli-use {entry.alias}",
                agents_path="AGENTS.md",
            )
            self._pause(f"Installed {entry.alias} with {len(tools)} tools. Skill: {skill_dir}")
        else:
            self._pause(f"Installed {entry.alias}, but no tools were reported.")

    def _refresh_tools(self, entry: RegistryEntry) -> None:
        if not _is_installed(entry):
            self._pause(f"{entry.alias} is not installed.")
            return
        try:
            tools = _get_tools(entry, refresh=True, fetch=True)
        except Exception as exc:
            self._pause(f"Refresh failed: {exc}")
            return
        self._pause(f"Refreshed {len(tools)} tools for {entry.alias}.")

    def _run_tool(self, entry: RegistryEntry, tool: Tool) -> None:
        try:
            arguments = self._prompt_arguments(tool)
        except ValueError as exc:
            self._pause(str(exc))
            return

        try:
            result = _call_tool(entry, tool.name, arguments)
        except Exception as exc:
            self._pause(f"Tool call failed: {exc}")
            return

        if result.get("isError"):
            text = extract_text_content(result) or json.dumps(result, indent=2, ensure_ascii=False)
            self._pause(f"Error:\n{text}")
            return

        text = extract_text_content(result)
        if not text:
            text = json.dumps(result, indent=2, ensure_ascii=False)
        self._pause(f"Output:\n{text}")

    def _prompt_arguments(self, tool: Tool) -> dict[str, Any]:
        schema = tool.input_schema or {}
        props = schema.get("properties", {}) or {}
        required = set(schema.get("required", []) or [])
        if not props:
            return {}

        self._write("")
        self._write(f"Arguments for {tool.name}")
        arguments: dict[str, Any] = {}
        for name, prop in props.items():
            marker = "required" if name in required else "optional"
            ptype = _schema_type(prop)
            choices = prop.get("enum")
            suffix = f"{ptype}, {marker}"
            if choices:
                suffix += ", choices: " + ", ".join(str(item) for item in choices)
            default = prop.get("default", _SKIP)
            if default is not _SKIP:
                suffix += f", default: {default}"
            desc = _first_line(prop.get("description", ""))
            if desc:
                self._write(f"  {name}: {_clip(desc, 96)}")
            raw = self._prompt(f"  {name} ({suffix}): ")
            value = coerce_schema_value(raw, prop, required=name in required)
            if value is not _SKIP:
                arguments[name] = value
        return arguments

    def _entry_from_command(self, command: str, search: str) -> RegistryEntry | None:
        entries = _filtered_entries(search)
        if command.isdigit():
            idx = int(command)
            if 1 <= idx <= len(entries):
                return entries[idx - 1]
        return _registry().get(command)

    def _tool_from_command(self, entry: RegistryEntry, command: str) -> Tool | None:
        try:
            tools = _get_tools(entry, refresh=False, fetch=True)
        except Exception as exc:
            self._set_message(f"Could not load tools: {exc}")
            return None
        if command.isdigit():
            idx = int(command)
            if 1 <= idx <= len(tools):
                return tools[idx - 1]
        return next((tool for tool in tools if tool.name == command), None)

    def _toggle_daemon(self, alias: str) -> None:
        if daemon.is_running(alias):
            self._stop_daemon(alias)
        else:
            self._start_daemon(alias)

    def _start_daemon(self, alias: str) -> None:
        if alias not in _registry():
            self._pause(f"Unknown alias: {alias}")
            return
        try:
            daemon.start(alias)
        except Exception as exc:
            self._pause(f"Daemon start failed: {exc}")
            return
        self._pause(f"Daemon start requested for {alias}.")

    def _stop_daemon(self, alias: str) -> None:
        try:
            daemon.stop(alias)
        except Exception as exc:
            self._pause(f"Daemon stop failed: {exc}")
            return
        self._pause(f"Daemon stop requested for {alias}.")

    def _remove_alias(self, alias: str) -> bool:
        confirm = self._prompt(f"Remove user alias {alias}? [y/N] ").strip().lower()
        if confirm not in {"y", "yes"}:
            return False
        if config.remove_alias(alias):
            self._pause(f"Removed {alias}.")
            return True
        self._pause(f"{alias} is built-in or not present in user aliases.")
        return False

    def _draw(self, body: str) -> None:
        if self.clear_screen and _isatty(self.output):
            self.output.write("\033[2J\033[H")
        self.output.write(body)
        if self._message:
            self.output.write(f"\n\n{self._message}")
            self._message = ""
        self.output.write("\n")
        self.output.flush()

    def _write(self, text: str) -> None:
        self.output.write(text + "\n")
        self.output.flush()

    def _prompt(self, text: str, *, choices: list[str] | None = None) -> str:
        self.output.write(text)
        self.output.flush()
        if _readline is None or not choices:
            return self.input("")
        previous = _readline.get_completer()
        _readline.set_completer(_make_completer(choices))
        _readline.parse_and_bind("tab: complete")
        try:
            return self.input("")
        finally:
            _readline.set_completer(previous)

    def _pause(self, message: str = "") -> None:
        if message:
            self._write("")
            self._write(message)
        self._prompt("Press Enter to continue...")

    def _set_message(self, message: str) -> None:
        self._message = message

    def _show_help(self, context: str) -> None:
        while True:
            self._draw(render_help(context))
            command = self._prompt("help> ", choices=["b", "back", "q", "quit", "exit"]).strip().lower()
            if command in {"q", "quit", "exit"}:
                raise QuitTUI()
            if command in {"", "b", "back"}:
                return

    def _home_choices(self) -> list[str]:
        entries = _filtered_entries(self.search)
        return (
            [
                "q", "quit", "a", "add", "g", "glama", "discover", "d", "daemon",
                "daemons", "s", "search", "c", "clear", "h", "help", "?",
            ]
            + [str(i) for i in range(1, len(entries) + 1)]
            + [entry.alias for entry in entries]
        )

    def _alias_choices(self, entry: RegistryEntry | None) -> list[str]:
        base = [
            "q", "quit", "b", "back", "i", "install", "u", "refresh", "d",
            "daemon", "r", "remove", "h", "help", "?",
        ]
        if entry is None:
            return base
        tools = _read_cached_tools(entry.alias) or []
        return base + [str(i) for i in range(1, len(tools) + 1)] + [tool.name for tool in tools]

    def _daemon_choices(self) -> list[str]:
        aliases = list(_registry().keys())
        return (
            ["q", "quit", "b", "back", "start", "stop", "h", "help", "?"]
            + aliases
            + [f"start {alias}" for alias in aliases]
            + [f"stop {alias}" for alias in aliases]
        )


def coerce_schema_value(raw: str, schema: dict[str, Any], *, required: bool = False) -> Any:
    raw = raw.strip()
    if raw == "":
        if "default" in schema:
            return schema["default"]
        if required:
            raise ValueError("Required argument missing.")
        return _SKIP

    ptype = _schema_type(schema)
    if raw.lower() in {"null", "none"} and not required:
        return None
    if ptype == "boolean":
        lowered = raw.lower()
        if lowered in {"1", "true", "t", "yes", "y", "on"}:
            return True
        if lowered in {"0", "false", "f", "no", "n", "off"}:
            return False
        raise ValueError(f"Expected boolean, got {raw!r}.")
    if ptype == "integer":
        return int(raw)
    if ptype == "number":
        return float(raw)
    if ptype in {"array", "object"}:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Expected JSON for {ptype}: {exc}") from exc
        if ptype == "array" and not isinstance(value, list):
            raise ValueError("Expected a JSON array.")
        if ptype == "object" and not isinstance(value, dict):
            raise ValueError("Expected a JSON object.")
        return value
    if ptype == "null":
        return None
    return raw


def _registry() -> dict[str, RegistryEntry]:
    return merged_registry(config.load_aliases())


def _filtered_entries(search: str = "") -> list[RegistryEntry]:
    q = search.lower().strip()
    entries = list(_registry().values())
    if q:
        entries = [
            entry
            for entry in entries
            if q in entry.alias.lower()
            or q in entry.name.lower()
            or q in entry.description.lower()
        ]
    return sorted(entries, key=lambda entry: entry.alias)


def _get_tools(entry: RegistryEntry, *, refresh: bool = False, fetch: bool = True) -> list[Tool]:
    if not refresh:
        cached = _read_cached_tools(entry.alias)
        if cached is not None:
            return cached
    if not fetch:
        return []
    tools = _fetch_tools(entry)
    _write_cached_tools(entry.alias, tools)
    return tools


def _fetch_tools(entry: RegistryEntry) -> list[Tool]:
    with MCPClient(entry.source.run_argv(entry.args), env=_env_for(entry)) as client:
        return client.list_tools()


def _call_tool(entry: RegistryEntry, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if daemon.is_running(entry.alias):
        return daemon.call_tool(entry.alias, tool_name, arguments)
    with MCPClient(entry.source.run_argv(entry.args), env=_env_for(entry)) as client:
        return client.call_tool(tool_name, arguments)


def _env_for(entry: RegistryEntry) -> dict[str, str]:
    return dict(entry.env)


def _read_cached_tools(alias: str) -> list[Tool] | None:
    raw = config.read_cached_tools(alias)
    if raw is None:
        return None
    tools = []
    for item in raw:
        if isinstance(item, dict) and item.get("name"):
            tools.append(
                Tool(
                    name=str(item["name"]),
                    description=str(item.get("description", "")),
                    input_schema=item.get("inputSchema", {}) or {},
                )
            )
    return tools


def _write_cached_tools(alias: str, tools: list[Tool]) -> None:
    config.write_cached_tools(
        alias,
        [
            {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.input_schema,
            }
            for tool in tools
        ],
    )


def _schema_type(schema: dict[str, Any]) -> str:
    raw = schema.get("type", "string")
    if isinstance(raw, list):
        for item in raw:
            if item != "null":
                return str(item)
        return "null"
    if raw is None:
        return "string"
    return str(raw)


def _source_label(entry: RegistryEntry) -> str:
    source = entry.source
    if source.type in {"npm", "pip"}:
        return f"{source.type}:{source.package}"
    if source.type == "local":
        return f"local:{source.command}"
    if source.type == "git":
        suffix = f"#{source.subdir}" if source.subdir else ""
        return f"git:{source.url}{suffix}"
    return source.type


def _source_kind(entry: RegistryEntry) -> str:
    return entry.source.type or "?"


def _is_installed(entry: RegistryEntry) -> bool:
    try:
        return entry.source.is_installed()
    except Exception:
        return False


def _first_line(text: str | None) -> str:
    return (text or "").strip().split("\n", 1)[0]


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."


def _isatty(output: TextIO) -> bool:
    isatty = getattr(output, "isatty", None)
    return bool(isatty and isatty())


def _make_completer(choices: list[str]):
    def complete(text: str, state: int):
        matches = [choice for choice in choices if choice.startswith(text)]
        if state < len(matches):
            return matches[state]
        return None

    return complete


if __name__ == "__main__":
    raise SystemExit(run())
