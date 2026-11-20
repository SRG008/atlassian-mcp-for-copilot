"""Tests against a fake Jira Data Center, through a real in-process MCP client."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from mcp import Client, types

from secure_jira_mcp import fields as fields_module
from secure_jira_mcp.config import Settings, normalize_base_url
from secure_jira_mcp.errors import AuthError, ConfigError
from secure_jira_mcp.jira_client import JiraClient
from secure_jira_mcp.policy import RateLimiter
from secure_jira_mcp.server import build_server, resolve_session

BASE = "https://jira.example.com"
SPRINT = "customfield_10010"
POINTS = "customfield_10002"
EPIC = "customfield_10008"
TEAM = "customfield_10500"


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
    values = dict(jira_url=BASE, jira_personal_token="good-pat", mcp_audit_log="", use_system_certificates=False)
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

    def factory(self, base_url: str, token: str) -> JiraClient:
        return JiraClient(base_url, token, transport=httpx.MockTransport(self.jira.handle))

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


if __name__ == "__main__":
    unittest.main()
