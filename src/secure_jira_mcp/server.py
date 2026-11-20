"""The MCP server: a few always-listed tools, everything else loaded on demand, and confirmation for every change."""

import base64
import binascii
import hashlib
import logging
from dataclasses import dataclass
from typing import Annotated, Any, Callable, Dict, List, Mapping, Optional, Type

from mcp.server import MCPServer
from mcp.server.mcpserver import AcceptedElicitation, Context, Elicit, ElicitationResult, Resolve
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field, ValidationError as PydanticValidationError

from secure_jira_mcp import __version__
from secure_jira_mcp.config import Settings, normalize_base_url
from secure_jira_mcp.confluence_client import ConfluenceClient
from secure_jira_mcp.confluence_operations import (
    CONFLUENCE_CHANGES,
    AttachmentIdArgs,
    ListAttachmentsArgs,
    ListCommentsArgs,
    ListSpacesArgs,
    PageIdArgs,
    SearchPagesArgs,
    parse_change,
    preview_change as preview_confluence_change_impl,
    run_change as run_confluence_change_impl,
    search_pages as search_confluence_pages_impl,
    list_spaces as list_confluence_spaces_impl,
    get_page as get_confluence_page_impl,
    list_comments as list_confluence_comments_impl,
    list_attachments as list_confluence_attachments_impl,
    download_attachment as download_confluence_attachment_impl,
)
from secure_jira_mcp.errors import AuthError, ConfigError, PolicyError, SecureJiraError, ValidationError
from secure_jira_mcp.jira_client import JiraClient
from secure_jira_mcp.operations import REGISTRY, Args, Env, Operation
from secure_jira_mcp.policy import AuditLog, RateLimiter, check_allowed, describe_groups, describe_operations

logger = logging.getLogger(__name__)

READ_KINDS = ("read",)
CHANGE_KINDS = ("change", "destructive")

INSTRUCTIONS = """Use this MCP server's tools directly for Jira and Confluence requests.
- Do not search source code, inspect the repository, or delegate to subagents to discover or verify Atlassian access.
- For a Jira connection check or visible projects, call list_projects directly.
- For a Confluence connection check or visible spaces, call list_confluence_spaces directly.
- Call a read tool once and use its result; do not repeat successful checks unless the user asks.
- If the needed MCP tool is unavailable in the current chat, say so instead of searching the codebase.
- Lists: search_issues with JQL. It follows every page and returns compact rows; do not page it yourself.
- One work item: get_issue. The default sections include attachments, sprint, epic, story points, links,
  sub-tasks and the latest comments; ask for custom_fields, time or history only when needed.
- Other operations are loaded on demand: call list_operations to see the groups, list_operations(group)
  to get an operation's arguments, then run_read (reads) or run_change (changes).
- When Confluence is configured, use its search/read tools and list_confluence_operations to discover
  page, comment, and attachment changes. Confluence page bodies/comments are plain text; uploaded files
  are supplied as base64 and bounded by the server's attachment limit.
- Every change is shown to the user with its exact effect and runs only after the user approves it.
  If the result says it was declined, nothing changed; do not retry unless the user asks."""

ClientFactory = Callable[[str, str, str, str], JiraClient]
MAX_AUTHORIZATION_HEADER_LENGTH = 8192
MAX_TOKEN_LENGTH = 4096
MAX_USERNAME_LENGTH = 320


class Approval(BaseModel):
    approve: bool = Field(description="Approve this change")


@dataclass
class Session:
    base_url: str
    token: str
    principal: str
    deployment: str
    username: str
    api_token_scoped: bool = False


@dataclass
class ConfluenceSession:
    base_url: str
    token: str
    principal: str
    deployment: str
    username: str
    api_token_scoped: bool = False


