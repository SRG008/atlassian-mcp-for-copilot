"""Every Jira operation the server can run, with its group, kind, strict arguments, and a preview for changes.

Only a few core tools are always listed to the model; the rest are loaded on demand through
list_operations and run with run_read / run_change, so each request carries few tool definitions.
"""

import json
from dataclasses import dataclass
from typing import Annotated, Any, Awaitable, Callable, Dict, List, Literal, Optional, Type

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from secure_jira_mcp.config import Settings
from secure_jira_mcp.errors import JiraError, ValidationError
from secure_jira_mcp.fields import FieldCatalog, field_catalog, simple_value
from secure_jira_mcp.formatting import (
    DEFAULT_SECTIONS,
    SECTION_FIELDS,
    compact_row,
    fields_for,
    issue_details,
)
from secure_jira_mcp.jira_client import JiraClient

Kind = Literal["read", "change", "destructive"]
PAGE_SIZE = 100
TEXT_TYPES = ("text/", "application/json", "application/xml", "application/x-yaml", "application/csv", "application/x-ndjson")
TEXT_EXTENSIONS = (".txt", ".log", ".json", ".xml", ".csv", ".yaml", ".yml", ".md", ".ini", ".cfg", ".conf", ".har", ".out")
MAX_ATTACHMENT_TEXT_CHARS = 50_000
# Standard fields update_issue may change. Anything else must be named in JIRA_UPDATABLE_CUSTOM_FIELDS.
SAFE_UPDATE_FIELDS = {"summary", "description", "priority", "labels", "components", "fixVersions", "duedate", "environment"}
# Fields with their own argument (or that decide where an item lives); extra_fields may never set them,
# so the approval prompt always shows them as the named arguments say.
RESERVED_FIELDS = {
    "project", "issuetype", "parent", "summary", "description", "priority", "labels", "components",
    "fixVersions", "assignee", "reporter", "status", "resolution", "duedate", "issuelinks", "attachment",
}


def _upper(value: Any) -> Any:
    return value.strip().upper() if isinstance(value, str) else value


IssueKey = Annotated[str, BeforeValidator(_upper), Field(pattern=r"^[A-Z][A-Z0-9_]+-\d+$", description="Work item key, e.g. PROJ-123.")]
ProjectKey = Annotated[str, BeforeValidator(_upper), Field(pattern=r"^[A-Z][A-Z0-9_]+$", description="Project key, e.g. PROJ.")]
Summary = Annotated[str, Field(min_length=1, max_length=255)]
LongText = Annotated[str, Field(max_length=32_000)]
Label = Annotated[str, Field(pattern=r"^\S{1,255}$")]


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass
class Env:
    client: JiraClient
    settings: Settings

    async def catalog(self) -> FieldCatalog:
        return await field_catalog(self.client)


@dataclass(frozen=True)
class Operation:
    name: str
    group: str
    kind: Kind
    description: str
    args: Type[Args]
    run: Callable[[Env, Any], Awaitable[Any]]
    preview: Optional[Callable[[Env, Any], Awaitable[str]]] = None

    def target(self, args: Args) -> str:
        for attr in ("issue_key", "project_key", "sprint_id", "attachment_id"):
            value = getattr(args, attr, None)
            if value:
                return str(value)
        return ""

    def parse(self, arguments: Dict[str, Any]) -> Args:
        try:
            return self.args.model_validate(arguments or {})
        except Exception as e:  # pydantic.ValidationError, shown compactly
            errors = getattr(e, "errors", lambda: [])()
            details = "; ".join(f"{'.'.join(str(p) for p in err.get('loc', ()))}: {err.get('msg')}" for err in errors)
            raise ValidationError(f"Invalid arguments for {self.name}: {details or e}") from e


REGISTRY: Dict[str, Operation] = {}


def operation(name: str, group: str, kind: Kind, description: str, args: Type[Args], preview=None):
    def register(run):
        REGISTRY[name] = Operation(name, group, kind, description, args, run, preview)
        return run

    return register


# ---------------------------------------------------------------- search & read


class SearchArgs(Args):
    jql: Annotated[str, Field(min_length=1, max_length=4000, description="JQL, e.g. project = PROJ AND issuetype = Story ORDER BY updated DESC")]
    max_results: Optional[int] = Field(None, ge=1, le=1000, description="Cap on returned items (default: the server limit).")
    extra_fields: List[str] = Field(default_factory=list, max_length=10, description="Extra columns by field name, e.g. Sprint, Story Points.")
    count_only: bool = Field(False, description="Return only the number of matching items.")


