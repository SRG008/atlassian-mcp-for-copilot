"""Strict, bounded Confluence reads and approval-gated changes."""

import base64
import binascii
import hashlib
import html
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Callable, Dict, Optional, Type
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from secure_jira_mcp.config import Settings
from secure_jira_mcp.confluence_client import ConfluenceClient
from secure_jira_mcp.errors import JiraError, ValidationError

PAGE_ID = r"^[1-9][0-9]{0,19}$"
MAX_BODY_CHARS = 32_000
MAX_PAGE_TEXT_CHARS = 50_000
MAX_ENCODED_ATTACHMENT_CHARS = 27_000_000


class CArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchPagesArgs(CArgs):
    cql: str = Field(min_length=1, max_length=4000, description="Confluence Query Language (CQL).")
    max_results: int = Field(50, ge=1, le=200)
    start: int = Field(0, ge=0, le=100_000)


class ListSpacesArgs(CArgs):
    max_results: int = Field(50, ge=1, le=200)
    start: int = Field(0, ge=0, le=100_000)


class PageIdArgs(CArgs):
    page_id: str = Field(pattern=PAGE_ID, description="Numeric Confluence page ID.")


class ListCommentsArgs(PageIdArgs):
    max_results: int = Field(50, ge=1, le=200)
    start: int = Field(0, ge=0, le=100_000)


class ListAttachmentsArgs(PageIdArgs):
    max_results: int = Field(50, ge=1, le=200)
    start: int = Field(0, ge=0, le=100_000)


class AttachmentIdArgs(CArgs):
    attachment_id: str = Field(pattern=PAGE_ID, description="Numeric Confluence attachment ID.")


class CreatePageArgs(CArgs):
    space_key: str = Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_-]+$")
    title: str = Field(min_length=1, max_length=255)
    body: str = Field(max_length=MAX_BODY_CHARS, description="Plain text; HTML is escaped before writing.")
    parent_id: Optional[str] = Field(None, pattern=PAGE_ID)


class UpdatePageArgs(PageIdArgs):
    title: Optional[str] = Field(None, min_length=1, max_length=255)
    body: Optional[str] = Field(None, max_length=MAX_BODY_CHARS, description="Plain text; HTML is escaped before writing.")

    @model_validator(mode="after")
    def require_update(self) -> "UpdatePageArgs":
        if self.title is None and self.body is None:
            raise ValueError("Provide a title or body to update.")
        return self


class AddCommentArgs(PageIdArgs):
    body: str = Field(min_length=1, max_length=MAX_BODY_CHARS)


class UpdateCommentArgs(CArgs):
    comment_id: str = Field(pattern=PAGE_ID, description="Numeric Confluence comment ID.")
    body: str = Field(min_length=1, max_length=MAX_BODY_CHARS)


