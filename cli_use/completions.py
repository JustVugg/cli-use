"""Generate static shell completion scripts."""
from __future__ import annotations

from cli_use import config
from cli_use.registry import merged_registry


def bash() -> str:
    reg = merged_registry(config.load_aliases())
    aliases = " ".join(reg.keys())
    lines = [
        "# bash completion for cli-use",
        "_cli_use() {",
        '    local cur="${COMP_WORDS[COMP_CWORD]}"',
        '    local alias="${COMP_WORDS[1]}"',
        '    local tool="${COMP_WORDS[2]}"',
        "",
        '    if [ "$COMP_CWORD" == "1" ]; then',
        f'        COMPREPLY=($(compgen -W "add remove list search convert run mcp-list daemon batch openapi completions {aliases}" -- "$cur"))',
        '        return',
        '    fi',
        "",
        '    case "$alias" in',
    ]
    for alias, entry in reg.items():
        tools = config.read_cached_tools(alias) or []
        tnames = [t["name"] for t in tools if "name" in t]
        lines.append(f'        {alias})')
        lines.append('            if [ "$COMP_CWORD" == "2" ]; then')
        lines.append(f'                COMPREPLY=($(compgen -W "{" ".join(tnames)} --list-tools" -- "$cur"))')
        lines.append('                return')
        lines.append('            fi')
        for t in tools:
            flags = [f"--{p}" for p in (t.get("inputSchema", {}).get("properties") or {}).keys()]
            lines.append(f'            if [ "$tool" == "{t["name"]}" ]; then')
            lines.append(f'                COMPREPLY=($(compgen -W "{" ".join(flags)}" -- "$cur"))')
            lines.append('                return')
            lines.append('            fi')
        lines.append('            ;;')
    lines += ['    esac', '}', '', 'complete -F _cli_use cli-use']
    return "\n".join(lines)