@operation("search_issues", "search", "read", "Search work items with JQL. Follows every page and returns compact rows.", SearchArgs)
async def search_issues(env: Env, args: SearchArgs) -> Dict[str, Any]:
    if args.count_only:
        if env.client.deployment != "cloud":
            page = await env.client.post("/rest/api/2/search", {"jql": args.jql, "maxResults": 0, "fields": ["key"]})
            return {"total": (page or {}).get("total", 0)}
        total = 0
        start = 0
        while True:
            page = await env.client.search_page(args.jql, start, PAGE_SIZE, ["key"]) or {}
            if page.get("total") is not None:
                return {"total": int(page["total"])}
            issues = page.get("issues") or []
            total += len(issues)
            if page.get("isLast") is True:
                return {"total": total}
            start += len(issues)
    catalog = await env.catalog()
    extra_ids = []
    for name in args.extra_fields:
        field = catalog.find(name)
        if field is None:
            raise ValidationError(f"Unknown field '{name}'. Use list_fields to find the exact name.")
        extra_ids.append(field.id)
    limit = min(args.max_results or env.settings.mcp_max_results, env.settings.mcp_max_results)
    fields = ["summary", "issuetype", "status", "priority", "assignee", "updated", *extra_ids]
    issues: List[Dict[str, Any]] = []
    total = 0
    total_known = False
    is_last = False
    while len(issues) < limit:
        page = await env.client.search_page(
            args.jql, len(issues), min(PAGE_SIZE, limit - len(issues)), fields
        ) or {}
        batch = page.get("issues") or []
        reported_total = page.get("total")
        if reported_total is not None:
            total = int(reported_total)
            total_known = True
        issues.extend(batch)
        is_last = page.get("isLast") is True or (
            env.client.deployment == "cloud" and not page.get("nextPageToken")
        )
        if not batch or (reported_total is not None and len(issues) >= total) or is_last:
            break
    complete = is_last if env.client.deployment == "cloud" else len(issues) >= total
    if env.client.deployment == "cloud" and not total_known and is_last:
        total = len(issues)
        total_known = True
    return {
        "total": total if total_known else None,
        "returned": len(issues),
        "complete": complete,
        "note": "" if complete else (
            f"Showing the first {len(issues)} of {total}. Narrow the JQL or raise max_results to see more."
            if total else f"More results may be available after the first {len(issues)} items. Narrow the JQL or raise max_results."
        ),
        "issues": [compact_row(issue, catalog, extra_ids) for issue in issues],
    }


SectionName = Literal["details", "people", "dates", "agile", "attachments", "links", "subtasks", "time", "custom_fields", "comments", "history", "all"]


class GetIssueArgs(Args):
    issue_key: IssueKey
    sections: List[SectionName] = Field(
        default_factory=lambda: list(DEFAULT_SECTIONS),
        description="Parts to return. Default: details, people, dates, agile, attachments, links, subtasks, comments. "
        "Add custom_fields, time or history when needed, or 'all'.",
    )
    comment_limit: int = Field(10, ge=0, le=100, description="Latest comments to include.")


@operation("get_issue", "read", "read", "One work item with the requested sections (attachments, sprint, links, comments, ...).", GetIssueArgs)
async def get_issue(env: Env, args: GetIssueArgs) -> Dict[str, Any]:
    sections = set(SECTION_FIELDS) if "all" in args.sections else set(args.sections)
    catalog = await env.catalog()
    fields = fields_for(sections, catalog) or ["summary"]
    params: Dict[str, Any] = {"fields": ",".join(fields)}
    if "history" in sections:
        params["expand"] = "changelog"
    issue = await env.client.get(f"/rest/api/2/issue/{args.issue_key}", params)
    comments = None
    if "comments" in sections and args.comment_limit:
        comments = await _latest_comments(env, args.issue_key, args.comment_limit)
    return issue_details(issue, catalog, sections, env.client.base_url, comments)


async def _latest_comments(env: Env, issue_key: str, limit: int) -> List[Dict[str, Any]]:
    first = await env.client.get(f"/rest/api/2/issue/{issue_key}/comment", {"maxResults": 0}) or {}
    total = int(first.get("total") or 0)
    if not total:
        return []
    page = await env.client.get(
        f"/rest/api/2/issue/{issue_key}/comment", {"startAt": max(0, total - limit), "maxResults": limit}
    ) or {}
    return page.get("comments") or []


class NoArgs(Args):
    pass


@operation("list_projects", "read", "read", "Projects you can see (key, name, type).", NoArgs)
async def list_projects(env: Env, _: NoArgs) -> Dict[str, Any]:
    projects = await env.client.get("/rest/api/2/project") or []
    return {"projects": [{"key": p.get("key"), "name": p.get("name"), "type": p.get("projectTypeKey")} for p in projects]}


class ProjectArgs(Args):
    project_key: ProjectKey