class UploadAttachmentArgs(PageIdArgs):
    filename: str = Field(min_length=1, max_length=255)
    content_base64: str = Field(min_length=1, max_length=MAX_ENCODED_ATTACHMENT_CHARS)
    content_type: str = Field(
        "application/octet-stream",
        min_length=3,
        max_length=127,
        pattern=r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$",
    )

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, value: str) -> str:
        if (
            value in (".", "..")
            or "/" in value
            or "\\" in value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            raise ValueError("Use a filename only, without path separators or control characters.")
        return value


@dataclass(frozen=True)
class ConfluenceChange:
    name: str
    group: str
    description: str
    args: Type[CArgs]
    target: Callable[[CArgs], str]


CONFLUENCE_CHANGES = {
    item.name: item
    for item in (
        ConfluenceChange("create_page", "confluence_write", "Create a page with plain-text content.", CreatePageArgs, lambda a: a.space_key),
        ConfluenceChange("update_page", "confluence_write", "Update a page title and/or plain-text body.", UpdatePageArgs, lambda a: a.page_id),
        ConfluenceChange("add_page_comment", "confluence_write", "Add a plain-text comment to a page.", AddCommentArgs, lambda a: a.page_id),
        ConfluenceChange("update_page_comment", "confluence_write", "Update an existing comment.", UpdateCommentArgs, lambda a: a.comment_id),
        ConfluenceChange("upload_attachment", "confluence_attachments", "Upload or replace an attachment using bounded base64 content.", UploadAttachmentArgs, lambda a: a.page_id),
    )
}


def parse_change(name: str, arguments: Dict[str, Any]) -> CArgs:
    operation = CONFLUENCE_CHANGES.get(name)
    if operation is None:
        raise ValidationError(f"Unknown Confluence change '{name}'.")
    try:
        return operation.args.model_validate(arguments or {})
    except Exception as e:
        errors = getattr(e, "errors", lambda: [])()
        details = "; ".join(
            f"{'.'.join(str(part) for part in error.get('loc', ()))}: {error.get('msg')}"
            for error in errors
        )
        raise ValidationError(f"Invalid arguments for {name}: {details or e}") from e


def storage_value(text: str) -> str:
    paragraphs = text.splitlines() or [""]
    return "".join(f"<p>{html.escape(line)}</p>" for line in paragraphs)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, Optional[str]]]) -> None:
        _ = _attrs
        if tag.lower() in ("p", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"):
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def plain_text(storage: str) -> str:
    parser = _TextExtractor()
    parser.feed(storage)
    return re.sub(r"\n{3,}", "\n\n", "".join(parser.parts)).strip()


def _page_path(page_id: str) -> str:
    return f"/rest/api/content/{page_id}"


def _mapping(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _results(response: Any, resource: str) -> list[Dict[str, Any]]:
    values = response.get("results") if isinstance(response, dict) else None
    if not isinstance(values, list) or any(not isinstance(item, dict) for item in values):
        raise JiraError(f"Confluence returned an invalid {resource} result list.")
    return values


async def search_pages(client: ConfluenceClient, settings: Settings, args: SearchPagesArgs) -> Dict[str, Any]:
    limit = min(args.max_results, settings.mcp_max_results, 200)
    page = await client.get(
        "/rest/api/content/search",
        {"cql": args.cql, "start": args.start, "limit": limit, "expand": "space,version"},
    ) or {}
    rows = []
    for item in _results(page, "page search"):
        space = _mapping(item.get("space"))
        rows.append({
            "id": str(item.get("id", "")),
            "title": item.get("title", ""),
            "type": item.get("type", ""),
            "status": item.get("status", ""),
            "space_key": space.get("key", ""),
            "version": _mapping(item.get("version")).get("number"),
            "url": _web_url(client, _mapping(item.get("_links")).get("webui")),
        })
    links = _mapping(page.get("_links"))
    has_more = bool(links.get("next"))
    return {
        "results": rows,
        "returned": len(rows),
        "start": args.start,
        "next_start": args.start + len(rows) if has_more else None,
        "limit": limit,
        "has_more": has_more,
    }


async def list_spaces(client: ConfluenceClient, settings: Settings, args: ListSpacesArgs) -> Dict[str, Any]:
    limit = min(args.max_results, settings.mcp_max_results, 200)
    page = await client.get("/rest/api/space", {"start": args.start, "limit": limit}) or {}
    results = [
        {
            "key": item.get("key", ""),
            "name": item.get("name", ""),
            "type": item.get("type", ""),
        }
        for item in _results(page, "space list")
    ]
    return {
        "spaces": results,
        "returned": len(results),
        "start": args.start,
        "next_start": args.start + len(results) if _mapping(page.get("_links")).get("next") else None,
        "limit": limit,
        "has_more": bool(_mapping(page.get("_links")).get("next")),
    }


async def get_page(client: ConfluenceClient, _settings: Settings, args: PageIdArgs) -> Dict[str, Any]:
    _ = _settings
    page = await client.get(
        _page_path(args.page_id),
        {"expand": "body.storage,space,version,ancestors"},
    )
    if not isinstance(page, dict) or page.get("type") != "page":
        raise JiraError("The requested Confluence content is not a page.")
    body = _mapping(_mapping(page.get("body")).get("storage")).get("value", "")
    text = plain_text(body) if isinstance(body, str) else ""
    space = _mapping(page.get("space"))
    version = _mapping(page.get("version"))
    result: Dict[str, Any] = {
        "id": str(page.get("id", "")),
        "title": page.get("title", ""),
        "status": page.get("status", ""),
        "space_key": space.get("key", ""),
        "version": version.get("number"),
        "body": text[:MAX_PAGE_TEXT_CHARS],
        "body_truncated": len(text) > MAX_PAGE_TEXT_CHARS,
        "url": _web_url(client, _mapping(page.get("_links")).get("webui")),
    }
    return result


async def list_comments(client: ConfluenceClient, settings: Settings, args: ListCommentsArgs) -> Dict[str, Any]:
    limit = min(args.max_results, settings.mcp_max_results, 200)
    response = await client.get(
        f"{_page_path(args.page_id)}/child/comment",
        {"start": args.start, "limit": limit, "expand": "body.storage,version"},
    ) or {}
    comments = []
    for item in _results(response, "comment list"):
        body = _mapping(_mapping(item.get("body")).get("storage")).get("value", "")
        comments.append({
            "id": str(item.get("id", "")),
            "title": item.get("title", ""),
            "body": plain_text(body) if isinstance(body, str) else "",
            "version": _mapping(item.get("version")).get("number"),
        })
    return {
        "comments": comments,
        "returned": len(comments),
        "start": args.start,
        "next_start": args.start + len(comments) if _mapping(response.get("_links")).get("next") else None,
        "limit": limit,
        "has_more": bool(_mapping(response.get("_links")).get("next")),
    }


async def list_attachments(client: ConfluenceClient, settings: Settings, args: ListAttachmentsArgs) -> Dict[str, Any]:
    limit = min(args.max_results, settings.mcp_max_results, 200)
    response = await client.get(
        f"{_page_path(args.page_id)}/child/attachment",
        {"start": args.start, "limit": limit, "expand": "version,container"},
    ) or {}
    attachments = []
    for item in _results(response, "attachment list"):
        attachments.append({
            "id": str(item.get("id", "")),
            "title": item.get("title", ""),
            "media_type": _mapping(item.get("metadata")).get("mediaType", ""),
            "file_size": _mapping(item.get("extensions")).get("fileSize"),
            "version": _mapping(item.get("version")).get("number"),
        })
    return {
        "attachments": attachments,
        "returned": len(attachments),
        "start": args.start,
        "next_start": args.start + len(attachments) if _mapping(response.get("_links")).get("next") else None,
        "limit": limit,
        "has_more": bool(_mapping(response.get("_links")).get("next")),
    }


async def download_attachment(
    client: ConfluenceClient,
    settings: Settings,
    args: AttachmentIdArgs,
) -> Dict[str, Any]:
    metadata = await client.get(
        _page_path(args.attachment_id),
        {"expand": "_links,container,metadata"},
    )
    if not isinstance(metadata, dict) or metadata.get("type") != "attachment":
        raise JiraError("The requested Confluence content is not an attachment.")
    download = _mapping(metadata.get("_links")).get("download")
    if not isinstance(download, str) or not download:
        raise JiraError("Confluence did not provide an attachment download link.")
    content, content_type = await client.download_attachment(download, settings.mcp_max_attachment_bytes)
    return {
        "id": str(metadata.get("id", "")),
        "filename": metadata.get("title", ""),
        "content_type": content_type.split(";", 1)[0].strip(),
        "size_bytes": len(content),
        "content_base64": base64.b64encode(content).decode("ascii"),
    }


async def preview_change(client: ConfluenceClient, name: str, args: CArgs, settings: Settings) -> str:
    if name == "create_page":
        assert isinstance(args, CreatePageArgs)
        parent = f"\nParent page ID: {args.parent_id}" if args.parent_id else ""
        return f"Create Confluence page\nSpace: {args.space_key}\nTitle: {args.title}{parent}\nBody:\n{args.body}"
    if name == "update_page":
        assert isinstance(args, UpdatePageArgs)
        current = await client.get(
            _page_path(args.page_id),
            {"expand": "body.storage,space,version"},
        )
        if not isinstance(current, dict) or current.get("type") != "page":
            raise JiraError("The requested Confluence content is not a page.")
        old_body = _mapping(_mapping(current.get("body")).get("storage")).get("value", "")
        old_text = plain_text(old_body) if isinstance(old_body, str) else ""
        version = _mapping(current.get("version")).get("number")
        title = args.title if args.title is not None else str(current.get("title", ""))
        body_preview = (
            f"Body unchanged ({len(old_text)} plain-text characters)"
            if args.body is None
            else f"Old body:\n{old_text}\nNew body:\n{args.body}"
        )
        return (
            f"Update Confluence page {args.page_id} (version {version})"
            f"\nTitle: {current.get('title', '')} -> {title}\n{body_preview}"
        )
    if name == "add_page_comment":
        assert isinstance(args, AddCommentArgs)
        return f"Add comment to Confluence page {args.page_id}:\n{args.body}"
    if name == "update_page_comment":
        assert isinstance(args, UpdateCommentArgs)
        current = await client.get(
            _page_path(args.comment_id),
            {"expand": "body.storage,container,version"},
        )
        if not isinstance(current, dict) or current.get("type") != "comment":
            raise JiraError("The requested Confluence content is not a comment.")
        old_body = _mapping(_mapping(current.get("body")).get("storage")).get("value", "")
        old_text = plain_text(old_body) if isinstance(old_body, str) else ""
        container = _mapping(current.get("container"))
        version = _mapping(current.get("version")).get("number")
        return (
            f"Update comment {args.comment_id} on page {container.get('id', '')}"
            f" (version {version})"
            f"\nOld comment:\n{old_text}\nNew comment:\n{args.body}"
        )
    if name == "upload_attachment":
        assert isinstance(args, UploadAttachmentArgs)
        content = _decode_attachment(args.content_base64, settings.mcp_max_attachment_bytes)
        current = await client.get(
            f"{_page_path(args.page_id)}/child/attachment",
            {"start": 0, "limit": 200, "expand": "version"},
        )
        existing = any(
            item.get("title") == args.filename
            for item in _results(current, "attachment list")
        )
        action = "Replace existing attachment" if existing else "Upload new attachment"
        return (
            f"{action} on Confluence page {args.page_id}\nFilename: {args.filename}"
            f"\nContent type: {args.content_type}\nSize: {len(content):,} bytes"
            f"\nSHA-256: {hashlib.sha256(content).hexdigest()}"
        )
    raise ValidationError(f"Unknown Confluence change '{name}'.")


async def run_change(client: ConfluenceClient, name: str, args: CArgs, settings: Settings) -> Dict[str, Any]:
    if name == "create_page":
        assert isinstance(args, CreatePageArgs)
        body: Dict[str, Any] = {
            "type": "page",
            "title": args.title,
            "space": {"key": args.space_key},
            "body": {"storage": {"value": storage_value(args.body), "representation": "storage"}},
        }
        if args.parent_id:
            body["ancestors"] = [{"id": args.parent_id}]
        result = await client.post("/rest/api/content", body)
        return _change_result(result)
    if name == "update_page":
        assert isinstance(args, UpdatePageArgs)
        current = await client.get(_page_path(args.page_id), {"expand": "body.storage,space,version"})
        if not isinstance(current, dict) or current.get("type") != "page":
            raise JiraError("The requested Confluence content is not a page.")
        version = _mapping(current.get("version")).get("number")
        if not isinstance(version, int) or version < 1:
            raise JiraError("Confluence did not return a valid page version.")
        space = _mapping(current.get("space"))
        if not space.get("key"):
            raise JiraError("Confluence did not return the page's space key.")
        body_storage = _mapping(_mapping(current.get("body")).get("storage")).get("value", "")
        current_text = plain_text(body_storage) if isinstance(body_storage, str) else ""
        body_value = (
            body_storage
            if args.body is None and isinstance(body_storage, str)
            else storage_value(args.body if args.body is not None else current_text)
        )
        update = {
            "id": args.page_id,
            "type": "page",
            "title": args.title if args.title is not None else current.get("title", ""),
            "space": {"key": space["key"]},
            "body": {"storage": {"value": body_value, "representation": "storage"}},
            "version": {"number": version + 1},
        }
        result = await client.put(_page_path(args.page_id), update)
        return _change_result(result)
    if name == "add_page_comment":
        assert isinstance(args, AddCommentArgs)
        result = await client.post("/rest/api/content", {
            "type": "comment",
            "container": {"id": args.page_id, "type": "page"},
            "body": {"storage": {"value": storage_value(args.body), "representation": "storage"}},
        })
        return _change_result(result)
    if name == "update_page_comment":
        assert isinstance(args, UpdateCommentArgs)
        current = await client.get(
            _page_path(args.comment_id),
            {"expand": "body.storage,container,version"},
        )
        if not isinstance(current, dict) or current.get("type") != "comment":
            raise JiraError("The requested Confluence content is not a comment.")
        version = _mapping(current.get("version")).get("number")
        container = _mapping(current.get("container"))
        if not isinstance(version, int) or version < 1 or not container.get("id"):
            raise JiraError("Confluence did not return a valid comment version and page.")
        result = await client.put(_page_path(args.comment_id), {
            "id": args.comment_id,
            "type": "comment",
            "title": current.get("title", ""),
            "container": {"id": str(container["id"]), "type": "page"},
            "body": {"storage": {"value": storage_value(args.body), "representation": "storage"}},
            "version": {"number": version + 1},
        })
        return _change_result(result)
    if name == "upload_attachment":
        assert isinstance(args, UploadAttachmentArgs)
        content = _decode_attachment(args.content_base64, settings.mcp_max_attachment_bytes)
        result = await client.upload_attachment(
            f"{_page_path(args.page_id)}/child/attachment",
            args.filename,
            content,
            args.content_type,
        )
        return _change_result(result)
    raise ValidationError(f"Unknown Confluence change '{name}'.")


def _decode_attachment(value: str, max_bytes: int) -> bytes:
    try:
        content = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as e:
        raise ValidationError("Attachment content must be valid base64.") from e
    if not content:
        raise ValidationError("Attachment content must not be empty.")
    if len(content) > max_bytes:
        raise ValidationError(f"The attachment is larger than the {max_bytes:,} byte limit.")
    return content


def _web_url(client: ConfluenceClient, webui: Any) -> str:
    if not isinstance(webui, str) or not webui:
        return ""
    base = client.base_url.rstrip("/")
    parsed = urlsplit(webui)
    if (
        parsed.scheme
        or parsed.netloc
        or not parsed.path.startswith("/")
        or parsed.path.startswith("//")
        or "\\" in unquote(parsed.path)
        or any(ord(char) < 32 or ord(char) == 127 for char in webui)
        or any(segment in (".", "..") for segment in unquote(parsed.path).split("/"))
    ):
        return ""
    return base + parsed.path + (f"?{parsed.query}" if parsed.query else "")


def _change_result(result: Any) -> Dict[str, Any]:
    if not isinstance(result, dict):
        return {"result": result}
    return {
        "id": str(result.get("id", "")),
        "title": result.get("title", ""),
        "type": result.get("type", ""),
        "status": result.get("status", ""),
        "version": _mapping(result.get("version")).get("number"),
    }
