"""Confluence Cloud and Data Center tests with a bounded fake REST API."""

import asyncio
import base64
import contextlib
import io
import json
import unittest
from typing import Any, Dict, List
from unittest.mock import patch

import httpx
from mcp import Client, types

from secure_jira_mcp import __main__ as cli
from secure_jira_mcp.config import Settings
from secure_jira_mcp.confluence_client import ConfluenceClient
from secure_jira_mcp.errors import ConfigError, ValidationError
from secure_jira_mcp.server import build_server, resolve_confluence_session

BASE = "https://confluence.example.com/confluence"


class FakeConfluence:
    def __init__(self) -> None:
        self.requests: List[httpx.Request] = []
        self.writes: List[Dict[str, Any]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("authorization") != "Bearer good-pat":
            return httpx.Response(302, headers={"location": "/login.action"})
        path, method = request.url.path, request.method
        if method != "GET":
            self.writes.append({"method": method, "path": path, "request": request})
        if path == "/confluence/rest/api/content/search":
            return httpx.Response(200, json={
                "results": [{
                    "id": "123", "title": "Welcome", "type": "page", "status": "current",
                    "space": {"key": "DOCS"}, "version": {"number": 2},
                    "_links": {"webui": "/display/DOCS/Welcome"},
                }],
                "_links": {"next": "/rest/api/content/search?start=1"},
            })
        if path == "/confluence/rest/api/space":
            return httpx.Response(200, json={"results": [{"key": "DOCS", "name": "Documentation", "type": "global"}]})
        if path == "/confluence/rest/api/content/123" and method == "GET":
            return httpx.Response(200, json={
                "id": "123", "type": "page", "title": "Welcome", "status": "current",
                "space": {"key": "DOCS"}, "version": {"number": 2},
                "body": {"storage": {"value": "<h1>Title</h1><p>Hello &amp; welcome</p>"}},
                "_links": {"webui": "/display/DOCS/Welcome"},
            })
        if path == "/confluence/rest/api/content/123" and method == "PUT":
            self.last_update = json.loads(request.content)
            return httpx.Response(200, json={
                "id": "123", "type": "page", "title": self.last_update["title"],
                "version": self.last_update["version"],
            })
        if path == "/confluence/rest/api/content" and method == "POST":
            body = json.loads(request.content)
            return httpx.Response(200, json={
                "id": "456", "type": body["type"], "title": body.get("title", ""),
                "version": {"number": 1},
            })
        if path == "/confluence/rest/api/content/123/child/comment":
            return httpx.Response(200, json={"results": [{
                "id": "77", "title": "comment", "version": {"number": 1},
                "body": {"storage": {"value": "<p>Existing comment</p>"}},
            }], "_links": {}})
        if path == "/confluence/rest/api/content/77" and method == "GET":
            return httpx.Response(200, json={
                "id": "77", "type": "comment", "title": "comment",
                "container": {"id": "123", "type": "page"},
                "version": {"number": 1},
                "body": {"storage": {"value": "<p>Existing comment</p>"}},
            })
        if path == "/confluence/rest/api/content/77" and method == "PUT":
            self.last_comment_update = json.loads(request.content)
            return httpx.Response(200, json={
                "id": "77", "type": "comment", "title": "comment",
                "version": self.last_comment_update["version"],
            })
        if path == "/confluence/rest/api/content/123/child/attachment" and method == "GET":
            return httpx.Response(200, json={"results": [], "_links": {}})
        if path == "/confluence/rest/api/content/123/child/attachment" and method == "POST":
            self.upload_request = request
            return httpx.Response(200, json={"results": [{"id": "88", "title": "note.txt"}]})
        if path == "/confluence/rest/api/content/55":
            return httpx.Response(200, json={
                "id": "55", "type": "attachment", "title": "note.txt",
                "_links": {"download": "/confluence/download/attachments/55/note.txt"},
            })
        if path == "/confluence/download/attachments/55/note.txt":
            return httpx.Response(200, headers={"content-type": "text/plain; charset=utf-8"}, content=b"hello")
        return httpx.Response(404, json={"message": f"not faked: {method} {path}"})


def settings(**overrides: Any) -> Settings:
    values = {
        "confluence_url": BASE,
        "confluence_personal_token": "good-pat",
        "mcp_audit_log": "",
        "mcp_read_only": False,
        "use_system_certificates": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def result_of(call: types.CallToolResult) -> Any:
    if call.is_error:
        raise AssertionError(call.content[0].text if call.content else "tool error")
    content = call.structured_content or {}
    return content.get("result", content)


class ConfluenceClientTests(unittest.TestCase):
    def test_scoped_cloud_token_uses_confluence_gateway_without_auth_on_site_id_lookup(self):
        requests = []
        cloud_id = "207208ad-71f6-42c9-a44e-84bd9de1fe13"

        def respond(request):
            requests.append(request)
            if request.url.path == "/_edge/tenant_info":
                return httpx.Response(200, json={"cloudId": cloud_id})
            if request.url.path == f"/ex/confluence/{cloud_id}/wiki/rest/api/user/current":
                return httpx.Response(200, json={"accountId": "cloud-user"})
            return httpx.Response(404)

        async def run():
            async with ConfluenceClient(
                "https://tenant.atlassian.net/wiki",
                "scoped-token",
                deployment="cloud",
                username="user@example.com",
                api_token_scoped=True,
                transport=httpx.MockTransport(respond),
            ) as client:
                return await client.get("/rest/api/user/current")

        self.assertEqual(asyncio.run(run()), {"accountId": "cloud-user"})
        self.assertEqual([str(request.url) for request in requests], [
            "https://tenant.atlassian.net/_edge/tenant_info",
            f"https://api.atlassian.com/ex/confluence/{cloud_id}/wiki/rest/api/user/current",
        ])
        self.assertNotIn("authorization", requests[0].headers)
        self.assertEqual(
            requests[1].headers["authorization"],
            "Basic " + base64.b64encode(b"user@example.com:scoped-token").decode(),
        )

    def test_scoped_cloud_attachment_uses_fixed_confluence_gateway(self):
        requests = []
        cloud_id = "207208ad-71f6-42c9-a44e-84bd9de1fe13"

        def respond(request):
            requests.append(request)
            if request.url.path == "/_edge/tenant_info":
                return httpx.Response(200, json={"cloudId": cloud_id})
            if request.url.path == f"/ex/confluence/{cloud_id}/wiki/download/attachments/55/note.txt":
                return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"scoped attachment")
            return httpx.Response(404)

        async def run():
            async with ConfluenceClient(
                "https://tenant.atlassian.net/wiki",
                "scoped-token",
                deployment="cloud",
                username="user@example.com",
                api_token_scoped=True,
                transport=httpx.MockTransport(respond),
            ) as client:
                return await client.download_attachment(
                    "https://tenant.atlassian.net/wiki/download/attachments/55/note.txt",
                    1024,
                )

        self.assertEqual(asyncio.run(run()), (b"scoped attachment", "text/plain"))
        self.assertEqual(
            requests[1].url.path,
            f"/ex/confluence/{cloud_id}/wiki/download/attachments/55/note.txt",
        )

    def test_cloud_basic_auth_and_wiki_context_path(self):
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={"accountId": "cloud-user"})

        async def run():
            async with ConfluenceClient(
                "https://tenant.atlassian.net/wiki",
                "api-token",
                deployment="cloud",
                username="user@example.com",
                transport=httpx.MockTransport(respond),
            ) as client:
                await client.get("/rest/api/user/current")

        asyncio.run(run())
        self.assertEqual(requests[0].url.path, "/wiki/rest/api/user/current")
        self.assertEqual(
            requests[0].headers["authorization"],
            "Basic " + base64.b64encode(b"user@example.com:api-token").decode(),
        )

    def test_cli_connection_check_verifies_confluence_credentials(self):
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={"username": "conf-user"})

        def client_factory(*args, **kwargs):
            return ConfluenceClient(*args, **kwargs, transport=httpx.MockTransport(respond))

        with patch.dict("os.environ", {
            "CONFLUENCE_URL": BASE,
            "CONFLUENCE_DEPLOYMENT": "data_center",
            "CONFLUENCE_PERSONAL_TOKEN": "good-pat",
            "USE_SYSTEM_CERTIFICATES": "false",
        }), patch.object(cli, "ConfluenceClient", side_effect=client_factory), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["--no-env-file", "--check-confluence-connection"]), 0)
        self.assertEqual(requests[0].url.path, "/confluence/rest/api/user/current")
        self.assertEqual(requests[0].headers["authorization"], "Bearer good-pat")

    def test_api_paths_and_downloads_are_same_site_and_bounded(self):
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(200, content=b"x")

        async def run():
            async with ConfluenceClient(
                BASE, "good-pat", transport=httpx.MockTransport(respond)
            ) as client:
                with self.assertRaises(ValidationError):
                    await client.get("https://attacker.example/rest/api/content")
                with self.assertRaises(ValidationError):
                    await client.download_attachment("https://attacker.example/download/attachments/1/a.txt", 100)
                with self.assertRaises(ValidationError):
                    await client.download_attachment("/confluence/download/attachments/1/a.txt", 0)

        asyncio.run(run())
        self.assertEqual(requests[0].url.path, "/confluence/download/attachments/1/a.txt")

    def test_site_normalization_and_host_allowlist(self):
        configured = settings(
            confluence_url="https://tenant.atlassian.net/wiki/spaces/DOCS/pages/123/Welcome",
            confluence_deployment="cloud",
            confluence_username="user@example.com",
            confluence_api_token="api-token",
            confluence_api_token_scoped=True,
        )
        current = resolve_confluence_session(None, configured, configured.confluence_allowed_hosts_list())
        self.assertEqual(current.base_url, "https://tenant.atlassian.net/wiki")
        self.assertTrue(current.api_token_scoped)
        with self.assertRaises(ConfigError):
            resolve_confluence_session(
                {
                    "x-confluence-token": "token",
                    "x-confluence-username": "user@example.com",
                    "x-confluence-url": "https://attacker.example",
                },
                configured,
                configured.confluence_allowed_hosts_list(),
            )


class ConfluenceServerTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeConfluence()
        self.asked: List[str] = []

    def factory(
        self,
        base_url: str,
        token: str,
        timeout: float = 30.0,
        deployment: str = "data_center",
        username: str = "",
        **kwargs: Any,
    ) -> ConfluenceClient:
        return ConfluenceClient(
            base_url, token, timeout=timeout, deployment=deployment, username=username,
            transport=httpx.MockTransport(self.api.handle), **kwargs,
        )

    def call(self, name: str, arguments: Dict[str, Any], answer: str = "accept", **overrides: Any):
        async def elicit(_context, params):
            self.asked.append(params.message)
            if answer == "accept":
                return types.ElicitResult(action="accept", content={"approve": True})
            return types.ElicitResult(action="decline")

        async def run():
            with patch("secure_jira_mcp.server.ConfluenceClient", side_effect=self.factory):
                async with Client(
                    build_server(settings(**overrides)),
                    elicitation_callback=elicit if answer else None,
                ) as client:
                    return await client.call_tool(name, arguments)

        return asyncio.run(run())

    def test_read_tools_search_spaces_and_page_in_context_path(self):
        async def run():
            with patch("secure_jira_mcp.server.ConfluenceClient", side_effect=self.factory):
                async with Client(build_server(settings())) as client:
                    search = await client.call_tool("search_confluence_pages", {"cql": "space = DOCS"})
                    spaces = await client.call_tool("list_confluence_spaces", {})
                    page = await client.call_tool("get_confluence_page", {"page_id": "123"})
                    return search, spaces, page

        search, spaces, page = asyncio.run(run())
        self.assertEqual(result_of(search)["results"][0]["id"], "123")
        self.assertEqual(result_of(search)["next_start"], 1)
        self.assertEqual(result_of(spaces)["spaces"][0]["key"], "DOCS")
        self.assertEqual(result_of(page)["body"], "Title\nHello & welcome")
        self.assertEqual(result_of(page)["url"], BASE + "/display/DOCS/Welcome")
        self.assertTrue(all(request.url.path.startswith("/confluence/rest/api/") for request in self.api.requests))

    def test_approval_gates_page_create_and_update_and_escapes_plain_text(self):
        created = self.call("run_confluence_change", {
            "operation": "create_page",
            "arguments": {"space_key": "DOCS", "title": "New page", "body": "<script>alert(1)</script>"},
        })
        self.assertEqual(result_of(created)["status"], "done")
        create_body = json.loads(self.api.writes[-1]["request"].content)
        self.assertEqual(
            create_body["body"]["storage"]["value"],
            "<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>",
        )
        self.assertIn("<script>alert(1)</script>", self.asked[-1])

        updated = self.call("run_confluence_change", {
            "operation": "update_page",
            "arguments": {"page_id": "123", "body": "Updated & safe"},
        })
        self.assertEqual(result_of(updated)["status"], "done")
        self.assertEqual(self.api.last_update["version"]["number"], 3)
        self.assertEqual(self.api.last_update["body"]["storage"]["value"], "<p>Updated &amp; safe</p>")
        self.call("run_confluence_change", {
            "operation": "update_page",
            "arguments": {"page_id": "123", "title": "Renamed"},
        })
        self.assertEqual(
            self.api.last_update["body"]["storage"]["value"],
            "<h1>Title</h1><p>Hello &amp; welcome</p>",
        )

    def test_declined_and_read_only_changes_do_not_write(self):
        declined = self.call(
            "run_confluence_change",
            {"operation": "add_page_comment", "arguments": {"page_id": "123", "body": "No"}},
            answer="decline",
        )
        self.assertEqual(result_of(declined)["status"], "declined")
        self.assertEqual(self.api.writes, [])

        read_only = self.call(
            "run_confluence_change",
            {"operation": "create_page", "arguments": {"space_key": "DOCS", "title": "No", "body": "No"}},
            mcp_read_only=True,
        )
        self.assertTrue(read_only.is_error)
        self.assertIn("read-only", read_only.content[0].text)
        self.assertEqual(self.api.writes, [])

    def test_comments_can_be_listed_added_and_updated(self):
        async def list_comments():
            with patch("secure_jira_mcp.server.ConfluenceClient", side_effect=self.factory):
                async with Client(build_server(settings())) as client:
                    return await client.call_tool("list_page_comments", {"page_id": "123"})

        listed = result_of(asyncio.run(list_comments()))
        self.assertEqual(listed["comments"][0]["body"], "Existing comment")

        added = self.call("run_confluence_change", {
            "operation": "add_page_comment",
            "arguments": {"page_id": "123", "body": "New <comment>"},
        })
        self.assertEqual(result_of(added)["status"], "done")
        added_body = json.loads(self.api.writes[-1]["request"].content)
        self.assertEqual(added_body["type"], "comment")
        self.assertEqual(added_body["body"]["storage"]["value"], "<p>New &lt;comment&gt;</p>")

        updated = self.call("run_confluence_change", {
            "operation": "update_page_comment",
            "arguments": {"comment_id": "77", "body": "Edited comment"},
        })
        self.assertEqual(result_of(updated)["status"], "done")
        self.assertEqual(self.api.last_comment_update["version"]["number"], 2)
        self.assertIn("Old comment:\nExisting comment", self.asked[-1])

    def test_attachment_upload_download_and_limits(self):
        uploaded = self.call("run_confluence_change", {
            "operation": "upload_attachment",
            "arguments": {
                "page_id": "123",
                "filename": "note.txt",
                "content_base64": base64.b64encode(b"hello").decode(),
                "content_type": "text/plain",
            },
        })
        self.assertEqual(result_of(uploaded)["status"], "done")
        self.assertEqual(self.api.upload_request.headers["x-atlassian-token"], "no-check")
        self.assertIn(b"hello", self.api.upload_request.content)
        self.assertIn("SHA-256:", self.asked[-1])

        async def run_download():
            with patch("secure_jira_mcp.server.ConfluenceClient", side_effect=self.factory):
                async with Client(build_server(settings())) as client:
                    return await client.call_tool("download_confluence_attachment", {"attachment_id": "55"})

        downloaded = asyncio.run(run_download())
        result = result_of(downloaded)
        self.assertEqual(base64.b64decode(result["content_base64"]), b"hello")
        self.assertEqual(result["size_bytes"], 5)

        too_large = self.call(
            "run_confluence_change",
            {
                "operation": "upload_attachment",
                "arguments": {
                    "page_id": "123", "filename": "bad.txt",
                    "content_base64": base64.b64encode(b"x" * 1001).decode(),
                },
            },
            mcp_max_attachment_bytes=1000,
        )
        self.assertTrue(too_large.is_error)
        self.assertIn("limit", too_large.content[0].text)

    def test_unsafe_attachment_filename_is_rejected(self):
        result = self.call("run_confluence_change", {
            "operation": "upload_attachment",
            "arguments": {
                "page_id": "123", "filename": "..\\secret.txt",
                "content_base64": base64.b64encode(b"hello").decode(),
            },
        })
        self.assertTrue(result.is_error)
        self.assertEqual(self.api.writes, [])