@operation("get_project", "read", "read", "A project's lead, description, issue types, components and versions.", ProjectArgs)
async def get_project(env: Env, args: ProjectArgs) -> Dict[str, Any]:
    p = await env.client.get(f"/rest/api/2/project/{args.project_key}") or {}
    return {
        "key": p.get("key"),
        "name": p.get("name"),
        "lead": simple_value(p.get("lead")),
        "description": p.get("description"),
        "issue_types": [t.get("name") for t in p.get("issueTypes") or []],
        "components": [c.get("name") for c in p.get("components") or []],
        "versions": [{"name": v.get("name"), "released": v.get("released")} for v in p.get("versions") or []],
    }


@operation("list_issue_types", "read", "read", "Work item types of a project (Bug, Story, Task, Sub-task, ...).", ProjectArgs)
async def list_issue_types(env: Env, args: ProjectArgs) -> Dict[str, Any]:
    p = await env.client.get(f"/rest/api/2/project/{args.project_key}") or {}
    return {"issue_types": [{"id": t.get("id"), "name": t.get("name"), "subtask": t.get("subtask")} for t in p.get("issueTypes") or []]}


class IssueArgs(Args):
    issue_key: IssueKey


@operation("get_transitions", "read", "read", "Status changes currently possible for a work item.", IssueArgs)
async def get_transitions(env: Env, args: IssueArgs) -> Dict[str, Any]:
    data = await env.client.get(f"/rest/api/2/issue/{args.issue_key}/transitions") or {}
    return {"transitions": [{"id": t.get("id"), "name": t.get("name"), "to": simple_value(t.get("to"))} for t in data.get("transitions") or []]}


@operation("list_components", "read", "read", "Components of a project.", ProjectArgs)
async def list_components(env: Env, args: ProjectArgs) -> Dict[str, Any]:
    items = await env.client.get(f"/rest/api/2/project/{args.project_key}/components") or []
    return {"components": [{"name": c.get("name"), "lead": simple_value(c.get("lead"))} for c in items]}


@operation("list_versions", "read", "read", "Versions (releases) of a project.", ProjectArgs)
async def list_versions(env: Env, args: ProjectArgs) -> Dict[str, Any]:
    items = await env.client.get(f"/rest/api/2/project/{args.project_key}/versions") or []
    return {"versions": [{"name": v.get("name"), "released": v.get("released"), "release_date": v.get("releaseDate")} for v in items]}


# ---------------------------------------------------------------- attachments


class AttachmentArgs(Args):
    attachment_id: Annotated[str, Field(pattern=r"^\d+$", description="Attachment id from get_issue.")]


def _is_text(content_type: str, filename: str = "") -> bool:
    content_type = content_type.split(";")[0].strip().lower()
    return content_type.startswith(TEXT_TYPES) or filename.lower().endswith(TEXT_EXTENSIONS)


@operation("get_attachment_text", "attachments", "read", "The text of a text-like attachment (logs, JSON, XML, CSV), size-limited.", AttachmentArgs)
async def get_attachment_text(env: Env, args: AttachmentArgs) -> Dict[str, Any]:
    meta = await env.client.get(f"/rest/api/2/attachment/{args.attachment_id}") or {}
    filename = str(meta.get("filename") or "")
    if not _is_text(str(meta.get("mimeType") or ""), filename):
        raise ValidationError(f"{filename} is not a text file ({meta.get('mimeType')}); only text attachments can be read.")
    if int(meta.get("size") or 0) > env.settings.mcp_max_attachment_bytes:
        raise ValidationError(f"{filename} is larger than the {env.settings.mcp_max_attachment_bytes:,} byte limit.")
    data = await env.client.download(
        str(meta.get("content") or ""), env.settings.mcp_max_attachment_bytes, lambda ct: _is_text(ct, filename)
    )
    text = data.decode("utf-8", errors="replace")
    truncated = len(text) > MAX_ATTACHMENT_TEXT_CHARS
    return {"filename": filename, "truncated": truncated, "text": text[:MAX_ATTACHMENT_TEXT_CHARS]}


# ---------------------------------------------------------------- comments


class CommentListArgs(Args):
    issue_key: IssueKey
    limit: int = Field(20, ge=1, le=100)


@operation("list_comments", "comments", "read", "The latest comments of a work item.", CommentListArgs)
async def list_comments(env: Env, args: CommentListArgs) -> Dict[str, Any]:
    comments = await _latest_comments(env, args.issue_key, args.limit)
    return {"comments": [{"id": c.get("id"), "author": simple_value(c.get("author")), "created": c.get("created"), "body": c.get("body")} for c in comments]}


class AddCommentArgs(Args):
    issue_key: IssueKey
    body: Annotated[str, Field(min_length=1, max_length=32_000)]


