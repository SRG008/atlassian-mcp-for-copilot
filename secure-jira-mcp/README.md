# secure-jira-mcp

A small, policy-controlled **MCP server for Jira Data Center / Server**. It is built to fix the problems of
general-purpose servers such as mcp-atlassian: it exposes few tools at a time, the administrator decides
what can exist, every change is approved by a human inside the server, ticket details are complete,
lists are never silently cut at 10, and every change attempt is audited.

For **Jira Cloud** use Atlassian's official Rovo MCP server; this project targets Data Center / Server
(Jira 8.x and 9.x, REST API v2 and Agile API 1.0).

## Why this instead of mcp-atlassian

| | mcp-atlassian | secure-jira-mcp |
|---|---|---|
| Tools sent to the model | ~39 Jira tools on every request | 7 core tools; other operations are loaded on demand with `list_operations` |
| What may exist | everything, unless filtered | admin policy `MCP_TOOL_GROUPS`; `delete` is off by default; `MCP_READ_ONLY` |
| Confirmation of changes | none (left to the client) | the **server** asks the user (MCP elicitation) with an exact preview; the model cannot approve |
| Writable fields | any | safe standard fields only; custom fields only if named in `JIRA_UPDATABLE_CUSTOM_FIELDS` |
| Ticket details | default field list (no attachments, sprint, ...) | attachments, sprint, epic, story points, links, sub-tasks, latest comments by default; more on request |
| Lists | 10 items by default | every page, up to `MCP_MAX_RESULTS`, with total and a note when capped |
| Server file access | attachment tools read server files | none; attachments are read from Jira only, text types only, size-limited |
| Hosts a token can reach | any allowed URL header | only `JIRA_ALLOWED_HOSTS`; redirects are never followed |
| Audit | none | JSON line per change attempt (no tokens, no changed text) |
| Rate limit | none | per user, `MCP_RATE_LIMIT_PER_MINUTE` |
| Dependencies | ~100 packages | `mcp`, `httpx`, `pydantic`, `pydantic-settings`, `truststore` |

## How it works

```
MCP client (VS Code, an agent app, ...)
   │  stdio, or HTTP with "Authorization: Bearer <PAT>"
   ▼
secure-jira-mcp ── policy check ── strict argument check ── rate limit
   │                                   │
   │   changes: preview ──► the server asks the user "Approve this change?" ──► run only if approved ──► audit
   ▼
Jira Data Center REST API (only the allowed host, Bearer PAT, no redirects)
```

### Tools the model always sees

| Tool | Purpose |
|---|---|
| `search_issues(jql, max_results?, extra_fields?, count_only?)` | Every matching item as compact rows (key, summary, type, status, priority, assignee, updated). `extra_fields` adds columns such as `Sprint` or `Story Points`. |
| `get_issue(issue_key, sections?, comment_limit?)` | One work item. Default sections: details, people, dates, agile (sprint, epic, story points), attachments, links, subtasks, comments. Optional: `custom_fields`, `time`, `history`, `all`. |
| `list_projects()` | Projects you can see. |
| `list_operations(group?)` | The operation groups (and which are enabled); with a group, its operations and their argument schemas. |
| `run_read(operation, arguments)` | Run a read operation. |
| `run_change(operation, arguments)` | Run a change. The user approves it first. |
| `preview_change(operation, arguments)` | Show what a change would do, without doing it. |

### Operation groups

| Group | Operations |
|---|---|
| `search` | `search_issues` |
| `read` | `get_issue`, `list_projects`, `get_project`, `list_issue_types`, `get_transitions`, `list_components`, `list_versions` |
| `attachments` | `get_attachment_text` (text-like files, size-limited) |
| `comments` | `list_comments`, `add_comment`* |
| `write` | `create_issue`*, `update_issue`*, `assign_issue`* |
| `transitions` | `transition_issue`* |
| `agile` | `list_boards`, `list_sprints`, `get_sprint_issues`, `add_issues_to_sprint`* |
| `users` | `search_users` |
| `fields` | `list_fields`, `get_create_fields` |
| `links` | `list_link_types`, `link_issues`* |
| `delete` (off by default) | `delete_issue`*, `delete_comment`* |

