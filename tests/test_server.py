"""Tests against a fake Jira Data Center, through a real in-process MCP client."""

import asyncio
import base64
import contextlib
import ipaddress
import io
import json
import socket
import ssl
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import httpcore
import httpx
from mcp import Client, types
from mcp.server.transport_security import TransportSecuritySettings

from secure_jira_mcp import fields as fields_module
from secure_jira_mcp import formatting as formatting_module
from secure_jira_mcp import __main__ as cli
from secure_jira_mcp import jira_client as jira_client_module
from secure_jira_mcp.config import Settings, normalize_base_url
from secure_jira_mcp.errors import AuthError, ConfigError, JiraError, ValidationError
from secure_jira_mcp.jira_client import JiraClient
from secure_jira_mcp.network import PinnedAsyncHTTPTransport, PinnedNetworkBackend
from secure_jira_mcp.network import connection_error_hint
from secure_jira_mcp.policy import AuditLog, RateLimiter
from secure_jira_mcp.server import build_server, resolve_session

BASE = "https://jira.example.com"
SPRINT = "customfield_10010"
POINTS = "customfield_10002"
EPIC = "customfield_10008"
TEAM = "customfield_10500"


class ConnectionCheckTests(unittest.TestCase):
    def test_connection_error_explains_private_network_allowlist_without_exposing_credentials(self):
        error = httpx.ConnectError(
            "Jira resolved to a non-global IP outside JIRA_ALLOWED_NETWORKS."
        )
        message = connection_error_hint(error, "Jira", "JIRA_ALLOWED_NETWORKS")
        self.assertIn("exact hostname", message)
        self.assertIn("VPN", message)
        self.assertNotIn("good-pat", message)

    def test_connection_check_prints_the_safe_network_diagnostic(self):
        def respond(request):
            raise httpx.ConnectError(
                "Jira resolved to a non-global IP outside JIRA_ALLOWED_NETWORKS.",
                request=request,
            )

        with patch.dict("os.environ", {
            "JIRA_URL": BASE,
            "JIRA_PERSONAL_TOKEN": "good-pat",
            "USE_SYSTEM_CERTIFICATES": "false",
        }), patch.object(cli, "JiraClient", side_effect=lambda *args, **kwargs: JiraClient(
            *args, **kwargs, transport=httpx.MockTransport(respond)
        )), contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(cli.main(["--no-env-file", "--check-connection"]), 2)

        self.assertIn("exact hostname", stderr.getvalue())
        self.assertNotIn("good-pat", stderr.getvalue())

    def test_connection_check_accepts_jira_user_and_rejects_bad_token_without_following_redirect(self):
        requests = []

        def respond(request):
            requests.append(request)
            if request.headers["Authorization"] == "Bearer good-pat":
                return httpx.Response(200, json={"name": "jira-user"})
            return httpx.Response(302, headers={"Location": "https://login.example.com/"})

        for token, expected in (("good-pat", 0), ("bad-pat", 3)):
            with self.subTest(token=token), patch.dict("os.environ", {
                "JIRA_URL": f"{BASE}/jira",
                "JIRA_ALLOWED_HOSTS": "jira.example.com",
                "JIRA_PERSONAL_TOKEN": token,
                "USE_SYSTEM_CERTIFICATES": "false",
            }), patch.object(cli, "JiraClient", side_effect=lambda *args, **kwargs: JiraClient(
                *args, **kwargs, transport=httpx.MockTransport(respond)
            )), contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(cli.main(["--no-env-file", "--check-connection"]), expected)
                self.assertNotIn(token, stderr.getvalue())

        self.assertEqual([(r.method, str(r.url)) for r in requests], [
            ("GET", f"{BASE}/jira/rest/api/2/myself"),
            ("GET", f"{BASE}/jira/rest/api/2/myself"),
        ])

    def test_cloud_connection_check_uses_api_token_and_v3_myself(self):
        requests = []
        expected_auth = "Basic " + base64.b64encode(b"user@example.com:api-token").decode()

        def respond(request):
            requests.append(request)
            if request.headers.get("authorization") != expected_auth:
                return httpx.Response(401)
            return httpx.Response(200, json={"accountId": "cloud-user"})

        with patch.dict("os.environ", {
            "JIRA_URL": BASE,
            "JIRA_ALLOWED_HOSTS": "jira.example.com",
            "JIRA_DEPLOYMENT": "cloud",
            "JIRA_USERNAME": "user@example.com",
            "JIRA_API_TOKEN": "api-token",
            "USE_SYSTEM_CERTIFICATES": "false",
        }), patch.object(cli, "JiraClient", side_effect=lambda *args, **kwargs: JiraClient(
            *args, **kwargs, transport=httpx.MockTransport(respond)
        )), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["--no-env-file", "--check-connection"]), 0)

        self.assertEqual([(request.method, request.url.path) for request in requests], [
            ("GET", "/rest/api/3/myself"),
        ])


