"""Turns Jira's verbose JSON into compact rows and readable work item details."""

import re
from typing import Any, Dict, Iterable, List, Optional

from secure_jira_mcp.fields import FieldCatalog, simple_value

MAX_TEXT_CHARS = 20_000
MAX_CUSTOM_VALUE_CHARS = 500
# Sections of get_issue and the Jira fields each one needs, so a narrow request fetches less.
SECTION_FIELDS: Dict[str, List[str]] = {
    "details": ["summary", "issuetype", "status", "priority", "resolution", "labels", "components",
                "fixVersions", "versions", "environment", "project", "description"],
    "people": ["assignee", "reporter", "creator", "watches"],
    "dates": ["created", "updated", "resolutiondate", "duedate"],
    "agile": [],  # sprint, epic and story point fields are added from the catalog
    "attachments": ["attachment"],
    "links": ["issuelinks"],
    "subtasks": ["subtasks", "parent"],
    "time": ["timetracking"],
    "custom_fields": ["*all"],
    "comments": [],  # fetched from the comment endpoint
    "history": [],  # fetched with expand=changelog
}
DEFAULT_SECTIONS = ("details", "people", "dates", "agile", "attachments", "links", "subtasks", "comments")
_SPRINT_TEXT = re.compile(r"(\w+)=([^,\]]*)")


def _text(value: Any, limit: int = MAX_TEXT_CHARS) -> Optional[str]:
    if value in (None, ""):
        return None
    text = str(value)
    return text if len(text) <= limit else text[:limit] + " ...[truncated]"


def parse_sprints(value: Any) -> List[Dict[str, Any]]:
    """Sprint values are objects on newer Jira and strings like 'Sprint@1[id=3,state=ACTIVE,name=S1,...]' on older."""
    sprints = []
    for item in value if isinstance(value, list) else [value] if value else []:
        if isinstance(item, dict):
            sprints.append({"id": item.get("id"), "name": item.get("name"), "state": item.get("state")})
        elif isinstance(item, str):
            pairs = dict(_SPRINT_TEXT.findall(item))
            if pairs.get("name"):
                sprints.append({"id": pairs.get("id"), "name": pairs.get("name"), "state": pairs.get("state")})
    return sprints


def agile_fields(catalog: FieldCatalog) -> List[str]:
    return catalog.sprint_ids() + catalog.epic_link_ids() + catalog.story_point_ids()


def fields_for(sections: Iterable[str], catalog: FieldCatalog) -> List[str]:
    wanted: List[str] = []
    for section in sections:
        wanted += SECTION_FIELDS.get(section, [])
        if section == "agile":
            wanted += agile_fields(catalog)
    return list(dict.fromkeys(wanted))


def _agile(fields: Dict[str, Any], catalog: FieldCatalog) -> Dict[str, Any]:
    agile: Dict[str, Any] = {}
    sprints = [s for fid in catalog.sprint_ids() for s in parse_sprints(fields.get(fid))]
    if sprints:
        agile["sprints"] = sprints
    for fid in catalog.epic_link_ids():
        if fields.get(fid):
            agile["epic_link"] = simple_value(fields[fid])
    for fid in catalog.story_point_ids():
        if fields.get(fid) is not None:
            agile["story_points"] = fields[fid]
    return agile


def _link(link: Dict[str, Any]) -> Dict[str, Any]:
    kind = link.get("type") or {}
    if link.get("outwardIssue"):
        other, relation = link["outwardIssue"], kind.get("outward")
    else:
        other, relation = link.get("inwardIssue") or {}, kind.get("inward")
    other_fields = other.get("fields") or {}
    return {
        "relation": relation,
        "key": other.get("key"),
        "summary": other_fields.get("summary"),
        "status": simple_value(other_fields.get("status")),
    }