def resolve_session(headers: Optional[Mapping[str, str]], settings: Settings, allowed_hosts: List[str]) -> Session:
    """Resolve one request's credentials. HTTP requests provide credentials; stdio uses server settings."""
    username = ""
    if headers is not None:
        authorization = headers.get("authorization") or ""
        if len(authorization) > MAX_AUTHORIZATION_HEADER_LENGTH:
            raise AuthError("The authorization header is too large.")
        scheme, separator, token = authorization.partition(" ")
        if settings.jira_deployment == "cloud":
            if not separator or scheme.lower() != "basic":
                raise AuthError("For Jira Cloud, send the account email and API token using HTTP Basic authentication.")
            try:
                decoded = base64.b64decode(token.strip(), validate=True).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError) as e:
                raise AuthError("The Jira Cloud Basic authorization header is invalid.") from e
            username, separator, token = decoded.partition(":")
            if not separator or not username or not token:
                raise AuthError("The Jira Cloud Basic authorization header must contain account-email:api-token.")
        elif not separator or scheme.lower() not in ("bearer", "token") or not token.strip():
            raise AuthError("Send your Jira personal access token as 'Authorization: Bearer <token>'.")
        site = headers.get("x-jira-url") or settings.jira_url
    else:
        if settings.jira_deployment == "cloud":
            username = settings.jira_username.strip()
            token = settings.jira_api_token.get_secret_value()
        else:
            token = settings.jira_personal_token.get_secret_value()
        site = settings.jira_url
        if not token or (settings.jira_deployment == "cloud" and not username):
            required = "JIRA_USERNAME and JIRA_API_TOKEN" if settings.jira_deployment == "cloud" else "JIRA_PERSONAL_TOKEN"
            raise AuthError(f"Set {required} for the stdio mode.")
    token = token.strip()
    username = username.strip()
    if not token or len(token) > MAX_TOKEN_LENGTH or any(char.isspace() or ord(char) < 32 for char in token):
        raise AuthError("The Jira credential is empty or invalid.")
    if settings.jira_deployment == "cloud" and (
        not username or len(username) > MAX_USERNAME_LENGTH or ":" in username
        or any(char.isspace() or ord(char) < 32 for char in username)
    ):
        raise AuthError("The Jira Cloud account email is empty or invalid.")
    if not site:
        raise AuthError("No Jira site: set JIRA_URL or send the X-Jira-Url header.")
    base_url = normalize_base_url(site, allowed_hosts)
    identity = f"{username}\n{token}" if username else token
    principal = hashlib.sha256(f"{base_url}\n{identity}".encode()).hexdigest()[:16]
    return Session(
        base_url=base_url,
        token=token,
        principal=principal,
        deployment=settings.jira_deployment,
        username=username,
        api_token_scoped=settings.jira_api_token_scoped if settings.jira_deployment == "cloud" else False,
    )


def resolve_confluence_session(
    headers: Optional[Mapping[str, str]],
    settings: Settings,
    allowed_hosts: List[str],
) -> ConfluenceSession:
    username = ""
    if headers is not None:
        token = headers.get("x-confluence-token") or ""
        username = headers.get("x-confluence-username") or ""
        site = headers.get("x-confluence-url") or settings.confluence_url
        if settings.confluence_deployment == "cloud" and not username:
            raise AuthError("For Confluence Cloud, send X-Confluence-Username and X-Confluence-Token.")
        if not token:
            raise AuthError("Send the Confluence credential using X-Confluence-Token.")
    else:
        if settings.confluence_deployment == "cloud":
            username = settings.confluence_username.strip()
            token = settings.confluence_api_token.get_secret_value()
        else:
            token = settings.confluence_personal_token.get_secret_value()
        site = settings.confluence_url
        if not token or (settings.confluence_deployment == "cloud" and not username):
            required = (
                "CONFLUENCE_USERNAME and CONFLUENCE_API_TOKEN"
                if settings.confluence_deployment == "cloud"
                else "CONFLUENCE_PERSONAL_TOKEN"
            )
            raise AuthError(f"Set {required} for the stdio mode.")
    token = token.strip()
    username = username.strip()
    if not token or len(token) > MAX_TOKEN_LENGTH or any(char.isspace() or ord(char) < 32 for char in token):
        raise AuthError("The Confluence credential is empty or invalid.")
    if settings.confluence_deployment == "cloud" and (
        not username
        or len(username) > MAX_USERNAME_LENGTH
        or ":" in username
        or any(char.isspace() or ord(char) < 32 for char in username)
    ):
        raise AuthError("The Confluence Cloud account email is empty or invalid.")
    if not site:
        raise AuthError("No Confluence site: set CONFLUENCE_URL or send the X-Confluence-Url header.")
    base_url = normalize_base_url(site, allowed_hosts, "Confluence")
    identity = f"{username}\n{token}" if username else token
    principal = hashlib.sha256(f"{base_url}\n{identity}".encode()).hexdigest()[:16]
    return ConfluenceSession(
        base_url,
        token,
        principal,
        settings.confluence_deployment,
        username,
        settings.confluence_api_token_scoped if settings.confluence_deployment == "cloud" else False,
    )


