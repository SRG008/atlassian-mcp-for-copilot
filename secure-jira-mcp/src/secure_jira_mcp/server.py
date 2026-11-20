"""The MCP server: a few always-listed tools, everything else loaded on demand, and confirmation for every change."""

import hashlib
import logging
from dataclasses import dataclass
from typing import Annotated, Any, Callable, Dict, List, Mapping, Optional

from mcp.server import MCPServer
from mcp.server.mcpserver import AcceptedElicitation, Context, Elicit, ElicitationResult, Resolve
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from secure_jira_mcp import __version__
from secure_jira_mcp.config import Settings, normalize_base_url
from secure_jira_mcp.errors import AuthError, SecureJiraError, ValidationError
from secure_jira_mcp.jira_client import JiraClient
from secure_jira_mcp.operations import REGISTRY, Args, Env, Operation
from secure_jira_mcp.policy import AuditLog, RateLimiter, check_allowed, describe_groups, describe_operations

logger = logging.getLogger(__name__)

READ_KINDS = ("read",)
CHANGE_KINDS = ("change", "destructive")

INSTRUCTIONS = """Jira Data Center tools with an administrator policy and human approval for every change.
- Lists: search_issues with JQL. It follows every page and returns compact rows; do not page it yourself.
- One work item: get_issue. The default sections include attachments, sprint, epic, story points, links,
  sub-tasks and the latest comments; ask for custom_fields, time or history only when needed.
- Other operations are loaded on demand: call list_operations to see the groups, list_operations(group)
  to get an operation's arguments, then run_read (reads) or run_change (changes).
- Every change is shown to the user with its exact effect and runs only after the user approves it.
  If the result says it was declined, nothing changed; do not retry unless the user asks."""

ClientFactory = Callable[[str, str], JiraClient]


class Approval(BaseModel):
    approve: bool = Field(description="Approve this change")


@dataclass
class Session:
    base_url: str
    token: str
    principal: str


def resolve_session(headers: Optional[Mapping[str, str]], settings: Settings, allowed_hosts: List[str]) -> Session:
    """The Jira site and token for one request. HTTP requests must bring their own token; stdio uses the settings."""
    if headers is not None:
        # The server's own token is never used for an HTTP request.
        scheme, _, token = (headers.get("authorization") or "").partition(" ")
        if scheme.lower() not in ("bearer", "token") or not token.strip():
            raise AuthError("Send your Jira personal access token as 'Authorization: Bearer <token>'.")
        site = headers.get("x-jira-url") or settings.jira_url
    else:
        token = settings.jira_personal_token.get_secret_value()
        site = settings.jira_url
        if not token:
            raise AuthError("Set JIRA_PERSONAL_TOKEN for the stdio mode.")
    if not site:
        raise AuthError("No Jira site: set JIRA_URL or send the X-Jira-Url header.")
    base_url = normalize_base_url(site, allowed_hosts)
    principal = hashlib.sha256(f"{base_url}\n{token.strip()}".encode()).hexdigest()[:16]
    return Session(base_url=base_url, token=token.strip(), principal=principal)