def compact_row(issue: Dict[str, Any], catalog: Optional[FieldCatalog] = None, extra: Iterable[str] = ()) -> Dict[str, Any]:
    fields = issue.get("fields") or {}
    row = {
        "key": issue.get("key"),
        "summary": fields.get("summary"),
        "type": simple_value(fields.get("issuetype")),
        "status": simple_value(fields.get("status")),
        "priority": simple_value(fields.get("priority")) or "None",
        "assignee": simple_value(fields.get("assignee")) or "Unassigned",
        "updated": str(fields.get("updated") or "")[:10],
    }
    for field_id in extra:
        label = catalog.name(field_id) if catalog else field_id
        value = fields.get(field_id)
        row[label] = [s["name"] for s in parse_sprints(value)] if catalog and field_id in catalog.sprint_ids() else simple_value(value)
    return row


def issue_details(
    issue: Dict[str, Any],
    catalog: FieldCatalog,
    sections: Iterable[str],
    base_url: str,
    comments: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    sections = set(sections)
    fields = issue.get("fields") or {}
    out: Dict[str, Any] = {"key": issue.get("key"), "url": f"{base_url}/browse/{issue.get('key')}"}
    if "details" in sections:
        out.update(
            summary=fields.get("summary"),
            type=simple_value(fields.get("issuetype")),
            project=simple_value(fields.get("project")),
            status=simple_value(fields.get("status")),
            resolution=simple_value(fields.get("resolution")),
            priority=simple_value(fields.get("priority")),
            labels=fields.get("labels") or [],
            components=simple_value(fields.get("components") or []),
            fix_versions=simple_value(fields.get("fixVersions") or []),
            affects_versions=simple_value(fields.get("versions") or []),
            environment=_text(fields.get("environment")),
            description=_text(fields.get("description")),
        )
    if "people" in sections:
        out.update(
            assignee=simple_value(fields.get("assignee")) or "Unassigned",
            reporter=simple_value(fields.get("reporter")),
            creator=simple_value(fields.get("creator")),
            watchers=(fields.get("watches") or {}).get("watchCount"),
        )
    if "dates" in sections:
        out.update(
            created=fields.get("created"),
            updated=fields.get("updated"),
            resolved=fields.get("resolutiondate"),
            due=fields.get("duedate"),
        )
    if "agile" in sections:
        out["agile"] = _agile(fields, catalog)
    if "attachments" in sections:
        out["attachments"] = [
            {
                "id": a.get("id"),
                "filename": a.get("filename"),
                "size_bytes": a.get("size"),
                "mime_type": a.get("mimeType"),
                "author": simple_value(a.get("author")),
                "created": a.get("created"),
                "url": a.get("content"),
            }
            for a in fields.get("attachment") or []
        ]
    if "links" in sections:
        out["links"] = [_link(link) for link in fields.get("issuelinks") or []]
    if "subtasks" in sections:
        out["subtasks"] = [compact_row(sub) for sub in fields.get("subtasks") or []]
        if fields.get("parent"):
            out["parent"] = {"key": fields["parent"].get("key"), "summary": (fields["parent"].get("fields") or {}).get("summary")}
    if "time" in sections:
        tracking = fields.get("timetracking") or {}
        out["time_tracking"] = {k: tracking.get(k) for k in ("originalEstimate", "remainingEstimate", "timeSpent") if tracking.get(k)}
    if "custom_fields" in sections:
        known = set(agile_fields(catalog))
        custom: Dict[str, Any] = {}
        for field_id, value in fields.items():
            field = catalog.by_id.get(field_id)
            if not field or not field.custom or field_id in known or value in (None, "", [], {}):
                continue
            readable = simple_value(value)
            if readable in (None, "", []):
                continue
            custom[field.name] = _text(readable, MAX_CUSTOM_VALUE_CHARS) if isinstance(readable, str) else readable
        out["custom_fields"] = custom
    if comments is not None:
        out["comments"] = [
            {"id": c.get("id"), "author": simple_value(c.get("author")), "created": c.get("created"), "body": _text(c.get("body"), 4000)}
            for c in comments
        ]
    if "history" in sections:
        out["history"] = [
            {
                "author": simple_value(h.get("author")),
                "created": h.get("created"),
                "changes": [
                    {"field": i.get("field"), "from": i.get("fromString"), "to": i.get("toString")} for i in h.get("items") or []
                ],
            }
            for h in ((issue.get("changelog") or {}).get("histories") or [])[-30:]
        ]
    return out