class JiraCloudClientTests(unittest.TestCase):
    def test_scoped_cloud_token_uses_tenant_id_gateway_without_auth_on_discovery(self):
        requests = []
        expected_auth = "Basic " + base64.b64encode(b"user@example.com:scoped-token").decode()
        cloud_id = "207208ad-71f6-42c9-a44e-84bd9de1fe13"

        def respond(request):
            requests.append(request)
            if request.url.path == "/_edge/tenant_info":
                return httpx.Response(200, json={"cloudId": cloud_id})
            if request.url.path == f"/ex/jira/{cloud_id}/rest/api/3/myself":
                return httpx.Response(200, json={"accountId": "cloud-user"})
            return httpx.Response(404)

        async def run():
            async with JiraClient(
                BASE, "scoped-token", deployment="cloud", username="user@example.com",
                api_token_scoped=True, transport=httpx.MockTransport(respond),
            ) as client:
                return await client.get("/rest/api/3/myself")

        self.assertEqual(asyncio.run(run()), {"accountId": "cloud-user"})
        self.assertEqual([str(request.url) for request in requests], [
            f"{BASE}/_edge/tenant_info",
            f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/myself",
        ])
        self.assertNotIn("authorization", requests[0].headers)
        self.assertEqual(requests[1].headers["authorization"], expected_auth)

    def test_scoped_cloud_attachment_uses_jira_content_api(self):
        requests = []
        cloud_id = "207208ad-71f6-42c9-a44e-84bd9de1fe13"

        def respond(request):
            requests.append(request)
            if request.url.path == "/_edge/tenant_info":
                return httpx.Response(200, json={"cloudId": cloud_id})
            if request.url.path == f"/ex/jira/{cloud_id}/rest/api/3/attachment/content/501":
                return httpx.Response(200, content=b"attachment text", headers={"content-type": "text/plain"})
            return httpx.Response(404)

        async def run():
            async with JiraClient(
                BASE, "scoped-token", deployment="cloud", username="user@example.com",
                api_token_scoped=True, transport=httpx.MockTransport(respond),
            ) as client:
                return await client.download(
                    f"{BASE}/secure/attachment/501/example.txt",
                    1024,
                    lambda content_type: content_type.startswith("text/"),
                )

        self.assertEqual(asyncio.run(run()), b"attachment text")
        self.assertEqual(requests[1].url.host, "api.atlassian.com")
        self.assertEqual(
            requests[1].url.path,
            f"/ex/jira/{cloud_id}/rest/api/3/attachment/content/501",
        )

    def test_cloud_auth_error_identifies_email_token_pair_without_exposing_token(self):
        async def run():
            async with JiraClient(
                BASE, "secret-token", deployment="cloud", username="user@example.com",
                transport=httpx.MockTransport(lambda _request: httpx.Response(401)),
            ) as client:
                await client.get("/rest/api/3/myself")

        with self.assertRaises(AuthError) as raised:
            asyncio.run(run())
        self.assertIn("account email and API token belong together", str(raised.exception))
        self.assertNotIn("secret-token", str(raised.exception))

    def test_jira_requests_identity_encoding_to_avoid_broken_proxy_compression(self):
        requests = []

        def respond(request):
            requests.append(request)
            if request.headers.get("accept-encoding") != "identity":
                return httpx.Response(
                    200,
                    headers={"Content-Encoding": "gzip"},
                    content=b"invalid compressed response",
                )
            return httpx.Response(200, json={"name": "jira-user"})

        async def run():
            async with JiraClient(
                BASE, "api-token", transport=httpx.MockTransport(respond)
            ) as client:
                return await client.get("/rest/api/2/myself")

        self.assertEqual(asyncio.run(run()), {"name": "jira-user"})
        self.assertEqual(requests[0].headers["accept-encoding"], "identity")

    def test_cloud_adf_descriptions_and_comments_are_returned_as_plain_text(self):
        document = {
            "type": "doc", "version": 1, "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "First line"}]},
                {"type": "paragraph", "content": [{"type": "text", "text": "Second line"}]},
            ],
        }
        details = formatting_module.issue_details(
            {"key": "CLOUD-1", "fields": {"description": document, "environment": document}},
            fields_module.FieldCatalog([]),
            {"details", "comments"},
            BASE,
            [{"body": document}],
        )

        self.assertEqual(details["description"], "First line\nSecond line")
        self.assertEqual(details["environment"], "First line\nSecond line")
        self.assertEqual(details["comments"][0]["body"], "First line\nSecond line")

    def test_cloud_uses_basic_auth_v3_paths_and_token_pagination(self):
        requests = []

        def respond(request):
            requests.append(request)
            if request.url.path == "/rest/api/3/myself":
                return httpx.Response(200, json={"accountId": "cloud-user"})
            if request.url.path == "/rest/api/3/search/jql":
                body = json.loads(request.content)
                if "nextPageToken" not in body:
                    return httpx.Response(200, json={
                        "issues": [{"key": "CLOUD-1"}, {"key": "CLOUD-2"}],
                        "nextPageToken": "continuation",
                    })
                return httpx.Response(200, json={
                    "issues": [{"key": "CLOUD-3"}],
                    "isLast": True,
                })
            return httpx.Response(404)

        async def run():
            async with JiraClient(
                BASE, "api-token", deployment="cloud", username="user@example.com",
                transport=httpx.MockTransport(respond),
            ) as client:
                await client.get("/rest/api/2/myself")
                first = await client.search_page("project = CLOUD", 0, 2, ["key"])
                second = await client.search_page("project = CLOUD", 2, 2, ["key"])
                return first, second

        first, second = asyncio.run(run())
        expected_auth = "Basic " + base64.b64encode(b"user@example.com:api-token").decode()
        self.assertEqual([request.url.path for request in requests], [
            "/rest/api/3/myself", "/rest/api/3/search/jql", "/rest/api/3/search/jql",
        ])
        self.assertTrue(all(request.headers["authorization"] == expected_auth for request in requests))
        self.assertEqual(json.loads(requests[2].content)["nextPageToken"], "continuation")
        self.assertEqual([issue["key"] for issue in first["issues"] + second["issues"]], [
            "CLOUD-1", "CLOUD-2", "CLOUD-3",
        ])

    def test_cloud_write_text_fields_and_comments_use_adf(self):
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(204)

        async def run():
            async with JiraClient(
                BASE, "api-token", deployment="cloud", username="user@example.com",
                transport=httpx.MockTransport(respond),
            ) as client:
                await client.put("/rest/api/2/issue/CLOUD-1", {
                    "fields": {"description": "First line\nSecond line", "environment": "Staging"},
                })
                await client.post("/rest/api/2/issue/CLOUD-1/comment", {"body": "A comment"})
                await client.post("/rest/api/2/issue/CLOUD-1/transitions", {
                    "transition": {"id": "31"},
                    "update": {"comment": [{"add": {"body": "Transition comment"}}]},
                })

        asyncio.run(run())
        self.assertEqual(json.loads(requests[0].content)["fields"], {
            "description": {
                "type": "doc", "version": 1,
                "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": "First line"}]},
                    {"type": "paragraph", "content": [{"type": "text", "text": "Second line"}]},
                ],
            },
            "environment": {
                "type": "doc", "version": 1,
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Staging"}]}],
            },
        })
        self.assertEqual(json.loads(requests[1].content)["body"]["type"], "doc")
        transition_comment = json.loads(requests[2].content)["update"]["comment"][0]["add"]["body"]
        self.assertEqual(transition_comment["content"][0]["content"][0]["text"], "Transition comment")

    def test_jira_response_size_is_bounded_before_json_parsing(self):
        with patch.object(jira_client_module, "MAX_RESPONSE_BYTES", 8):
            async def run():
                async with JiraClient(
                    BASE,
                    "api-token",
                    transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=b"0123456789")),
                ) as client:
                    await client.get("/rest/api/2/myself")

            with self.assertRaisesRegex(JiraError, "safety limit"):
                asyncio.run(run())

    def test_cloud_http_session_resolves_email_and_api_token_from_basic_auth(self):
        credentials = base64.b64encode(b"user@example.com:api-token").decode()
        current = resolve_session(
            {"authorization": f"Basic {credentials}", "x-jira-url": BASE},
            settings(jira_deployment="cloud", jira_api_token_scoped=True),
            ["jira.example.com"],
        )

        self.assertEqual((current.username, current.token, current.deployment), (
            "user@example.com", "api-token", "cloud",
        ))
        self.assertTrue(current.api_token_scoped)

    def test_cloud_search_reports_capping_and_counts_token_pages(self):
        requests = []

        def respond(request):
            requests.append(request)
            if request.url.path == "/rest/api/3/field":
                return httpx.Response(200, json=[
                    {"id": "summary", "name": "Summary", "custom": False, "schema": {"type": "string"}},
                    {"id": "issuetype", "name": "Issue Type", "custom": False, "schema": {"type": "issuetype"}},
                    {"id": "status", "name": "Status", "custom": False, "schema": {"type": "status"}},
                    {"id": "priority", "name": "Priority", "custom": False, "schema": {"type": "priority"}},
                    {"id": "assignee", "name": "Assignee", "custom": False, "schema": {"type": "user"}},
                    {"id": "updated", "name": "Updated", "custom": False, "schema": {"type": "datetime"}},
                ])
            if request.url.path == "/rest/api/3/search/jql":
                body = json.loads(request.content)
                if body.get("nextPageToken"):
                    return httpx.Response(200, json={
                        "issues": [{"key": "CLOUD-2", "fields": {"summary": "Second"}}],
                        "isLast": True,
                    })
                return httpx.Response(200, json={
                    "issues": [{"key": "CLOUD-1", "fields": {"summary": "First"}}],
                    "nextPageToken": "continuation",
                    "isLast": False,
                })
            return httpx.Response(404)

        def factory(base_url, token, deployment, username):
            return JiraClient(
                base_url, token, deployment=deployment, username=username,
                transport=httpx.MockTransport(respond),
            )

        server = build_server(settings(
            jira_deployment="cloud",
            jira_username="user@example.com",
            jira_api_token="api-token",
            mcp_max_results=1,
        ), factory)

        async def run():
            async with Client(server) as client:
                capped = await client.call_tool("search_issues", {"jql": "project = CLOUD"})
                counted = await client.call_tool("search_issues", {"jql": "project = CLOUD", "count_only": True})
                return capped, counted

        capped, counted = asyncio.run(run())
        self.assertFalse(capped.is_error, capped.content[0].text if capped.content else "")
        self.assertFalse(counted.is_error, counted.content[0].text if counted.content else "")
        self.assertEqual(result_of(capped)["complete"], False)
        self.assertEqual(result_of(capped)["total"], None)
        self.assertEqual(result_of(counted), {"total": 2})
        search_bodies = [
            json.loads(request.content) for request in requests
            if request.url.path == "/rest/api/3/search/jql"
        ]
        self.assertTrue(any(body.get("nextPageToken") == "continuation" for body in search_bodies))