async def _preview_comment(env: Env, args: AddCommentArgs) -> str:
    issue = await env.client.get(f"/rest/api/2/issue/{args.issue_key}", {"fields": "summary"}) or {}
    return f"Add a comment to {args.issue_key} ({(issue.get('fields') or {}).get('summary')}):\n\n{args.body}"


@operation("add_comment", "comments", "change", "Add a comment to a work item.", AddCommentArgs, _preview_comment)
async def add_comment(env: Env, args: AddCommentArgs) -> Dict[str, Any]:
    created = await env.client.post(f"/rest/api/2/issue/{args.issue_key}/comment", {"body": args.body}) or {}
    return {"comment_id": created.get("id"), "issue_key": args.issue_key}


# ---------------------------------------------------------------- fields & users


class FieldListArgs(Args):
    query: str = Field("", max_length=100, description="Part of a field name; empty lists custom fields.")


@operation("list_fields", "fields", "read", "Field names and ids, filtered by a name fragment.", FieldListArgs)
async def list_fields(env: Env, args: FieldListArgs) -> Dict[str, Any]:
    catalog = await env.catalog()
    query = args.query.strip().lower()
    if query:
        matches = [f for f in catalog.by_id.values() if query in f.name.lower()]
    else:
        matches = [f for f in catalog.by_id.values() if f.custom]
    return {"fields": [{"id": f.id, "name": f.name, "custom": f.custom} for f in matches[:100]], "more": len(matches) > 100}


class CreateFieldsArgs(Args):
    project_key: ProjectKey
    issue_type: Annotated[str, Field(min_length=1, max_length=100)]


async def _create_fields(env: Env, project_key: str, issue_type: str) -> List[Dict[str, Any]]:
    try:
        types = await env.client.get(f"/rest/api/2/issue/createmeta/{project_key}/issuetypes") or {}
        match = next((t for t in types.get("values") or [] if str(t.get("name", "")).lower() == issue_type.lower()), None)
        if match is None:
            raise ValidationError(f"{project_key} has no work item type '{issue_type}'. Use list_issue_types.")
        meta = await env.client.get(f"/rest/api/2/issue/createmeta/{project_key}/issuetypes/{match['id']}", {"maxResults": 200}) or {}
        return list(meta.get("values") or [])
    except JiraError:
        # Jira older than 8.4 only has the legacy createmeta endpoint.
        legacy = await env.client.get(
            "/rest/api/2/issue/createmeta",
            {"projectKeys": project_key, "issuetypeNames": issue_type, "expand": "projects.issuetypes.fields"},
        ) or {}
        for project in legacy.get("projects") or []:
            for kind in project.get("issuetypes") or []:
                return [{"fieldId": fid, **meta} for fid, meta in (kind.get("fields") or {}).items()]
        raise ValidationError(f"{project_key} has no work item type '{issue_type}'. Use list_issue_types.")


def _describe_field(meta: Dict[str, Any]) -> Dict[str, Any]:
    allowed = [simple_value(v) for v in meta.get("allowedValues") or []][:50]
    return {
        "field_id": meta.get("fieldId"),
        "name": meta.get("name"),
        "required": bool(meta.get("required")),
        "type": (meta.get("schema") or {}).get("type"),
        **({"allowed_values": allowed} if allowed else {}),
    }


@operation("get_create_fields", "fields", "read", "Fields of a work item type in a project, which are required, and allowed values.", CreateFieldsArgs)
async def get_create_fields(env: Env, args: CreateFieldsArgs) -> Dict[str, Any]:
    metas = await _create_fields(env, args.project_key, args.issue_type)
    described = [_describe_field(m) for m in metas if m.get("fieldId") not in ("project", "issuetype")]
    return {"required": [d for d in described if d["required"]], "optional": [d for d in described if not d["required"]][:40]}


class UserSearchArgs(Args):
    query: Annotated[str, Field(min_length=2, max_length=100, description="Part of a name, username or email.")]


@operation("search_users", "users", "read", "Find users; returns the username to use for assignee.", UserSearchArgs)
async def search_users(env: Env, args: UserSearchArgs) -> Dict[str, Any]:
    users = await env.client.get("/rest/api/2/user/search", {"username": args.query, "maxResults": 20}) or []
    return {"users": [{"username": u.get("name"), "display_name": u.get("displayName"), "active": u.get("active")} for u in users]}


# ---------------------------------------------------------------- agile


class BoardArgs(Args):
    project_key: Optional[ProjectKey] = None
    name: Optional[Annotated[str, Field(max_length=100)]] = None


