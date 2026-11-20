"""The site's field catalog, so custom fields (Sprint, Story Points, Epic Link, ...) get their display names."""

import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from secure_jira_mcp.jira_client import JiraClient

CATALOG_TTL_SECONDS = 3600.0
SPRINT_SCHEMA = "com.pyxis.greenhopper.jira:gh-sprint"
EPIC_LINK_SCHEMA = "com.pyxis.greenhopper.jira:gh-epic-link"
STORY_POINT_NAMES = ("story points", "story point estimate")


@dataclass
class Field:
    id: str
    name: str
    custom: bool
    schema_type: str
    custom_type: str


class FieldCatalog:
    def __init__(self, fields: List[Field]) -> None:
        self.by_id: Dict[str, Field] = {f.id: f for f in fields}
        self._by_name: Dict[str, Field] = {}
        for f in fields:
            self._by_name.setdefault(f.name.lower(), f)

    def name(self, field_id: str) -> str:
        field = self.by_id.get(field_id)
        return field.name if field else field_id

    def find(self, name_or_id: str) -> Optional[Field]:
        return self.by_id.get(name_or_id) or self._by_name.get(name_or_id.strip().lower())

    def ids_with_custom_type(self, custom_type: str) -> List[str]:
        return [f.id for f in self.by_id.values() if f.custom_type == custom_type]

    def sprint_ids(self) -> List[str]:
        return self.ids_with_custom_type(SPRINT_SCHEMA)

    def epic_link_ids(self) -> List[str]:
        return self.ids_with_custom_type(EPIC_LINK_SCHEMA)

    def story_point_ids(self) -> List[str]:
        return [f.id for f in self.by_id.values() if f.custom and f.name.lower() in STORY_POINT_NAMES]


_cache: Dict[str, Tuple[float, FieldCatalog]] = {}


async def field_catalog(client: JiraClient) -> FieldCatalog:
    """Cached per site for an hour; field definitions are the same for every user of a site."""
    hit = _cache.get(client.base_url)
    if hit and time.monotonic() - hit[0] < CATALOG_TTL_SECONDS:
        return hit[1]
    raw = await client.get("/rest/api/2/field") or []
    fields = [
        Field(
            id=str(item.get("id")),
            name=str(item.get("name") or item.get("id")),
            custom=bool(item.get("custom")),
            schema_type=str((item.get("schema") or {}).get("type") or ""),
            custom_type=str((item.get("schema") or {}).get("custom") or ""),
        )
        for item in raw
        if isinstance(item, dict) and item.get("id")
    ]
    catalog = FieldCatalog(fields)
    _cache[client.base_url] = (time.monotonic(), catalog)
    return catalog


def clear_cache() -> None:
    _cache.clear()


def simple_value(value: Any) -> Any:
    """A readable form of a Jira field value: names instead of nested objects."""
    if isinstance(value, dict):
        for key in ("displayName", "name", "value", "key"):
            if value.get(key) not in (None, ""):
                return value[key]
        return None
    if isinstance(value, list):
        items = [simple_value(item) for item in value]
        return [item for item in items if item not in (None, "")]
    return value