\* change: needs approval.

### Three layers of control

1. **Administrator** (`.env`): `MCP_TOOL_GROUPS`, `MCP_READ_ONLY`, `JIRA_ALLOWED_HOSTS`, `JIRA_UPDATABLE_CUSTOM_FIELDS`. The model cannot change these.
2. **The user**: approves or declines every change, seeing its exact effect (old → new values, target status, comment text).
3. **The model**: loads only the operations a task needs (`list_operations(group)`), which keeps each request small and cheap.

### Confirmation modes

- `MCP_CONFIRMATION_MODE=elicit` (default): the server asks the user through **MCP elicitation**. The approval is not a tool argument, so the model cannot fill it in. A client that cannot show the prompt cannot make changes at all.
- `MCP_CONFIRMATION_MODE=client`: for an app that already has its own approval step (for example the Troubleshooting Agent's Approve/Reject gate). The server then runs a change when it is called, still with policy, validation and audit.

## Install

Requires Python 3.11+.

```powershell
cd "C:\Agentic AI\secure-jira-mcp"
python -m venv .venv
.venv\Scripts\python -m pip install -e .
Copy-Item .env.example .env
```

Behind Zscaler or a similar proxy, set `$env:HTTPS_PROXY="http://127.0.0.1:9000"` before `pip install`.

## Configure (`.env`)

1. `JIRA_URL=https://jira.company.com`
2. For the single-user stdio mode: `JIRA_PERSONAL_TOKEN=` a token from Jira (profile > Personal Access Tokens).
3. Review `MCP_TOOL_GROUPS` and `MCP_READ_ONLY`. Add `delete` only if you really want it.
4. On a corporate network: `HTTPS_PROXY=http://127.0.0.1:9000` (Zscaler) and keep `USE_SYSTEM_CERTIFICATES=true`.

## Run

**stdio** (one user; the MCP client starts the server):

```powershell
.venv\Scripts\secure-jira-mcp --transport stdio --env-file .env
```

**HTTP** (many users; each request sends its own token):

```powershell
.venv\Scripts\secure-jira-mcp --transport http --port 9200 --env-file .env
```

The server listens on `127.0.0.1` only. Clients send `Authorization: Bearer <their PAT>` and, optionally,
`X-Jira-Url` (must be in `JIRA_ALLOWED_HOSTS`). To listen on another address use `--allow-remote` and put a
TLS reverse proxy in front: tokens travel in headers.

### Use it from VS Code (stdio)

`.vscode/mcp.json` in any workspace:

```json
{
  "servers": {
    "secure-jira": {
      "type": "stdio",
      "command": "C:\\Agentic AI\\secure-jira-mcp\\.venv\\Scripts\\secure-jira-mcp.exe",
      "args": ["--transport", "stdio", "--env-file", "C:\\Agentic AI\\secure-jira-mcp\\.env"]
    }
  }
}
```

Use absolute paths: the client decides the working folder. Set `MCP_AUDIT_LOG` to an absolute path too.

## Test

```powershell
.venv\Scripts\python -m unittest discover -s tests -t .
```

The tests run a real MCP client in-process against a fake Jira Data Center and check: complete paging,
full ticket details, the policy, read-only mode, approval and decline, clients without approval support,
field allow-listing, attachment limits, host allowlist, and that the audit log holds no tokens or text.

## Audit log

`MCP_AUDIT_LOG` (default `audit.jsonl`) gets one line per change attempt:

```json
{"time": "...", "user": "<hash of site+token>", "site": "https://jira.company.com", "operation": "add_comment",
 "target": "GHP-3213", "outcome": "done|declined|failed", "arguments_sha256": "..."}
```

## Limits

- Jira Data Center / Server only. For Jira Cloud use Atlassian's Rovo MCP server.
- Built and tested against a fake Jira that follows the REST API v2 and Agile 1.0 formats. Try it on a test
  project first; field formats can differ between Jira versions and plugins.
- `create_issue` uses the createmeta API (Jira 8.4+ endpoints, with a fallback for older versions).
