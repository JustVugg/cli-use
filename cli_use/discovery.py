"""Discovery providers for MCP servers.

The first provider is Glama's public MCP API. The module uses only stdlib
networking and keeps a tiny local cache for shell/TUI autocomplete.
"""
from __future__ import annotations

import html
import json
import os
import re
import shlex
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from cli_use import config
from cli_use.registry import RegistryEntry, Source, parse_source_spec


DEFAULT_GLAMA_BASE_URL = "https://glama.ai/api/mcp/v1"
DEFAULT_GLAMA_SITE_URL = "https://glama.ai"


@dataclass
class GlamaServer:
    id: str
    namespace: str
    slug: str
    name: str
    description: str = ""
    url: str = ""
    repository_url: str = ""
    attributes: list[str] = field(default_factory=list)
    env_schema: dict[str, Any] = field(default_factory=dict)
    tools: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GlamaServer":
        repository = data.get("repository") or {}
        return cls(
            id=str(data.get("id", "")),
            namespace=str(data.get("namespace", "")),
            slug=str(data.get("slug", "")),
            name=str(data.get("name", "")),
            description=str(data.get("description", "")),
            url=str(data.get("url", "")),
            repository_url=str(repository.get("url", "")),
            attributes=[str(item) for item in data.get("attributes", [])],
            env_schema=data.get("environmentVariablesJsonSchema", {}) or {},
            tools=list(data.get("tools", []) or []),
            raw=data,
        )

    @property
    def ref(self) -> str:
        return f"{self.namespace}/{self.slug}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "namespace": self.namespace,
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "url": self.url,
            "repository": {"url": self.repository_url} if self.repository_url else {},
            "attributes": self.attributes,
            "environmentVariablesJsonSchema": self.env_schema,
            "tools": self.tools,
        }


class DiscoveryError(RuntimeError):
    pass


class GlamaClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        site_url: str | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.base_url = (base_url or os.environ.get("CLI_USE_GLAMA_BASE_URL") or DEFAULT_GLAMA_BASE_URL).rstrip("/")
        self.site_url = (site_url or os.environ.get("CLI_USE_GLAMA_SITE_URL") or DEFAULT_GLAMA_SITE_URL).rstrip("/")
        self.timeout = timeout

    def search(
        self,
        query: str = "",
        *,
        attributes: list[str] | None = None,
        first: int = 20,
    ) -> list[GlamaServer]:
        params: dict[str, str] = {"first": str(first)}
        if query:
            params["query"] = query
        for i, attr in enumerate(attributes or []):
            params[f"attributes[{i}]"] = attr
        raw = self._get_json("/servers", params=params)
        servers = [GlamaServer.from_dict(item) for item in raw.get("servers", [])]
        write_cache(servers)
        return servers

    def get_server(self, ref: str) -> GlamaServer:
        namespace, slug = self.resolve_ref(ref)
        raw = self._get_json(f"/servers/{urllib.parse.quote(namespace)}/{urllib.parse.quote(slug)}")
        server = GlamaServer.from_dict(raw)
        write_cache([server], merge=True)
        return server

    def resolve_ref(self, ref: str) -> tuple[str, str]:
        cleaned = normalize_ref(ref)
        parts = cleaned.split("/")
        if len(parts) == 2 and all(parts):
            return parts[0], parts[1]

        cached = find_cached_server(cleaned)
        if cached is not None:
            return cached.namespace, cached.slug

        hits = self.search(cleaned, first=1)
        if not hits:
            raise DiscoveryError(f"no Glama server found for {ref!r}")
        return hits[0].namespace, hits[0].slug

    def fetch_page(self, server: GlamaServer) -> str:
        url = server.url or f"{self.site_url}/mcp/servers/{server.namespace}/{server.slug}"
        return self._get_text(url)

    def attributes(self) -> list[dict[str, Any]]:
        raw = self._get_json("/attributes")
        return list(raw.get("attributes", []) or [])

    def _get_json(self, path: str, *, params: dict[str, str] | None = None) -> dict[str, Any]:
        text = self._get_text(self._url(path, params=params))
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise DiscoveryError(f"Glama returned invalid JSON: {exc}") from exc

    def _get_text(self, url: str) -> str:
        req = urllib.request.Request(url, headers={"User-Agent": "cli-use/0.3 discovery"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.URLError as exc:
            raise DiscoveryError(f"Glama request failed: {exc}") from exc

    def _url(self, path: str, *, params: dict[str, str] | None = None) -> str:
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return url


def normalize_ref(ref: str) -> str:
    ref = ref.strip()
    if ref.startswith("glama:"):
        ref = ref.split(":", 1)[1]
    if ref.startswith("glama/"):
        ref = ref.split("/", 1)[1]
    if ref.startswith("https://glama.ai/mcp/servers/"):
        ref = ref.removeprefix("https://glama.ai/mcp/servers/")
    return ref.strip("/")


def entry_from_ref(
    ref: str,
    *,
    alias: str | None = None,
    server_args: list[str] | None = None,
    source_override: str | None = None,
    client: GlamaClient | None = None,
) -> RegistryEntry:
    client = client or GlamaClient()
    server = client.get_server(ref)
    source, default_args, args_hint = resolve_source(
        server,
        client=client,
        source_override=source_override,
    )
    chosen_args = list(server_args) if server_args is not None else default_args
    return RegistryEntry(
        alias=alias or _safe_alias(server.slug or server.name or server.id),
        name=server.name or server.slug,
        description=server.description or f"Glama MCP server {server.ref}",
        source=source,
        args=chosen_args,
        args_hint=args_hint,
        needs_args=bool(args_hint and not chosen_args),
        env_required=_required_env(server),
    )


def resolve_source(
    server: GlamaServer,
    *,
    client: GlamaClient | None = None,
    source_override: str | None = None,
) -> tuple[Source, list[str], str]:
    if source_override:
        return parse_source_spec(source_override), [], ""

    client = client or GlamaClient()
    page = client.fetch_page(server)
    configs = extract_server_configs(page)
    if not configs:
        raise DiscoveryError(
            f"Glama has no installable config block for {server.ref}; pass --from <source>."
        )

    config_data = choose_config(configs)
    if config_data is None:
        raise DiscoveryError(
            f"Glama config for {server.ref} is not usable by cli-use; pass --from <source>."
        )
    return source_from_config(config_data)


def extract_server_configs(page_html: str) -> list[dict[str, Any]]:
    configs: list[dict[str, Any]] = []
    for block in re.findall(r'<code class="raw-code">(.*?)</code>', page_html, flags=re.DOTALL):
        raw = html.unescape(block).strip()
        if not raw.startswith("{"):
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for root_key in ("mcpServers", "servers"):
            servers = data.get(root_key)
            if not isinstance(servers, dict):
                continue
            for name, server_config in servers.items():
                if isinstance(server_config, dict) and server_config.get("command"):
                    item = dict(server_config)
                    item.setdefault("name", name)
                    configs.append(item)
    return configs


def choose_config(configs: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not configs:
        return None
    priority = {"npx": 0, "uvx": 1, "python": 2, "node": 3, "docker": 4}

    def key(item: dict[str, Any]) -> tuple[int, str]:
        command, _args = normalize_command(item)
        return (priority.get(command, 10), command)

    return sorted(configs, key=key)[0]


def normalize_command(config_data: dict[str, Any]) -> tuple[str, list[str]]:
    command = str(config_data.get("command", "")).strip()
    args = [str(item) for item in config_data.get("args", []) or []]
    if command.lower() == "cmd" and len(args) >= 2 and args[0].lower() in {"/c", "-c"}:
        return args[1], args[2:]
    return command, args


def source_from_config(config_data: dict[str, Any]) -> tuple[Source, list[str], str]:
    command, args = normalize_command(config_data)
    if not command:
        raise DiscoveryError("install config has no command")

    if command == "npx":
        pkg_index = _find_npx_package_index(args)
        if pkg_index is not None:
            package = args[pkg_index]
            rest = args[pkg_index + 1 :]
            default_args, args_hint = _split_default_args(rest)
            return (
                Source(type="npm", package=package, binary=package.split("/")[-1]),
                default_args,
                args_hint,
            )

    full = [command] + args
    return Source(type="local", command=shlex.join(full)), [], ""


def _find_npx_package_index(args: list[str]) -> int | None:
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in {"-y", "--yes"}:
            i += 1
            continue
        if arg.startswith("-"):
            # Most npx flags with a value consume the following token. If the
            # next token also looks like an option, leave it for the next pass.
            if i + 1 < len(args) and not args[i + 1].startswith("-"):
                i += 2
            else:
                i += 1
            continue
        return i
    return None


def _split_default_args(args: list[str]) -> tuple[list[str], str]:
    if not args:
        return [], ""
    if all(_looks_like_placeholder(item) for item in args):
        return [], " ".join(args)
    return args, ""


def _looks_like_placeholder(value: str) -> bool:
    lowered = value.lower()
    return (
        lowered.startswith("/users/")
        or lowered.startswith("/path")
        or lowered.startswith("path/")
        or "<" in lowered
        or "your-" in lowered
        or "example" in lowered
        or "username" in lowered
    )


def _required_env(server: GlamaServer) -> list[str]:
    required = server.env_schema.get("required", []) if isinstance(server.env_schema, dict) else []
    return [str(item) for item in required]


def format_search_results(servers: list[GlamaServer], *, format: str = "table") -> str:
    if format == "json":
        return json.dumps([server.to_dict() for server in servers], indent=2, ensure_ascii=False)
    if not servers:
        return "(no matches)"
    width = max(len(server.ref) for server in servers)
    lines = [f"  {'Ref':<{width}}  Description"]
    for server in servers:
        desc = _clip(_first_line(server.description), 82)
        attrs = f" [{', '.join(server.attributes)}]" if server.attributes else ""
        lines.append(f"  {server.ref:<{width}}  {desc}{attrs}")
    return "\n".join(lines)


def format_details(server: GlamaServer, *, format: str = "table") -> str:
    if format == "json":
        return json.dumps(server.to_dict(), indent=2, ensure_ascii=False)
    lines = [
        f"{server.ref}",
        server.name,
        "",
        server.description or "(no description)",
    ]
    if server.repository_url:
        lines.extend(["", f"Repository: {server.repository_url}"])
    if server.url:
        lines.append(f"Glama: {server.url}")
    if server.attributes:
        lines.append(f"Attributes: {', '.join(server.attributes)}")
    env_required = _required_env(server)
    if env_required:
        lines.append(f"Required env: {', '.join(env_required)}")
    return "\n".join(lines)


def cache_path() -> str:
    return str(config.ensure_dir() / "glama_cache.json")


def read_cache() -> list[GlamaServer]:
    path = config.config_dir() / "glama_cache.json"
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [GlamaServer.from_dict(item) for item in raw.get("servers", []) if isinstance(item, dict)]


def write_cache(servers: list[GlamaServer], *, merge: bool = False) -> None:
    current = {server.ref: server for server in read_cache()} if merge else {}
    for server in servers:
        current[server.ref] = server
    path = config.ensure_dir() / "glama_cache.json"
    path.write_text(
        json.dumps({"servers": [server.to_dict() for server in current.values()]}, indent=2, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )


def find_cached_server(value: str) -> GlamaServer | None:
    value = normalize_ref(value).lower()
    for server in read_cache():
        keys = {
            server.ref.lower(),
            server.slug.lower(),
            server.name.lower(),
            server.id.lower(),
        }
        if value in keys:
            return server
    matches = [server for server in read_cache() if value and value in server.ref.lower()]
    if len(matches) == 1:
        return matches[0]
    return None


def complete(prefix: str = "") -> list[str]:
    prefix = normalize_ref(prefix).lower()
    values: list[str] = []
    for server in read_cache():
        refs = [server.ref, f"glama:{server.ref}", server.slug]
        for ref in refs:
            if ref.lower().startswith(prefix) and ref not in values:
                values.append(ref)
    return sorted(values)


def _safe_alias(value: str) -> str:
    alias = re.sub(r"[^A-Za-z0-9_-]+", "-", value.strip().lower()).strip("-")
    return alias or "glama-server"


def _first_line(text: str) -> str:
    return (text or "").strip().split("\n", 1)[0]


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."