class FakeJira:
    """Enough of the Jira Data Center REST API for the server's operations. Records every request."""

    def __init__(self, issue_count: int = 150) -> None:
        self.requests: List[httpx.Request] = []
        self.writes: List[Dict[str, Any]] = []
        self.issue_count = issue_count
        self.token = "good-pat"

    def _issue(self, n: int) -> Dict[str, Any]:
        return {
            "key": f"GHP-{n}",
            "fields": {
                "summary": f"Story {n}",
                "issuetype": {"name": "Story"},
                "status": {"name": "Done"},
                "priority": {"name": "Medium"},
                "assignee": {"displayName": "Sharath Kumar R", "name": "sharath"},
                "updated": "2026-10-02T10:00:00.000+0000",
                SPRINT: [f"com.atlassian.greenhopper.service.sprint.Sprint@1a[id=7,rapidViewId=3,state=CLOSED,name=PI-3 Sprint 2,goal=]"],
            },
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(302, headers={"location": "/login.jsp"})
        path, method = request.url.path, request.method
        if method != "GET":
            self.writes.append({"method": method, "path": path, "body": json.loads(request.content or b"{}")})
        if path == "/rest/api/2/field":
            return httpx.Response(200, json=[
                {"id": "project", "name": "Project", "custom": False, "schema": {"type": "project"}},
                {"id": "issuetype", "name": "Issue Type", "custom": False, "schema": {"type": "issuetype"}},
                {"id": "summary", "name": "Summary", "custom": False, "schema": {"type": "string"}},
                {"id": "description", "name": "Description", "custom": False, "schema": {"type": "string"}},
                {"id": "labels", "name": "Labels", "custom": False, "schema": {"type": "array"}},
                {"id": SPRINT, "name": "Sprint", "custom": True, "schema": {"type": "array", "custom": "com.pyxis.greenhopper.jira:gh-sprint"}},
                {"id": POINTS, "name": "Story Points", "custom": True, "schema": {"type": "number"}},
                {"id": EPIC, "name": "Epic Link", "custom": True, "schema": {"type": "any", "custom": "com.pyxis.greenhopper.jira:gh-epic-link"}},
                {"id": TEAM, "name": "Team", "custom": True, "schema": {"type": "option"}},
            ])
        if path == "/rest/api/2/search":
            body = json.loads(request.content)
            start, size = body["startAt"], body["maxResults"]
            batch = [self._issue(n) for n in range(start, min(start + size, self.issue_count))] if size else []
            return httpx.Response(200, json={"total": self.issue_count, "startAt": start, "issues": batch})
        if path == "/rest/api/2/issue/GHP-3213" and method == "GET":
            return httpx.Response(200, json={"key": "GHP-3213", "fields": {
                "summary": "User Local Access (Edge)",
                "issuetype": {"name": "Story"},
                "status": {"name": "Done"},
                "project": {"key": "GHP", "name": "Global HEMS Platform"},
                "assignee": {"displayName": "Sharath Kumar R"},
                "reporter": {"displayName": "B Suraj"},
                "labels": ["PI-3", "UserManagement"],
                "description": "Enable secure local user access.",
                "attachment": [{"id": "501", "filename": "gateway.log", "size": 2048, "mimeType": "text/plain",
                                "author": {"displayName": "B Suraj"}, "created": "2026-04-16", "content": f"{BASE}/secure/attachment/501/gateway.log"},
                               {"id": "502", "filename": "diagram.png", "size": 90000, "mimeType": "image/png",
                                "author": {"displayName": "B Suraj"}, "created": "2026-04-16", "content": f"{BASE}/secure/attachment/502/diagram.png"}],
                SPRINT: [{"id": 7, "name": "PI-3 Sprint 2", "state": "closed"}],
                POINTS: 5,
                EPIC: "GHP-3000",
                "issuelinks": [{"type": {"inward": "is blocked by", "outward": "blocks"},
                                "inwardIssue": {"key": "GHP-3100", "fields": {"summary": "Gateway auth", "status": {"name": "Done"}}}}],
                "subtasks": [{"key": "GHP-3214", "fields": {"summary": "Role model", "status": {"name": "Done"}, "issuetype": {"name": "Sub-task"}}}],
            }})
        if path == "/rest/api/2/issue/GHP-3213/comment":
            total = 12
            if request.url.params.get("maxResults") == "0":
                return httpx.Response(200, json={"total": total, "comments": []})
            start = int(request.url.params.get("startAt", 0))
            return httpx.Response(200, json={"total": total, "comments": [
                {"id": str(i), "author": {"displayName": "B Suraj"}, "created": "2026-05-01", "body": f"comment {i}"} for i in range(start, total)
            ]})
        if path == "/rest/api/2/issue/GHP-3213/transitions" and method == "GET":
            return httpx.Response(200, json={"transitions": [{"id": "31", "name": "Reopen", "to": {"name": "Open"}}]})
        if path == "/rest/api/2/attachment/501":
            return httpx.Response(200, json={"filename": "gateway.log", "mimeType": "text/plain", "size": 2048, "content": f"{BASE}/secure/attachment/501/gateway.log"})
        if path == "/rest/api/2/attachment/502":
            return httpx.Response(200, json={"filename": "diagram.png", "mimeType": "image/png", "size": 90000, "content": f"{BASE}/secure/attachment/502/diagram.png"})
        if path == "/secure/attachment/501/gateway.log":
            return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"ERROR auth timeout\n")
        if path == "/rest/api/2/issue/createmeta/GHP/issuetypes":
            return httpx.Response(200, json={"values": [{"id": "10001", "name": "Story"}]})
        if path == "/rest/api/2/issue/createmeta/GHP/issuetypes/10001":
            return httpx.Response(200, json={"values": [
                {"fieldId": "project", "name": "Project", "required": True, "schema": {"type": "project"}},
                {"fieldId": "issuetype", "name": "Issue Type", "required": True, "schema": {"type": "issuetype"}},
                {"fieldId": "summary", "name": "Summary", "required": True, "schema": {"type": "string"}},
                {"fieldId": TEAM, "name": "Team", "required": True, "schema": {"type": "option"}, "allowedValues": [{"value": "Edge"}]},
            ]})
        if path == "/rest/api/2/issue" and method == "POST":
            return httpx.Response(201, json={"key": "GHP-9999"})
        if path.startswith("/rest/api/2/issue/GHP-3213") and method in ("POST", "PUT"):
            return httpx.Response(201, json={"id": "900"})
        if path == "/rest/api/2/issue/GHP-404":
            return httpx.Response(404, json={"errorMessages": ["Issue does not exist or you do not have permission to see it."]})
        return httpx.Response(404, json={"errorMessages": [f"not faked: {method} {path}"]})


