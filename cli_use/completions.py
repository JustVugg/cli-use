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
        '    local prev="${COMP_WORDS[COMP_CWORD-1]}"',
        '    local alias="${COMP_WORDS[1]}"',
        '    local tool="${COMP_WORDS[2]}"',
        "",
        '    if [ "$COMP_CWORD" == "1" ]; then',
        f'        COMPREPLY=($(compgen -W "add remove list search discover convert run mcp-list daemon batch openapi completions tui {aliases}" -- "$cur"))',
        '        return',
        '    fi',
        "",
        '    if [ "$alias" == "discover" ]; then',
        '        case "$prev" in',
        '            --provider)',
        '                COMPREPLY=($(compgen -W "glama" -- "$cur"))',
        '                return',
        '                ;;',
        '            --format)',
        '                COMPREPLY=($(compgen -W "table json" -- "$cur"))',
        '                return',
        '                ;;',
        '            --from)',
        '                COMPREPLY=($(compgen -W "npm: pip: local: git: glama:" -- "$cur"))',
        '                return',
        '                ;;',
        '            --install|--details)',
        '                COMPREPLY=($(compgen -W "$(cli-use discover --complete "$cur" 2>/dev/null)" -- "$cur"))',
        '                return',
        '                ;;',
        '        esac',
        '        COMPREPLY=($(compgen -W "--query --provider --attribute --category --limit --format --details --install --alias --from --server-args --no-skill --skills-dir --agents-file" -- "$cur"))',
        '        return',
        '    fi',
        "",
        '    if [ "$alias" == "tui" ]; then',
        f'        COMPREPLY=($(compgen -W "--snapshot --no-clear --refresh {aliases}" -- "$cur"))',
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