@operation("list_boards", "agile", "read", "Scrum and Kanban boards, optionally of one project.", BoardArgs)
async def list_boards(env: Env, args: BoardArgs) -> Dict[str, Any]:
    params: Dict[str, Any] = {"maxResults": 50}
    if args.project_key:
        params["projectKeyOrId"] = args.project_key
    if args.name:
        params["name"] = args.name
    data = await env.client.get("/rest/agile/1.0/board", params) or {}
    return {"boards": [{"id": b.get("id"), "name": b.get("name"), "type": b.get("type")} for b in data.get("values") or []]}


class SprintListArgs(Args):
    board_id: int = Field(..., ge=1)
    state: Optional[Literal["active", "future", "closed", "active,future"]] = None


@operation("list_sprints", "agile", "read", "Sprints of a board, optionally by state.", SprintListArgs)
async def list_sprints(env: Env, args: SprintListArgs) -> Dict[str, Any]:
    sprints: List[Dict[str, Any]] = []
    while True:
        params: Dict[str, Any] = {"startAt": len(sprints), "maxResults": 50}
        if args.state:
            params["state"] = args.state
        page = await env.client.get(f"/rest/agile/1.0/board/{args.board_id}/sprint", params) or {}
        batch = page.get("values") or []
        sprints.extend(batch)
        if page.get("isLast", True) or not batch or len(sprints) >= 500:
            break
    keys = ("id", "name", "state", "startDate", "endDate", "goal")
    return {"sprints": [{k: s.get(k) for k in keys if s.get(k) is not None} for s in sprints]}


class SprintArgs(Args):
    sprint_id: int = Field(..., ge=1)


@operation("get_sprint_issues", "agile", "read", "All work items in a sprint, as compact rows.", SprintArgs)
async def get_sprint_issues(env: Env, args: SprintArgs) -> Dict[str, Any]:
    limit = env.settings.mcp_max_results
    issues: List[Dict[str, Any]] = []
    total = 0
    while len(issues) < limit:
        page = await env.client.get(
            f"/rest/agile/1.0/sprint/{args.sprint_id}/issue",
            {"startAt": len(issues), "maxResults": min(PAGE_SIZE, limit - len(issues)), "fields": "summary,issuetype,status,priority,assignee,updated"},
        ) or {}
        batch = page.get("issues") or []
        total = int(page.get("total") or 0)
        issues.extend(batch)
        if not batch or len(issues) >= total:
            break
    return {"total": total, "returned": len(issues), "complete": len(issues) >= total, "issues": [compact_row(i) for i in issues]}


class AddToSprintArgs(Args):
    sprint_id: int = Field(..., ge=1)
    issue_keys: List[IssueKey] = Field(..., min_length=1, max_length=50)


async def _preview_sprint(env: Env, args: AddToSprintArgs) -> str:
    sprint = await env.client.get(f"/rest/agile/1.0/sprint/{args.sprint_id}") or {}
    return f"Move {len(args.issue_keys)} work item(s) into sprint '{sprint.get('name')}' ({sprint.get('state')}): {', '.join(args.issue_keys)}"


@operation("add_issues_to_sprint", "agile", "change", "Move work items into a sprint.", AddToSprintArgs, _preview_sprint)
async def add_issues_to_sprint(env: Env, args: AddToSprintArgs) -> Dict[str, Any]:
    await env.client.post(f"/rest/agile/1.0/sprint/{args.sprint_id}/issue", {"issues": list(args.issue_keys)})
    return {"sprint_id": args.sprint_id, "moved": list(args.issue_keys)}


# ---------------------------------------------------------------- create, update, assign, transition, link


class CreateIssueArgs(Args):
    project_key: ProjectKey
    issue_type: Annotated[str, Field(min_length=1, max_length=100, description="Exact type name, e.g. Story, Bug, Task, Sub-task.")]
    summary: Summary
    description: Optional[LongText] = None
    priority: Optional[Annotated[str, Field(max_length=50)]] = None
    labels: List[Label] = Field(default_factory=list, max_length=20)
    components: List[Annotated[str, Field(max_length=255)]] = Field(default_factory=list, max_length=20)
    assignee: Optional[Annotated[str, Field(max_length=255, description="Username from search_users.")]] = None
    parent_key: Optional[IssueKey] = Field(None, description="Parent work item, for sub-tasks.")
    epic_key: Optional[IssueKey] = None
    story_points: Optional[float] = Field(None, ge=0, le=1000)
    extra_fields: Dict[str, Any] = Field(
        default_factory=dict,
        description="Other fields by name: required fields from get_create_fields, or fields the administrator allowed.",
    )