def settings(**overrides: Any) -> Settings:
    values = dict(
        jira_url=BASE, jira_personal_token="good-pat", mcp_audit_log="",
        mcp_read_only=False, use_system_certificates=False,
    )
    values.update(overrides)
    return Settings(_env_file=None, **values)


def result_of(call: types.CallToolResult) -> Any:
    if call.is_error:
        raise AssertionError(call.content[0].text if call.content else "tool error")
    content = call.structured_content or {}
    return content.get("result", content)


class ServerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        fields_module.clear_cache()
        self.jira = FakeJira()
        self.asked: List[str] = []

    def factory(self, base_url: str, token: str, deployment: str, username: str) -> JiraClient:
        return JiraClient(
            base_url, token, deployment=deployment, username=username,
            transport=httpx.MockTransport(self.jira.handle),
        )

    def call(self, tool: str, arguments: Dict[str, Any], answer: Optional[str] = "accept", **overrides: Any) -> types.CallToolResult:
        server = build_server(settings(**overrides), self.factory)

        async def elicit(_context, params):
            self.asked.append(params.message)
            if answer == "accept":
                return types.ElicitResult(action="accept", content={"approve": True})
            if answer == "accept-no":
                return types.ElicitResult(action="accept", content={"approve": False})
            return types.ElicitResult(action="decline")

        async def run():
            async with Client(server, elicitation_callback=elicit if answer else None) as client:
                return await client.call_tool(tool, arguments)

        return asyncio.run(run())


