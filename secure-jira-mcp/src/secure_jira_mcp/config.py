"""Settings, read from environment variables or a .env file in the working folder."""

import urllib.parse
from typing import List, Literal, Optional, Set

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
}
DEFAULT_GROUPS = "search,read,attachments,comments,write,transitions,agile,users,fields,links"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # The Jira Data Center / Server base URL, e.g. https://jira.company.com (or https://company.com/jira).
    jira_url: str = ""
    # Only for the single-user stdio mode. In HTTP mode every request brings its own token.
    jira_personal_token: SecretStr = SecretStr("")
    # Hosts a token may be sent to (comma-separated). Empty means only the host of JIRA_URL.
    jira_allowed_hosts: str = ""
    # Extra fields (display names) that update/create may set, besides the standard safe fields.
    jira_updatable_custom_fields: str = ""

    mcp_tool_groups: str = DEFAULT_GROUPS
    mcp_read_only: bool = False
    # "elicit": the server itself asks the user to approve every change (needs a client that supports
    # MCP elicitation). "client": the client is trusted to have asked the user (for example an app with
    # its own approval step); changes then run when called.
    mcp_confirmation_mode: Literal["elicit", "client"] = "elicit"
    mcp_max_results: int = Field(200, ge=1, le=1000)
    mcp_max_attachment_bytes: int = Field(1_000_000, ge=1_000, le=20_000_000)
    mcp_request_timeout_seconds: float = Field(30.0, gt=0, le=300)
    mcp_rate_limit_per_minute: int = Field(120, ge=1)
    mcp_audit_log: str = "audit.jsonl"

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
        hosts = [h.strip().lower() for h in self.jira_allowed_hosts.split(",") if h.strip()]
        if not hosts and self.jira_url:
            default = urllib.parse.urlsplit(normalize_base_url(self.jira_url, None)).hostname
            hosts = [default] if default else []
        return hosts

    def updatable_custom_fields(self) -> Set[str]:
        return {f.strip().lower() for f in self.jira_updatable_custom_fields.split(",") if f.strip()}


def normalize_base_url(url: str, allowed_hosts: Optional[List[str]]) -> str:
    """https://host[:port][/context-path] without a trailing slash. Raises when the host is not allowed."""
    raw = url.strip()
    if "://" not in raw:
        raw = f"https://{raw}"
    parts = urllib.parse.urlsplit(raw)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host:
        raise ConfigError(f"Jira URL must use https: {url}")
    if allowed_hosts is not None and host not in allowed_hosts:
        raise ConfigError(f"Jira host {host} is not in JIRA_ALLOWED_HOSTS.")
    port = f":{parts.port}" if parts.port else ""
    # Keep a context path such as /jira, but drop page paths such as /browse/KEY-1.
    path = parts.path.rstrip("/")
    for marker in ("/browse/", "/secure/", "/projects/", "/issues/", "/plugins/", "/rest/"):
        if marker in path + "/":
            path = path[: (path + "/").index(marker)]
            break
    return f"https://{host}{port}{path}"