def _convert(meta: Optional[Dict[str, Any]], value: Any) -> Any:
    """Plain answers to the JSON shape Jira expects for the field's schema."""
    schema = (meta or {}).get("schema") or {}
    kind, items = schema.get("type"), schema.get("items")
    if isinstance(value, (dict, list)) and kind not in ("array",):
        return value
    if kind == "option":
        return {"value": value}
    if kind in ("priority", "version", "component", "issuetype", "resolution"):
        return {"name": value}
    if kind == "user":
        return {"name": value}
    if kind == "array":
        values = value if isinstance(value, list) else [v.strip() for v in str(value).split(",") if v.strip()]
        if items == "option":
            return [{"value": v} for v in values]
        if items in ("version", "component", "user"):
            return [{"name": v} for v in values]
        return values
    if kind == "number":
        return float(value)
    return value


async def _resolve_extra(env: Env, extra: Dict[str, Any], allowed_ids: set) -> Dict[str, Any]:
    if not extra:
        return {}
    catalog = await env.catalog()
    allowed_custom = env.settings.updatable_custom_fields()
    resolved = {}
    for name, value in extra.items():
        field = catalog.find(name)
        if field is None:
            raise ValidationError(f"Unknown field '{name}'. Use get_create_fields or list_fields.")
        if field.id in RESERVED_FIELDS:
            raise ValidationError(f"'{field.name}' cannot be set through extra_fields; use its own argument.")
        if field.id not in allowed_ids and field.name.lower() not in allowed_custom:
            raise ValidationError(
                f"Field '{field.name}' may not be set by this server. An administrator can allow it in JIRA_UPDATABLE_CUSTOM_FIELDS."
            )
        resolved[field.id] = value
    return resolved


async def _create_payload(env: Env, args: CreateIssueArgs) -> Dict[str, Any]:
    metas = {m.get("fieldId"): m for m in await _create_fields(env, args.project_key, args.issue_type)}
    catalog = await env.catalog()
    fields: Dict[str, Any] = {"project": {"key": args.project_key}, "issuetype": {"name": args.issue_type}, "summary": args.summary}
    if args.description:
        fields["description"] = args.description
    if args.priority:
        fields["priority"] = {"name": args.priority}
    if args.labels:
        fields["labels"] = list(args.labels)
    if args.components:
        fields["components"] = [{"name": c} for c in args.components]
    if args.assignee:
        fields["assignee"] = {"name": args.assignee}
    if args.parent_key:
        fields["parent"] = {"key": args.parent_key}
    if args.epic_key:
        epic_ids = catalog.epic_link_ids()
        if not epic_ids:
            raise ValidationError("This Jira has no Epic Link field.")
        fields[epic_ids[0]] = args.epic_key
    if args.story_points is not None:
        point_ids = [fid for fid in catalog.story_point_ids() if fid in metas] or catalog.story_point_ids()
        if not point_ids:
            raise ValidationError("This Jira has no Story Points field.")
        fields[point_ids[0]] = args.story_points
    required_ids = {fid for fid, m in metas.items() if m.get("required")}
    for field_id, value in (await _resolve_extra(env, args.extra_fields, required_ids)).items():
        fields[field_id] = _convert(metas.get(field_id), value)
    missing = [m.get("name") or fid for fid, m in metas.items() if m.get("required") and fid not in fields and not m.get("hasDefaultValue")]
    if missing:
        raise ValidationError(f"Missing required fields: {', '.join(missing)}. Ask the user and pass them in extra_fields.")
    return fields


def _shown(value: Any) -> str:
    """The full value as the user will see it in the approval prompt; never shortened, so nothing is written unseen."""
    shown = simple_value(value) if isinstance(value, (dict, list)) else value
    return shown if isinstance(shown, str) else json.dumps(shown, ensure_ascii=False)


def _bullet(fields: Dict[str, Any], catalog: FieldCatalog) -> str:
    return "\n".join(f"- {catalog.name(field_id)}: {_shown(value)}" for field_id, value in fields.items())


async def _preview_create(env: Env, args: CreateIssueArgs) -> str:
    fields = await _create_payload(env, args)
    return f"Create a new work item with exactly these fields:\n{_bullet(fields, await env.catalog())}"


@operation("create_issue", "write", "change", "Create a work item. Check required fields first with get_create_fields.", CreateIssueArgs, _preview_create)
async def create_issue(env: Env, args: CreateIssueArgs) -> Dict[str, Any]:
    created = await env.client.post("/rest/api/2/issue", {"fields": await _create_payload(env, args)}) or {}
    key = created.get("key")
    return {"key": key, "url": f"{env.client.base_url}/browse/{key}"}