class ReadTests(ServerTestCase):
    def test_search_returns_every_page_as_compact_rows_with_extra_columns(self):
        out = result_of(self.call("search_issues", {"jql": "project = GHP AND issuetype = Story", "extra_fields": ["Sprint"]}))

        self.assertEqual((out["total"], out["returned"], out["complete"]), (150, 150, True))
        self.assertEqual(out["issues"][0]["key"], "GHP-0")
        self.assertEqual(out["issues"][0]["Sprint"], ["PI-3 Sprint 2"])
        self.assertEqual(len([r for r in self.jira.requests if r.url.path == "/rest/api/2/search"]), 2)

    def test_search_says_when_capped(self):
        out = result_of(self.call("search_issues", {"jql": "project = GHP"}, mcp_max_results=120))
        self.assertEqual((out["returned"], out["complete"]), (120, False))
        self.assertIn("120 of 150", out["note"])

    def test_issue_details_include_attachments_agile_links_subtasks_and_latest_comments(self):
        out = result_of(self.call("get_issue", {"issue_key": "ghp-3213", "comment_limit": 3}))

        self.assertEqual(out["summary"], "User Local Access (Edge)")
        self.assertEqual([a["filename"] for a in out["attachments"]], ["gateway.log", "diagram.png"])
        self.assertEqual(out["agile"], {"sprints": [{"id": 7, "name": "PI-3 Sprint 2", "state": "closed"}], "epic_link": "GHP-3000", "story_points": 5})
        self.assertEqual(out["links"][0], {"relation": "is blocked by", "key": "GHP-3100", "summary": "Gateway auth", "status": "Done"})
        self.assertEqual(out["subtasks"][0]["key"], "GHP-3214")
        self.assertEqual([c["body"] for c in out["comments"]], ["comment 9", "comment 10", "comment 11"])
        issue_request = next(r for r in self.jira.requests if r.url.path == "/rest/api/2/issue/GHP-3213")
        self.assertNotIn("*all", issue_request.url.params["fields"], "custom fields are only fetched when asked for")

    def test_narrow_sections_fetch_less(self):
        out = result_of(self.call("get_issue", {"issue_key": "GHP-3213", "sections": ["people"]}))
        self.assertEqual(set(out), {"key", "url", "assignee", "reporter", "creator", "watchers"})
        self.assertFalse([r for r in self.jira.requests if r.url.path.endswith("/comment")])

    def test_text_attachment_is_read_and_binary_is_refused_without_download(self):
        text = result_of(self.call("run_read", {"operation": "get_attachment_text", "arguments": {"attachment_id": "501"}}))
        self.assertEqual(text["text"], "ERROR auth timeout\n")

        refused = self.call("run_read", {"operation": "get_attachment_text", "arguments": {"attachment_id": "502"}})
        self.assertTrue(refused.is_error)
        self.assertIn("not a text file", refused.content[0].text)
        self.assertFalse([r for r in self.jira.requests if r.url.path.endswith("diagram.png")])

    def test_rejected_token_and_missing_issue_give_clear_errors(self):
        self.jira.token = "other"
        rejected = self.call("get_issue", {"issue_key": "GHP-3213"})
        self.assertIn("rejected the personal access token", rejected.content[0].text)

        self.jira.token = "good-pat"
        missing = self.call("get_issue", {"issue_key": "GHP-404"})
        self.assertIn("Issue does not exist", missing.content[0].text)

    def test_invalid_arguments_never_reach_jira(self):
        bad = self.call("get_issue", {"issue_key": "not a key"})
        self.assertTrue(bad.is_error)
        self.assertIn("issue_key", bad.content[0].text)
        self.assertEqual(self.jira.requests, [])


class PolicyTests(ServerTestCase):
    def test_tools_are_few_and_the_approval_is_not_a_model_argument(self):
        async def run():
            async with Client(build_server(settings(), self.factory)) as client:
                return (await client.list_tools()).tools

        tools = {t.name: t for t in asyncio.run(run())}
        self.assertEqual(set(tools), {"search_issues", "get_issue", "list_projects", "list_operations", "run_read", "run_change", "preview_change"})
        self.assertEqual(set(tools["run_change"].input_schema["properties"]), {"operation", "arguments"})

    def test_operations_load_on_demand_and_delete_is_off_by_default(self):
        groups = result_of(self.call("list_operations", {}))["groups"]
        self.assertFalse(next(g for g in groups if g["group"] == "delete")["enabled"])
        comments = result_of(self.call("list_operations", {"group": "comments"}))["operations"]
        self.assertEqual({op["operation"] for op in comments}, {"list_comments", "add_comment"})
        self.assertIn("issue_key", comments[0]["arguments"]["properties"])

        refused = self.call("run_change", {"operation": "delete_issue", "arguments": {"issue_key": "GHP-3213"}})
        self.assertTrue(refused.is_error)
        self.assertIn("turned off", refused.content[0].text)
        self.assertEqual(self.jira.writes, [])

    def test_read_only_mode_blocks_every_change(self):
        out = self.call("run_change", {"operation": "add_comment", "arguments": {"issue_key": "GHP-3213", "body": "hi"}}, mcp_read_only=True)
        self.assertIn("read-only", out.content[0].text)
        self.assertEqual((self.jira.writes, self.asked), ([], []))

    def test_reads_and_changes_need_their_own_runner(self):
        out = self.call("run_read", {"operation": "add_comment", "arguments": {"issue_key": "GHP-3213", "body": "hi"}})
        self.assertIn("run_change", out.content[0].text)
        self.assertEqual(self.jira.writes, [])

    def test_rate_limit(self):
        limiter = RateLimiter(2)
        limiter.check("user")
        limiter.check("user")
        with self.assertRaises(Exception):
            limiter.check("user")
        limiter.check("other-user")