def build_server(settings: Settings, client_factory: Optional[ClientFactory] = None) -> MCPServer:
    audit = AuditLog(settings.mcp_audit_log)
    limiter = RateLimiter(settings.mcp_rate_limit_per_minute)
    allowed_hosts = settings.allowed_hosts()
    settings.groups()  # fail fast on unknown group names

    def factory(base_url: str, token: str) -> JiraClient:
        if client_factory:
            return client_factory(base_url, token)
        return JiraClient(base_url, token, settings.mcp_request_timeout_seconds)

    def session(ctx: Context) -> Session:
        return resolve_session(ctx.headers, settings, allowed_hosts)

    def prepare(ctx: Context, name: str, arguments: Dict[str, Any], kinds: tuple) -> tuple:
        op = REGISTRY.get(name)
        if op is None:
            raise ValidationError(f"Unknown operation '{name}'. Call list_operations to see what is available.")
        if op.kind not in kinds:
            other = "run_change" if op.kind in CHANGE_KINDS else "run_read"
            raise ValidationError(f"'{name}' must be run with {other}.")
        check_allowed(op, settings)
        args = op.parse(arguments)
        current = session(ctx)
        limiter.check(current.principal)
        return op, args, current

    async def execute(op: Operation, args: Args, current: Session) -> Any:
        async with factory(current.base_url, current.token) as client:
            return await op.run(Env(client, settings), args)

    async def read(ctx: Context, name: str, arguments: Dict[str, Any]) -> Any:
        try:
            op, args, current = prepare(ctx, name, arguments, READ_KINDS)
            return await execute(op, args, current)
        except SecureJiraError as e:
            raise ToolError(str(e)) from e

    async def preview(ctx: Context, name: str, arguments: Dict[str, Any]) -> str:
        op, args, current = prepare(ctx, name, arguments, CHANGE_KINDS)
        async with factory(current.base_url, current.token) as client:
            text = await op.preview(Env(client, settings), args) if op.preview else f"Run {op.name} with {arguments}"
        return f"{text}\n\nSite: {current.base_url}"

    async def change(ctx: Context, name: str, arguments: Dict[str, Any], approved: bool) -> Dict[str, Any]:
        try:
            op, args, current = prepare(ctx, name, arguments, CHANGE_KINDS)
        except SecureJiraError as e:
            raise ToolError(str(e)) from e
        target = op.target(args)
        if not approved:
            audit.record(current.principal, current.base_url, op.name, target, "declined", arguments)
            return {"status": "declined", "message": "The user did not approve this change, so nothing was changed."}
        try:
            result = await execute(op, args, current)
        except SecureJiraError as e:
            audit.record(current.principal, current.base_url, op.name, target, "failed", arguments, str(e))
            raise ToolError(str(e)) from e
        audit.record(current.principal, current.base_url, op.name, target, "done", arguments)
        return {"status": "done", "result": result}

    server = MCPServer("secure-jira-mcp", instructions=INSTRUCTIONS, version=__version__)
    read_only = ToolAnnotations(read_only_hint=True, open_world_hint=True)

    @server.tool(annotations=read_only)
    async def search_issues(
        jql: str,
        ctx: Context,
        max_results: Optional[int] = None,
        extra_fields: Optional[List[str]] = None,
        count_only: bool = False,
    ) -> Dict[str, Any]:
        """Search work items with JQL (e.g. project = PROJ AND issuetype = Story ORDER BY updated DESC).
        Returns every matching item up to the server limit as compact rows (key, summary, type, status,
        priority, assignee, updated). extra_fields adds columns by field name, e.g. ["Sprint", "Story Points"].
        count_only returns just the number of matches."""
        arguments: Dict[str, Any] = {"jql": jql, "count_only": count_only, "extra_fields": extra_fields or []}
        if max_results is not None:
            arguments["max_results"] = max_results
        return await read(ctx, "search_issues", arguments)

    @server.tool(annotations=read_only)
    async def get_issue(
        issue_key: str,
        ctx: Context,
        sections: Optional[List[str]] = None,
        comment_limit: int = 10,
    ) -> Dict[str, Any]:
        """One work item. Default sections: details, people, dates, agile (sprint, epic, story points),
        attachments (name, size, type, link), links, subtasks, comments. Optional: custom_fields, time,
        history, or "all". Read a text attachment with run_read("get_attachment_text")."""
        arguments: Dict[str, Any] = {"issue_key": issue_key, "comment_limit": comment_limit}
        if sections:
            arguments["sections"] = sections
        return await read(ctx, "get_issue", arguments)

    @server.tool(annotations=read_only)
    async def list_projects(ctx: Context) -> Dict[str, Any]:
        """Projects you can see (key, name, type)."""
        return await read(ctx, "list_projects", {})

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
    def list_operations(group: Optional[str] = None) -> Dict[str, Any]:
        """Without a group: the operation groups and which are enabled. With a group: its operations
        and their arguments, to run with run_read or run_change."""
        try:
            if group:
                return {"group": group, "operations": describe_operations(group.strip().lower(), settings)}
            return {"groups": describe_groups(settings), "read_only": settings.mcp_read_only}
        except SecureJiraError as e:
            raise ToolError(str(e)) from e

    @server.tool(annotations=read_only)
    async def run_read(operation: str, ctx: Context, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Run a read operation from list_operations, e.g. run_read("list_sprints", {"board_id": 12})."""
        return await read(ctx, operation, arguments or {})

    change_annotations = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)
    change_doc = (
        "Run a change from list_operations, e.g. run_change(\"add_comment\", {\"issue_key\": \"PROJ-1\", \"body\": \"...\"}). "
        "The user sees the exact effect and must approve it before anything changes."
    )

    if settings.mcp_confirmation_mode == "elicit":

        async def ask_user(operation: str, arguments: Dict[str, Any], ctx: Context) -> Elicit[Approval]:
            # Runs before the tool body on every round, so it must only read.
            capabilities = ctx.client_capabilities
            if not (capabilities and capabilities.elicitation):
                raise ToolError(
                    "This client cannot show approval prompts (MCP elicitation), so changes are refused. Use a client "
                    "that supports it, or set MCP_CONFIRMATION_MODE=client if the client has its own approval step."
                )
            try:
                text = await preview(ctx, operation, arguments or {})
            except SecureJiraError as e:
                raise ToolError(str(e)) from e
            return Elicit(f"{text}\n\nApprove this change?", Approval)

        async def run_change(
            operation: str,
            arguments: Dict[str, Any],
            ctx: Context,
            decision: Annotated[ElicitationResult[Approval], Resolve(ask_user)],
        ) -> Dict[str, Any]:
            approved = isinstance(decision, AcceptedElicitation) and bool(decision.data and decision.data.approve)
            return await change(ctx, operation, arguments or {}, approved)

    else:

        async def run_change(operation: str, arguments: Dict[str, Any], ctx: Context) -> Dict[str, Any]:
            # "client" mode: the calling app is trusted to have asked the user already.
            result = await change(ctx, operation, arguments or {}, True)
            return result

    server.tool(name="run_change", description=change_doc, annotations=change_annotations)(run_change)

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
    async def preview_change(operation: str, arguments: Dict[str, Any], ctx: Context) -> Dict[str, Any]:
        """Show what a change would do, without doing it. Useful to discuss a change before running it."""
        try:
            return {"preview": await preview(ctx, operation, arguments or {})}
        except SecureJiraError as e:
            raise ToolError(str(e)) from e

    return server