class UpdateIssueArgs(Args):
    issue_key: IssueKey
    summary: Optional[Summary] = None
    description: Optional[LongText] = None
    priority: Optional[Annotated[str, Field(max_length=50)]] = None
    labels: Optional[List[Label]] = Field(None, max_length=20, description="Replaces all labels.")
    components: Optional[List[Annotated[str, Field(max_length=255)]]] = Field(None, max_length=20)
    fix_versions: Optional[List[Annotated[str, Field(max_length=255)]]] = Field(None, max_length=20)
    due_date: Optional[Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]] = None
    story_points: Optional[float] = Field(None, ge=0, le=1000)
    extra_fields: Dict[str, Any] = Field(default_factory=dict, description="Only fields the administrator allowed.")

    @model_validator(mode="after")
    def _something_to_change(self):
        if not any(v is not None and v != {} for k, v in self.__dict__.items() if k != "issue_key"):
            raise ValueError("Give at least one field to change.")
        return self


async def _update_fields(env: Env, args: UpdateIssueArgs) -> Dict[str, Any]:
    fields: Dict[str, Any] = {}
    if args.summary is not None:
        fields["summary"] = args.summary
    if args.description is not None:
        fields["description"] = args.description
    if args.priority is not None:
        fields["priority"] = {"name": args.priority}
    if args.labels is not None:
        fields["labels"] = list(args.labels)
    if args.components is not None:
        fields["components"] = [{"name": c} for c in args.components]
    if args.fix_versions is not None:
        fields["fixVersions"] = [{"name": v} for v in args.fix_versions]
    if args.due_date is not None:
        fields["duedate"] = args.due_date
    catalog = await env.catalog()
    if args.story_points is not None:
        point_ids = catalog.story_point_ids()
        if not point_ids:
            raise ValidationError("This Jira has no Story Points field.")
        fields[point_ids[0]] = args.story_points
    for field_id, value in (await _resolve_extra(env, args.extra_fields, set())).items():
        fields[field_id] = value
    blocked = [f for f in fields if f not in SAFE_UPDATE_FIELDS and f not in catalog.story_point_ids() and catalog.name(f).lower() not in env.settings.updatable_custom_fields()]
    if blocked:
        raise ValidationError(f"These fields may not be changed: {', '.join(catalog.name(f) for f in blocked)}.")
    return fields


async def _preview_update(env: Env, args: UpdateIssueArgs) -> str:
    fields = await _update_fields(env, args)
    current = await env.client.get(f"/rest/api/2/issue/{args.issue_key}", {"fields": ",".join(["summary", *fields])}) or {}
    now = current.get("fields") or {}
    catalog = await env.catalog()
    lines = []
    for field_id, value in fields.items():
        lines.append(f"- {catalog.name(field_id)}: {_shown(now.get(field_id))} → {_shown(value)}")
    return f"Update {args.issue_key} ({now.get('summary')}):\n" + "\n".join(lines)


@operation("update_issue", "write", "change", "Change fields of a work item (summary, description, priority, labels, components, fix versions, due date, story points).", UpdateIssueArgs, _preview_update)
async def update_issue(env: Env, args: UpdateIssueArgs) -> Dict[str, Any]:
    await env.client.put(f"/rest/api/2/issue/{args.issue_key}", {"fields": await _update_fields(env, args)})
    return {"key": args.issue_key, "updated": True}


class AssignArgs(Args):
    issue_key: IssueKey
    assignee: Annotated[str, Field(max_length=255, description="Username from search_users; empty to unassign.")]


async def _preview_assign(env: Env, args: AssignArgs) -> str:
    issue = await env.client.get(f"/rest/api/2/issue/{args.issue_key}", {"fields": "summary,assignee"}) or {}
    fields = issue.get("fields") or {}
    return f"Assign {args.issue_key} ({fields.get('summary')}): {simple_value(fields.get('assignee')) or 'Unassigned'} → {args.assignee or 'Unassigned'}"


@operation("assign_issue", "write", "change", "Assign a work item to a user, or unassign it.", AssignArgs, _preview_assign)
async def assign_issue(env: Env, args: AssignArgs) -> Dict[str, Any]:
    await env.client.put(f"/rest/api/2/issue/{args.issue_key}/assignee", {"name": args.assignee or None})
    return {"key": args.issue_key, "assignee": args.assignee or None}


class TransitionArgs(Args):
    issue_key: IssueKey
    to: Annotated[str, Field(min_length=1, max_length=100, description="Transition name or target status, e.g. Done.")]
    comment: Optional[LongText] = None


async def _find_transition(env: Env, args: TransitionArgs) -> Dict[str, Any]:
    data = await env.client.get(f"/rest/api/2/issue/{args.issue_key}/transitions") or {}
    wanted = args.to.strip().lower()
    options = data.get("transitions") or []
    for t in options:
        if wanted in (str(t.get("name", "")).lower(), str(simple_value(t.get("to")) or "").lower(), str(t.get("id"))):
            return t
    names = ", ".join(f"{t.get('name')} (to {simple_value(t.get('to'))})" for t in options) or "none"
    raise ValidationError(f"'{args.to}' is not possible for {args.issue_key} now. Possible: {names}.")