def build_server(settings: Settings, client_factory: Optional[ClientFactory] = None) -> MCPServer:
    audit = AuditLog(settings.mcp_audit_log)
    limiter = RateLimiter(settings.mcp_rate_limit_per_minute)
    allowed_hosts = settings.allowed_hosts()
    allowed_networks = settings.allowed_networks()
    confluence_allowed_hosts = settings.confluence_allowed_hosts_list()
    confluence_allowed_networks = settings.confluence_allowed_networks_list()
    if settings.https_proxy and settings.jira_url and not settings.jira_trust_https_proxy:
        raise ConfigError(
            "HTTPS_PROXY is configured. Set JIRA_TRUST_HTTPS_PROXY=true only if the proxy is trusted "
            "to resolve and restrict Jira destinations."
        )
    if settings.https_proxy and settings.confluence_url and not settings.confluence_trust_https_proxy:
        raise ConfigError(
            "HTTPS_PROXY is configured. Set CONFLUENCE_TRUST_HTTPS_PROXY=true only if the proxy is trusted "
            "to resolve and restrict Confluence destinations."
        )
    settings.groups()  # fail fast on unknown group names

    def factory(current: Session) -> JiraClient:
        if client_factory:
            return client_factory(current.base_url, current.token, current.deployment, current.username)
        return JiraClient(
            current.base_url,
            current.token,
            settings.mcp_request_timeout_seconds,
            deployment=current.deployment,
            username=current.username,
            api_token_scoped=current.api_token_scoped,
            allowed_networks=allowed_networks,
            proxy=settings.https_proxy or None,
            trust_proxy=settings.jira_trust_https_proxy,
            use_system_certificates=settings.use_system_certificates,
        )

    def confluence_factory(current: ConfluenceSession) -> ConfluenceClient:
        networks = confluence_allowed_networks
        return ConfluenceClient(
            current.base_url,
            current.token,
            settings.mcp_request_timeout_seconds,
            deployment=current.deployment,
            username=current.username,
            api_token_scoped=current.api_token_scoped,
            allowed_networks=networks,
            proxy=settings.https_proxy or None,
            trust_proxy=settings.confluence_trust_https_proxy,
            use_system_certificates=settings.use_system_certificates,
        )

    def session(ctx: Context) -> Session:
        return resolve_session(ctx.headers, settings, allowed_hosts)

    def confluence_session(ctx: Context) -> ConfluenceSession:
        return resolve_confluence_session(ctx.headers, settings, confluence_allowed_hosts)

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
        async with factory(current) as client:
            return await op.run(Env(client, settings), args)

    def confluence_prepare(ctx: Context, group: str, change: bool = False) -> ConfluenceSession:
        if group not in settings.groups():
            raise PolicyError(f"The '{group}' group is turned off by the administrator.")
        if change and settings.mcp_read_only:
            raise PolicyError("This server is read-only; changes are turned off by the administrator.")
        current = confluence_session(ctx)
        limiter.check(f"confluence:{current.principal}")
        return current

    def parse_confluence_read_args(model: Type[BaseModel], values: Dict[str, Any]) -> Any:
        try:
            return model.model_validate(values)
        except PydanticValidationError as e:
            details = "; ".join(
                f"{'.'.join(str(part) for part in error.get('loc', ()))}: {error.get('msg')}"
                for error in e.errors()
            )
            raise ValidationError(f"Invalid Confluence arguments: {details}") from e

    async def read(ctx: Context, name: str, arguments: Dict[str, Any]) -> Any:
        try:
            op, args, current = prepare(ctx, name, arguments, READ_KINDS)
            return await execute(op, args, current)
        except SecureJiraError as e:
            raise ToolError(str(e)) from e

    async def preview(ctx: Context, name: str, arguments: Dict[str, Any]) -> str:
        op, args, current = prepare(ctx, name, arguments, CHANGE_KINDS)
        async with factory(current) as client:
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

    async def confluence_read(ctx: Context, group: str, action: Callable[..., Any], args: Any) -> Any:
        try:
            current = confluence_prepare(ctx, group)
            async with confluence_factory(current) as client:
                return await action(client, settings, args)
        except SecureJiraError as e:
            raise ToolError(str(e)) from e

    async def preview_confluence(ctx: Context, name: str, arguments: Dict[str, Any]) -> str:
        operation = CONFLUENCE_CHANGES.get(name)
        if operation is None:
            raise ValidationError(f"Unknown Confluence change '{name}'.")
        current = confluence_prepare(ctx, operation.group, change=True)
        args = parse_change(name, arguments)
        async with confluence_factory(current) as client:
            text = await preview_confluence_change_impl(client, name, args, settings)
        return f"{text}\n\nSite: {current.base_url}"

    async def change_confluence(
        ctx: Context,
        name: str,
        arguments: Dict[str, Any],
        approved: bool,
    ) -> Dict[str, Any]:
        try:
            operation = CONFLUENCE_CHANGES.get(name)
            if operation is None:
                raise ValidationError(f"Unknown Confluence change '{name}'.")
            current = confluence_prepare(ctx, operation.group, change=True)
            args = parse_change(name, arguments)
        except SecureJiraError as e:
            raise ToolError(str(e)) from e
        target = operation.target(args)
        if not approved:
            audit.record(current.principal, current.base_url, name, target, "declined", arguments)
            return {"status": "declined", "message": "The user did not approve this change, so nothing was changed."}
        try:
            async with confluence_factory(current) as client:
                result = await run_confluence_change_impl(client, name, args, settings)
        except SecureJiraError as e:
            audit.record(current.principal, current.base_url, name, target, "failed", arguments, str(e))
            raise ToolError(str(e)) from e
        audit.record(current.principal, current.base_url, name, target, "done", arguments)
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
        """Direct Jira connection check and project list. Call this tool for Jira status; do not search code or delegate."""
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

    if settings.confluence_url or confluence_allowed_hosts:
        @server.tool(annotations=read_only)
        async def search_confluence_pages(
            cql: str,
            ctx: Context,
            max_results: int = 50,
            start: int = 0,
        ) -> Dict[str, Any]:
            """Search Confluence pages with CQL. Use next_start to request another capped page."""
            return await confluence_read(
                ctx, "confluence_read", search_confluence_pages_impl,
                parse_confluence_read_args(
                    SearchPagesArgs, {"cql": cql, "max_results": max_results, "start": start}
                ),
            )

        @server.tool(annotations=read_only)
        async def get_confluence_page(page_id: str, ctx: Context) -> Dict[str, Any]:
            """Read a Confluence page's plain-text body and metadata."""
            return await confluence_read(
                ctx, "confluence_read", get_confluence_page_impl,
                parse_confluence_read_args(PageIdArgs, {"page_id": page_id}),
            )

        @server.tool(annotations=read_only)
        async def list_confluence_spaces(ctx: Context, max_results: int = 50, start: int = 0) -> Dict[str, Any]:
            """Direct Confluence connection check and space list. Call this tool for Confluence status; do not search code or delegate."""
            return await confluence_read(
                ctx, "confluence_read", list_confluence_spaces_impl,
                parse_confluence_read_args(ListSpacesArgs, {"max_results": max_results, "start": start}),
            )

        @server.tool(annotations=read_only)
        async def list_page_comments(
            page_id: str,
            ctx: Context,
            max_results: int = 50,
            start: int = 0,
        ) -> Dict[str, Any]:
            """List comments on a Confluence page."""
            return await confluence_read(
                ctx,
                "confluence_read",
                list_confluence_comments_impl,
                parse_confluence_read_args(ListCommentsArgs, {
                    "page_id": page_id, "max_results": max_results, "start": start,
                }),
            )

        @server.tool(annotations=read_only)
        async def list_page_attachments(
            page_id: str,
            ctx: Context,
            max_results: int = 50,
            start: int = 0,
        ) -> Dict[str, Any]:
            """List attachment metadata for a Confluence page."""
            return await confluence_read(
                ctx,
                "confluence_attachments",
                list_confluence_attachments_impl,
                parse_confluence_read_args(ListAttachmentsArgs, {
                    "page_id": page_id, "max_results": max_results, "start": start,
                }),
            )

        @server.tool(annotations=read_only)
        async def download_confluence_attachment(attachment_id: str, ctx: Context) -> Dict[str, Any]:
            """Download one attachment as base64, subject to MCP_MAX_ATTACHMENT_BYTES."""
            return await confluence_read(
                ctx,
                "confluence_attachments",
                download_confluence_attachment_impl,
                parse_confluence_read_args(AttachmentIdArgs, {"attachment_id": attachment_id}),
            )

        @server.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
        def list_confluence_operations(group: Optional[str] = None) -> Dict[str, Any]:
            """List available approval-gated Confluence changes and their strict arguments."""
            try:
                groups = settings.groups()
                selected = [group] if group else ["confluence_write", "confluence_attachments"]
                result = []
                for selected_group in selected:
                    if selected_group not in ("confluence_write", "confluence_attachments"):
                        raise ValidationError("Choose confluence_write or confluence_attachments.")
                    if selected_group not in groups:
                        raise PolicyError(f"The '{selected_group}' group is turned off by the administrator.")
                    if settings.mcp_read_only:
                        continue
                    result.extend({
                        "operation": op.name,
                        "group": op.group,
                        "description": op.description,
                        "arguments": op.args.model_json_schema(),
                    } for op in CONFLUENCE_CHANGES.values() if op.group == selected_group)
                return {"operations": result, "read_only": settings.mcp_read_only}
            except SecureJiraError as e:
                raise ToolError(str(e)) from e

        @server.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
        async def preview_confluence_change(
            operation: str,
            arguments: Dict[str, Any],
            ctx: Context,
        ) -> Dict[str, Any]:
            """Preview a Confluence page, comment, or attachment change without performing it."""
            try:
                return {"preview": await preview_confluence(ctx, operation, arguments or {})}
            except SecureJiraError as e:
                raise ToolError(str(e)) from e

        confluence_change_doc = (
            "Run an approval-gated Confluence change from list_confluence_operations. "
            "The exact effect is shown before it runs."
        )
        if settings.mcp_confirmation_mode == "elicit":
            async def ask_confluence_user(
                operation: str,
                arguments: Dict[str, Any],
                ctx: Context,
            ) -> Elicit[Approval]:
                capabilities = ctx.client_capabilities
                if not (capabilities and capabilities.elicitation):
                    raise ToolError(
                        "This client cannot show approval prompts (MCP elicitation), so Confluence changes are refused. "
                        "Use a client that supports it, or set MCP_CONFIRMATION_MODE=client if the client has its own approval step."
                    )
                try:
                    text = await preview_confluence(ctx, operation, arguments or {})
                except SecureJiraError as e:
                    raise ToolError(str(e)) from e
                return Elicit(f"{text}\n\nApprove this change?", Approval)

            async def run_confluence_change(
                operation: str,
                arguments: Dict[str, Any],
                ctx: Context,
                decision: Annotated[ElicitationResult[Approval], Resolve(ask_confluence_user)],
            ) -> Dict[str, Any]:
                approved = isinstance(decision, AcceptedElicitation) and bool(decision.data and decision.data.approve)
                return await change_confluence(ctx, operation, arguments or {}, approved)
        else:
            async def run_confluence_change(
                operation: str,
                arguments: Dict[str, Any],
                ctx: Context,
            ) -> Dict[str, Any]:
                return await change_confluence(ctx, operation, arguments or {}, True)

        server.tool(
            name="run_confluence_change",
            description=confluence_change_doc,
            annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True),
        )(run_confluence_change)

    return server