class ConfirmationTests(ServerTestCase):
    def test_change_runs_only_after_the_user_approves_the_shown_preview(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = Path(tmp) / "audit.jsonl"
            out = result_of(self.call(
                "run_change", {"operation": "add_comment", "arguments": {"issue_key": "GHP-3213", "body": "Fixed in build 42"}},
                mcp_audit_log=str(audit),
            ))
            entries = [json.loads(line) for line in audit.read_text().splitlines()]

        self.assertEqual(out["status"], "done")
        self.assertIn("Fixed in build 42", self.asked[0])
        self.assertIn("Approve this change?", self.asked[0])
        self.assertEqual(self.jira.writes, [{"method": "POST", "path": "/rest/api/2/issue/GHP-3213/comment", "body": {"body": "Fixed in build 42"}}])
        self.assertEqual((entries[0]["operation"], entries[0]["target"], entries[0]["outcome"]), ("add_comment", "GHP-3213", "done"))
        raw = json.dumps(entries)
        self.assertNotIn("good-pat", raw)
        self.assertNotIn("Fixed in build 42", raw)

    def test_declined_or_unapproved_change_does_nothing(self):
        for answer in ("decline", "accept-no"):
            self.jira.writes.clear()
            out = result_of(self.call("run_change", {"operation": "add_comment", "arguments": {"issue_key": "GHP-3213", "body": "x"}}, answer=answer))
            self.assertEqual(out["status"], "declined", answer)
            self.assertEqual(self.jira.writes, [], answer)

    def test_client_without_confirmation_support_cannot_change_anything(self):
        out = self.call("run_change", {"operation": "add_comment", "arguments": {"issue_key": "GHP-3213", "body": "x"}}, answer=None)
        self.assertTrue(out.is_error)
        self.assertIn("cannot show approval prompts", out.content[0].text)
        self.assertEqual(self.jira.writes, [])

    def test_client_mode_trusts_the_calling_app(self):
        out = result_of(self.call(
            "run_change", {"operation": "add_comment", "arguments": {"issue_key": "GHP-3213", "body": "x"}},
            answer=None, mcp_confirmation_mode="client",
        ))
        self.assertEqual(out["status"], "done")
        self.assertEqual(len(self.jira.writes), 1)

    def test_transition_preview_and_impossible_target(self):
        preview = result_of(self.call("preview_change", {"operation": "transition_issue", "arguments": {"issue_key": "GHP-3213", "to": "Open"}}))
        self.assertIn("Done → Open", preview["preview"])

        impossible = self.call("run_change", {"operation": "transition_issue", "arguments": {"issue_key": "GHP-3213", "to": "Closed"}})
        self.assertIn("Possible: Reopen (to Open)", impossible.content[0].text)
        self.assertEqual(self.jira.writes, [])

    def test_only_allowed_fields_can_be_updated(self):
        blocked = self.call("run_change", {"operation": "update_issue", "arguments": {"issue_key": "GHP-3213", "extra_fields": {"Team": "Edge"}}})
        self.assertIn("JIRA_UPDATABLE_CUSTOM_FIELDS", blocked.content[0].text)
        self.assertEqual(self.jira.writes, [])

        allowed = result_of(self.call(
            "run_change", {"operation": "update_issue", "arguments": {"issue_key": "GHP-3213", "extra_fields": {"Team": "Edge"}}},
            jira_updatable_custom_fields="Team",
        ))
        self.assertEqual(allowed["status"], "done")
        self.assertEqual(self.jira.writes[0]["body"], {"fields": {TEAM: "Edge"}})

    def test_create_shows_every_field_in_full_and_cannot_retarget_the_project(self):
        description = "Harmless start. " + "x" * 600 + " HIDDEN-END"
        arguments = {"project_key": "GHP", "issue_type": "Story", "summary": "Local access", "description": description,
                     "extra_fields": {"Team": "Edge"}}
        out = result_of(self.call("run_change", {"operation": "create_issue", "arguments": arguments}))

        self.assertEqual(out["result"]["key"], "GHP-9999")
        prompt = self.asked[0]
        self.assertIn("- Project: GHP", prompt)
        self.assertIn("- Issue Type: Story", prompt)
        self.assertIn("HIDDEN-END", prompt, "long text is shown in full before approval")
        self.assertEqual(self.jira.writes[0]["body"]["fields"][TEAM], {"value": "Edge"})

        self.jira.writes.clear()
        retarget = dict(arguments, extra_fields={"Team": "Edge", "Project": {"key": "OPS"}})
        refused = self.call("run_change", {"operation": "create_issue", "arguments": retarget})
        self.assertIn("cannot be set through extra_fields", refused.content[0].text)
        self.assertEqual(self.jira.writes, [])


class SessionTests(unittest.TestCase):
    def test_http_requests_need_their_own_token_and_an_allowed_site(self):
        s = settings(jira_personal_token="server-token")
        with self.assertRaises(AuthError):
            resolve_session({}, s, ["jira.example.com"])
        session = resolve_session({"authorization": "Bearer user-token"}, s, ["jira.example.com"])
        self.assertEqual((session.token, session.base_url), ("user-token", BASE))
        with self.assertRaises(ConfigError):
            resolve_session({"authorization": "Bearer user-token", "x-jira-url": "https://evil.example.net"}, s, ["jira.example.com"])

    def test_site_urls_are_normalized(self):
        self.assertEqual(normalize_base_url("jira.example.com/browse/GHP-1", None), BASE)
        self.assertEqual(normalize_base_url("https://example.com/jira/secure/Dashboard.jspa", None), "https://example.com/jira")
        with self.assertRaises(ConfigError):
            normalize_base_url("http://jira.example.com", None)
        for unsafe in (
            "https://user:password@jira.example.com",
            "https://jira.example.com/jira?token=secret",
            "https://jira.example.com/jira#fragment",
            "https://jira.example.com/jira/../admin",
            "https://jira.example.com/jira/%2e%2e/admin",
            "https://jira.example.com:invalid",
        ):
            with self.subTest(url=unsafe), self.assertRaises(ConfigError):
                normalize_base_url(unsafe, None)

    def test_allowed_jira_origin_includes_the_port(self):
        with self.assertRaises(ConfigError):
            normalize_base_url("https://jira.example.com:8443", ["jira.example.com"])
        self.assertEqual(
            normalize_base_url("https://jira.example.com:8443", ["jira.example.com:8443"]),
            "https://jira.example.com:8443",
        )

    def test_auth_header_is_bounded_and_default_settings_are_read_only(self):
        configured = Settings(_env_file=None, jira_url=BASE)
        self.assertTrue(configured.mcp_read_only)
        with self.assertRaises(AuthError):
            resolve_session(
                {"authorization": "Bearer " + ("x" * 9000)},
                settings(),
                ["jira.example.com"],
            )

    def test_client_refuses_unsafe_paths_and_non_https_sites(self):
        client = JiraClient(BASE, "good-pat", transport=httpx.MockTransport(lambda _request: httpx.Response(200)))

        async def run():
            try:
                for path in (
                    "https://evil.example/rest/api/2/myself",
                    "/rest/api/2/../admin",
                    "/rest/api/2/%2e%2e/admin",
                    "/rest/api/2/myself?redirect=evil",
                    "/rest/api/2\\\\admin",
                ):
                    with self.subTest(path=path), self.assertRaises(ValidationError):
                        await client.get(path)
            finally:
                await client.__aexit__()

        asyncio.run(run())
        with self.assertRaises(ValidationError):
            JiraClient("http://jira.example.com", "good-pat")

    def test_proxy_requires_explicit_trust(self):
        with self.assertRaisesRegex(ValidationError, "JIRA_TRUST_HTTPS_PROXY"):
            JiraClient(BASE, "good-pat", proxy="http://proxy.example:8080")

    def test_jira_client_uses_the_pinned_transport_by_default(self):
        async def run():
            async with JiraClient(BASE, "good-pat") as client:
                self.assertIsInstance(client._client._transport, PinnedAsyncHTTPTransport)

        asyncio.run(run())

    def test_audit_omits_arguments_and_provider_error_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.jsonl"
            AuditLog(str(path)).record(
                "user-hash", BASE, "add_comment", "GHP-1", "failed",
                {"body": "confidential comment"}, "provider echoed confidential comment",
            )
            text = path.read_text()

        self.assertNotIn("confidential comment", text)
        self.assertNotIn("arguments_sha256", text)
        self.assertIn('"outcome": "failed"', text)


class _FakeResolver:
    def __init__(self, address: str) -> None:
        self.address = address

    async def getaddrinfo(self, _host, port, type):
        return [(socket.AF_INET6 if ":" in self.address else socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 (self.address, port, 0, 0) if ":" in self.address else (self.address, port))]


class _FakeNetworkBackend(httpcore.AsyncNetworkBackend):
    def __init__(self) -> None:
        self.connected_to = []

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.connected_to.append((host, port))
        return object()

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        return object()

    async def sleep(self, seconds):
        return None


class NetworkPolicyTests(unittest.TestCase):
    def test_dns_result_is_pinned_to_the_validated_ip(self):
        resolver = _FakeResolver("8.8.8.8")
        network = _FakeNetworkBackend()
        backend = PinnedNetworkBackend([], delegate=network)

        with patch("secure_jira_mcp.network.asyncio.get_running_loop", return_value=resolver):
            asyncio.run(backend.connect_tcp("jira.example.com", 443))

        self.assertEqual(network.connected_to, [("8.8.8.8", 443)])

    def test_private_dns_result_requires_an_explicit_allowed_network(self):
        resolver = _FakeResolver("10.20.30.40")
        backend = PinnedNetworkBackend([], delegate=_FakeNetworkBackend())
        with patch("secure_jira_mcp.network.asyncio.get_running_loop", return_value=resolver):
            with self.assertRaisesRegex(httpcore.ConnectError, "JIRA_ALLOWED_NETWORKS"):
                asyncio.run(backend.connect_tcp("jira.internal.example", 443))

        allowed = [ipaddress.ip_network("10.20.0.0/16")]
        network = _FakeNetworkBackend()
        backend = PinnedNetworkBackend(allowed, delegate=network)
        with patch("secure_jira_mcp.network.asyncio.get_running_loop", return_value=resolver):
            asyncio.run(backend.connect_tcp("jira.internal.example", 443))
        self.assertEqual(network.connected_to, [("10.20.30.40", 443)])

    def test_private_dns_is_allowed_for_exact_configured_service_host(self):
        resolver = _FakeResolver("10.20.30.40")
        network = _FakeNetworkBackend()
        backend = PinnedNetworkBackend(
            [],
            delegate=network,
            trusted_private_hosts=["jira.internal.example"],
        )
        with patch("secure_jira_mcp.network.asyncio.get_running_loop", return_value=resolver):
            asyncio.run(backend.connect_tcp("jira.internal.example", 443))
        self.assertEqual(network.connected_to, [("10.20.30.40", 443)])

    def test_private_dns_is_not_allowed_for_a_different_host(self):
        resolver = _FakeResolver("10.20.30.40")
        backend = PinnedNetworkBackend(
            [],
            delegate=_FakeNetworkBackend(),
            trusted_private_hosts=["jira.internal.example"],
        )
        with patch("secure_jira_mcp.network.asyncio.get_running_loop", return_value=resolver):
            with self.assertRaisesRegex(httpcore.ConnectError, "JIRA_ALLOWED_NETWORKS"):
                asyncio.run(backend.connect_tcp("other.internal.example", 443))

    def test_exact_host_trust_does_not_allow_loopback_or_metadata_addresses(self):
        for address in ("127.0.0.1", "169.254.169.254", "100.100.100.200", "fd00:ec2::254"):
            with self.subTest(address=address):
                resolver = _FakeResolver(address)
                backend = PinnedNetworkBackend(
                    [],
                    delegate=_FakeNetworkBackend(),
                    trusted_private_hosts=["jira.internal.example"],
                )
                with patch("secure_jira_mcp.network.asyncio.get_running_loop", return_value=resolver):
                    with self.assertRaisesRegex(httpcore.ConnectError, "JIRA_ALLOWED_NETWORKS"):
                        asyncio.run(backend.connect_tcp("jira.internal.example", 443))

    def test_non_global_ip_literal_is_checked_too(self):
        backend = PinnedNetworkBackend([], delegate=_FakeNetworkBackend())
        with self.assertRaisesRegex(httpcore.ConnectError, "JIRA_ALLOWED_NETWORKS"):
            asyncio.run(backend.connect_tcp("127.0.0.1", 443))

    def test_allowed_network_setting_is_parsed_and_invalid_cidrs_fail_closed(self):
        configured = settings(jira_allowed_networks="10.20.0.0/16,fd00:1234::/48")
        self.assertEqual(
            configured.allowed_networks(),
            [ipaddress.ip_network("10.20.0.0/16"), ipaddress.ip_network("fd00:1234::/48")],
        )
        with self.assertRaises(ConfigError):
            settings(jira_allowed_networks="10.20.0.999/16").allowed_networks()

    def test_pinned_http_transport_connects_only_to_an_explicitly_allowed_private_ip(self):
        requests = []

        async def respond(reader, writer):
            request_line = await reader.readline()
            headers = []
            while line := await reader.readline():
                if line == b"\r\n":
                    break
                headers.append(line.decode("ascii").strip())
            requests.append((request_line.decode("ascii").strip(), headers))
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        async def run():
            listener = await asyncio.start_server(respond, "127.0.0.1", 0)
            port = listener.sockets[0].getsockname()[1]
            try:
                transport = PinnedAsyncHTTPTransport([ipaddress.ip_network("127.0.0.0/8")])
                async with httpx.AsyncClient(
                    transport=transport,
                    trust_env=False,
                    base_url=f"http://127.0.0.1:{port}",
                ) as client:
                    response = await client.get("/rest/api/2/myself")
                    self.assertEqual(response.text, "ok")
            finally:
                listener.close()
                await listener.wait_closed()
            return port

        port = asyncio.run(run())
        self.assertEqual(requests[0][0], "GET /rest/api/2/myself HTTP/1.1")
        self.assertIn(f"Host: 127.0.0.1:{port}", requests[0][1])


class HttpListenerTests(unittest.TestCase):
    def test_remote_http_requires_tls_and_allowed_host(self):
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            result = cli.main([
                "--no-env-file", "--transport", "http", "--host", "0.0.0.0", "--allow-remote",
            ])
        self.assertEqual(result, 2)
        self.assertIn("requires --tls-certfile", stderr.getvalue())

    def test_mtls_requires_client_certificates_and_enforces_host_allowlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            cert = Path(tmp) / "server.pem"
            key = Path(tmp) / "server.key"
            ca = Path(tmp) / "clients.pem"
            for path in (cert, key, ca):
                path.write_text("test", encoding="utf-8")
            with patch.dict("os.environ", {"MCP_HTTP_ALLOWED_HOSTS": "mcp.example.com:9200"}), \
                    patch("mcp.server.mcpserver.MCPServer.streamable_http_app", return_value=object()) as app_factory, \
                    patch("uvicorn.Config") as config_factory, patch("uvicorn.Server") as server_factory, \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main([
                    "--no-env-file", "--transport", "http", "--host", "0.0.0.0", "--port", "9200",
                    "--allow-remote", "--tls-certfile", str(cert), "--tls-keyfile", str(key),
                    "--mtls-ca-file", str(ca),
                ]), 0)

        kwargs = config_factory.call_args.kwargs
        self.assertEqual(kwargs["ssl_cert_reqs"], ssl.CERT_REQUIRED)
        self.assertEqual(kwargs["ssl_ca_certs"], str(ca))
        transport_security = app_factory.call_args.kwargs["transport_security"]
        self.assertTrue(transport_security.enable_dns_rebinding_protection)
        self.assertEqual(transport_security.allowed_hosts, ["mcp.example.com:9200"])
        server_factory.return_value.run.assert_called_once_with()

    def test_http_listener_rejects_unlisted_host_and_origin(self):
        app = build_server(settings()).streamable_http_app(
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=["mcp.example.com:9200"],
                allowed_origins=["https://client.example"],
            ),
        )

        async def run():
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://mcp.example.com:9200",
                ) as client:
                    bad_host = await client.get("/mcp", headers={"host": "attacker.example"})
                    bad_origin = await client.get("/mcp", headers={"origin": "https://attacker.example"})
                    allowed_origin = await client.get("/mcp", headers={"origin": "https://client.example"})
                    return bad_host.status_code, bad_origin.status_code, allowed_origin.status_code

        bad_host, bad_origin, allowed_origin = asyncio.run(run())
        self.assertEqual(bad_host, 421)
        self.assertEqual(bad_origin, 403)
        self.assertNotIn(allowed_origin, (403, 421))


if __name__ == "__main__":
    unittest.main()