async def _preview_transition(env: Env, args: TransitionArgs) -> str:
    transition = await _find_transition(env, args)
    issue = await env.client.get(f"/rest/api/2/issue/{args.issue_key}", {"fields": "summary,status"}) or {}
    fields = issue.get("fields") or {}
    text = f"Move {args.issue_key} ({fields.get('summary')}): {simple_value(fields.get('status'))} → {simple_value(transition.get('to'))} (transition '{transition.get('name')}')"
    return text + (f"\nwith comment:\n{args.comment}" if args.comment else "")


@operation("transition_issue", "transitions", "change", "Move a work item to another status.", TransitionArgs, _preview_transition)
async def transition_issue(env: Env, args: TransitionArgs) -> Dict[str, Any]:
    transition = await _find_transition(env, args)
    body: Dict[str, Any] = {"transition": {"id": transition.get("id")}}
    if args.comment:
        body["update"] = {"comment": [{"add": {"body": args.comment}}]}
    await env.client.post(f"/rest/api/2/issue/{args.issue_key}/transitions", body)
    return {"key": args.issue_key, "status": simple_value(transition.get("to"))}


@operation("list_link_types", "links", "read", "Link types (blocks, relates to, duplicates, ...).", NoArgs)
async def list_link_types(env: Env, _: NoArgs) -> Dict[str, Any]:
    data = await env.client.get("/rest/api/2/issueLinkType") or {}
    return {"link_types": [{"name": t.get("name"), "outward": t.get("outward"), "inward": t.get("inward")} for t in data.get("issueLinkTypes") or []]}


class LinkArgs(Args):
    link_type: Annotated[str, Field(min_length=1, max_length=100, description="Link type name from list_link_types.")]
    from_key: IssueKey
    to_key: IssueKey


async def _preview_link(env: Env, args: LinkArgs) -> str:
    return f"Link {args.from_key} → {args.to_key} with '{args.link_type}' ({args.from_key} is the outward side)."


@operation("link_issues", "links", "change", "Link two work items.", LinkArgs, _preview_link)
async def link_issues(env: Env, args: LinkArgs) -> Dict[str, Any]:
    await env.client.post(
        "/rest/api/2/issueLink",
        {"type": {"name": args.link_type}, "outwardIssue": {"key": args.from_key}, "inwardIssue": {"key": args.to_key}},
    )
    return {"linked": [args.from_key, args.to_key], "type": args.link_type}


# ---------------------------------------------------------------- destructive (group "delete", off by default)


class DeleteIssueArgs(Args):
    issue_key: IssueKey
    delete_subtasks: bool = False


async def _preview_delete(env: Env, args: DeleteIssueArgs) -> str:
    issue = await env.client.get(f"/rest/api/2/issue/{args.issue_key}", {"fields": "summary,subtasks"}) or {}
    fields = issue.get("fields") or {}
    subtasks = len(fields.get("subtasks") or [])
    return (
        f"PERMANENTLY DELETE {args.issue_key} ({fields.get('summary')})"
        + (f" and its {subtasks} sub-task(s)" if args.delete_subtasks and subtasks else "")
        + ". This cannot be undone."
    )


@operation("delete_issue", "delete", "destructive", "Permanently delete a work item.", DeleteIssueArgs, _preview_delete)
async def delete_issue(env: Env, args: DeleteIssueArgs) -> Dict[str, Any]:
    await env.client.delete(f"/rest/api/2/issue/{args.issue_key}", {"deleteSubtasks": str(args.delete_subtasks).lower()})
    return {"deleted": args.issue_key}


class DeleteCommentArgs(Args):
    issue_key: IssueKey
    comment_id: Annotated[str, Field(pattern=r"^\d+$")]


async def _preview_delete_comment(env: Env, args: DeleteCommentArgs) -> str:
    comment = await env.client.get(f"/rest/api/2/issue/{args.issue_key}/comment/{args.comment_id}") or {}
    return f"PERMANENTLY DELETE this comment on {args.issue_key} by {simple_value(comment.get('author'))}:\n\n{str(comment.get('body') or '')[:1000]}"


@operation("delete_comment", "delete", "destructive", "Permanently delete a comment.", DeleteCommentArgs, _preview_delete_comment)
async def delete_comment(env: Env, args: DeleteCommentArgs) -> Dict[str, Any]:
    await env.client.delete(f"/rest/api/2/issue/{args.issue_key}/comment/{args.comment_id}")
    return {"deleted_comment": args.comment_id, "issue_key": args.issue_key}
