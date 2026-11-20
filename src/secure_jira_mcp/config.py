"""Settings, read from environment variables or a .env file in the working folder."""

import ipaddress
import urllib.parse
from typing import List, Literal, Optional, Set, Union

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from secure_jira_mcp.errors import ConfigError

# Every tool group the server knows. TOOL_GROUPS selects which of them exist at all.
GROUP_DESCRIPTIONS = {
    "search": "Search work items with JQL (complete results, compact rows).",
    "read": "Work item details, projects, issue types, transitions, components and versions.",
    "attachments": "Read the text of text-like attachments (logs, JSON, XML, CSV).",
    "comments": "List comments; add comments (change).",
    "write": "Create and update work items, assign them (changes).",
    "transitions": "Move a work item to another status (change).",
    "agile": "Boards and sprints; add work items to a sprint (change).",
    "users": "Find users by name or username.",
    "fields": "Field catalog and the fields required to create a work item.",
    "links": "Link types; link two work items (change).",
    "delete": "Delete work items or comments (destructive change). Off unless explicitly enabled.",
    "confluence_read": "Search Confluence pages and list spaces.",
    "confluence_write": "Create pages, update pages, and add comments (changes).",
    "confluence_attachments": "Download and upload Confluence attachments.",
}
DEFAULT_GROUPS = (
    "search,read,attachments,comments,write,transitions,agile,users,fields,links,"
    "confluence_read,confluence_write,confluence_attachments"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Jira deployment. Cloud uses API v3 and email/API-token basic auth; Server/DC uses API v2 and PAT.
    jira_deployment: Literal["data_center", "cloud"] = "data_center"
    # Jira base URL, e.g. https://jira.company.com (or https://company.com/jira).
    jira_url: str = ""
    # Only for the single-user stdio mode. In HTTP mode every request brings its own credentials.
    jira_personal_token: SecretStr = SecretStr("")
    jira_username: str = ""
    jira_api_token: SecretStr = SecretStr("")
    # Scoped Cloud tokens must use Atlassian's api.atlassian.com Jira gateway.
    jira_api_token_scoped: bool = False
    # Hosts a token may be sent to (comma-separated). Empty means only the host of JIRA_URL.
    jira_allowed_hosts: str = ""
    # Private or otherwise non-global destination ranges allowed for direct Jira connections.
    jira_allowed_networks: str = ""
    # HTTPS proxies resolve Jira destinations themselves and therefore become a trusted egress boundary.
    jira_trust_https_proxy: bool = False
    # Extra fields (display names) that update/create may set, besides the standard safe fields.
    jira_updatable_custom_fields: str = ""

    # Confluence is optional and has a separate URL and credential set from Jira.
    confluence_deployment: Literal["data_center", "cloud"] = "data_center"
    confluence_url: str = ""
    confluence_personal_token: SecretStr = SecretStr("")
    confluence_username: str = ""
    confluence_api_token: SecretStr = SecretStr("")
    confluence_api_token_scoped: bool = False
    confluence_allowed_hosts: str = ""
    confluence_allowed_networks: str = ""
    confluence_trust_https_proxy: bool = False

    mcp_tool_groups: str = DEFAULT_GROUPS
    # Changes are opt-in; writable operations still require the configured confirmation mode.
    mcp_read_only: bool = True
    # "elicit": the server itself asks the user to approve every change (needs a client that supports
    # MCP elicitation). "client": the client is trusted to have asked the user (for example an app with
    # its own approval step); changes then run when called.
    mcp_confirmation_mode: Literal["elicit", "client"] = "elicit"
    mcp_max_results: int = Field(200, ge=1, le=1000)
    mcp_max_attachment_bytes: int = Field(1_000_000, ge=1_000, le=20_000_000)
    mcp_request_timeout_seconds: float = Field(30.0, gt=0, le=300)
    mcp_rate_limit_per_minute: int = Field(120, ge=1)
    mcp_audit_log: str = "audit.jsonl"
    mcp_http_allowed_hosts: str = ""
    mcp_http_allowed_origins: str = ""

    use_system_certificates: bool = True
    # A proxy for reaching Jira when Python cannot discover it (for example Zscaler's local proxy).
    https_proxy: str = ""

    def groups(self) -> Set[str]:
        chosen = {g.strip().lower() for g in self.mcp_tool_groups.split(",") if g.strip()}
        unknown = chosen - set(GROUP_DESCRIPTIONS)
        if unknown:
            raise ConfigError(f"Unknown MCP_TOOL_GROUPS entries: {', '.join(sorted(unknown))}.")
        return chosen

    def allowed_hosts(self) -> List[str]:
        hosts = [_canonical_origin(h) for h in self.jira_allowed_hosts.split(",") if h.strip()]
        if not hosts and self.jira_url:
            parsed = urllib.parse.urlsplit(normalize_base_url(self.jira_url, None))
            if parsed.hostname:
                hosts = [_origin_key(parsed.hostname, parsed.port)]
        return hosts

    def allowed_networks(self) -> List[Union[ipaddress.IPv4Network, ipaddress.IPv6Network]]:
        return _parse_networks(self.jira_allowed_networks, "JIRA_ALLOWED_NETWORKS")

    def confluence_allowed_hosts_list(self) -> List[str]:
        hosts = [
            _canonical_origin(h, "CONFLUENCE_ALLOWED_HOSTS")
            for h in self.confluence_allowed_hosts.split(",")
            if h.strip()
        ]
        if not hosts and self.confluence_url:
            parsed = urllib.parse.urlsplit(normalize_base_url(self.confluence_url, None, "Confluence"))
            if parsed.hostname:
                hosts = [_origin_key(parsed.hostname, parsed.port)]
        return hosts

    def confluence_allowed_networks_list(self) -> List[Union[ipaddress.IPv4Network, ipaddress.IPv6Network]]:
        return _parse_networks(self.confluence_allowed_networks, "CONFLUENCE_ALLOWED_NETWORKS")

    def has_confluence_credentials(self) -> bool:
        if not self.confluence_url:
            return False
        if self.confluence_deployment == "cloud":
            return bool(self.confluence_username.strip() and self.confluence_api_token.get_secret_value())
        return bool(self.confluence_personal_token.get_secret_value())

    def has_jira_credentials(self) -> bool:
        if not self.jira_url:
            return False
        if self.jira_deployment == "cloud":
            return bool(self.jira_username.strip() and self.jira_api_token.get_secret_value())
        return bool(self.jira_personal_token.get_secret_value())

    def http_allowed_hosts(self) -> List[str]:
        return [host.strip() for host in self.mcp_http_allowed_hosts.split(",") if host.strip()]

    def http_allowed_origins(self) -> List[str]:
        return [origin.strip() for origin in self.mcp_http_allowed_origins.split(",") if origin.strip()]

    def updatable_custom_fields(self) -> Set[str]:
        return {f.strip().lower() for f in self.jira_updatable_custom_fields.split(",") if f.strip()}


def _parse_networks(
    raw: str,
    setting_name: str,
) -> List[Union[ipaddress.IPv4Network, ipaddress.IPv6Network]]:
    networks = []
    for value in raw.split(","):
        value = value.strip()
        if not value:
            continue
        try:
            networks.append(ipaddress.ip_network(value, strict=False))
        except ValueError as e:
            raise ConfigError(f"Invalid {setting_name} entry: {value}.") from e
    return networks


def _origin_key(host: str, port: Optional[int]) -> str:
    display_host = f"[{host}]" if ":" in host else host
    return f"{display_host}:{port}" if port and port != 443 else display_host


def _canonical_origin(value: str, setting_name: str = "JIRA_ALLOWED_HOSTS") -> str:
    parsed = urllib.parse.urlsplit(f"//{value.strip()}")
    try:
        port = parsed.port
    except ValueError as e:
        raise ConfigError(f"Invalid {setting_name} entry: {value}.") from e
    if (
        not parsed.hostname
        or any(ord(char) <= 32 or ord(char) == 127 for char in value)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or port == 0
    ):
        raise ConfigError(f"Invalid {setting_name} entry: {value}.")
    return _origin_key(parsed.hostname.lower(), port)


def normalize_base_url(url: str, allowed_hosts: Optional[List[str]], service: str = "Jira") -> str:
    """https://host[:port][/context-path] without a trailing slash. Raises when the host is not allowed."""
    raw = url.strip()
    if not raw or len(raw) > 2048 or any(ord(char) < 32 for char in raw):
        raise ConfigError(f"{service} URL is empty, too long, or contains control characters.")
    if "://" not in raw:
        raw = f"https://{raw}"
    parts = urllib.parse.urlsplit(raw)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host or parts.username is not None or parts.password is not None:
        raise ConfigError(f"{service} URL must use https: {url}")
    if parts.query or parts.fragment or "?" in raw or "#" in raw:
        raise ConfigError(f"{service} URL must not include a query or fragment.")
    if "\\" in parts.path or "%" in parts.path or "//" in parts.path:
        raise ConfigError(f"{service} URL contains an invalid context path.")
    if any(segment in (".", "..") for segment in parts.path.split("/")):
        raise ConfigError(f"{service} URL context path must not contain dot segments.")
    try:
        port_number = parts.port
    except ValueError as e:
        raise ConfigError(f"{service} URL contains an invalid port.") from e
    if port_number == 0:
        raise ConfigError(f"{service} URL port must be between 1 and 65535.")
    if allowed_hosts is not None:
        setting_name = "JIRA_ALLOWED_HOSTS" if service == "Jira" else "CONFLUENCE_ALLOWED_HOSTS"
        allowed_origins = {_canonical_origin(entry, setting_name) for entry in allowed_hosts}
        if _origin_key(host, port_number) not in allowed_origins:
            raise ConfigError(f"{service} origin {_origin_key(host, port_number)} is not in {setting_name}.")
    host_display = f"[{host}]" if ":" in host else host
    port = f":{port_number}" if port_number else ""
    # Keep a context path such as /jira, but drop page paths such as /browse/KEY-1.
    path = parts.path.rstrip("/")
    for marker in (
        "/browse/",
        "/secure/",
        "/projects/",
        "/issues/",
        "/plugins/",
        "/spaces/",
        "/display/",
        "/pages/",
        "/rest/",
    ):
        if marker in path + "/":
            path = path[: (path + "/").index(marker)]
            break
    return f"https://{host_display}{port}{path}"
