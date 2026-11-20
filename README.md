# Atlassian MCP for Copilot

A policy-controlled **Model Context Protocol (MCP) server for Jira and Confluence Cloud and Data Center / Server**.
It connects Copilot and other compatible MCP clients to Atlassian through the standard MCP tool interface.
Use the VS Code extension for guided setup and direct editor integration, or run the Python server with stdio
or Streamable HTTP transport.

Jira and Confluence use separate site URLs and credentials. Jira Cloud supports standard and scoped API tokens
with account-email authentication and REST API v3; Confluence Cloud supports standard and scoped API tokens
with account-email authentication and REST API v1. Data Center / Server uses personal access tokens (PATs).
OAuth is not implemented.

## What this application provides

- **MCP-native integration:** Uses the Python MCP SDK and supports stdio for locally managed clients and
  Streamable HTTP for HTTP deployments. The VS Code extension verifies credentials, stores them in VS Code
  SecretStorage, and lets VS Code manage the server process.
- **Controlled capabilities:** Administrators choose enabled tool groups. The server starts read-only by
  default, validates operation arguments, and checks policy at execution time—not just when tools are listed.
- **Human-confirmed changes:** In the default `elicit` mode, the server presents a preview and requires an
  explicit user decision before a change runs. If the client cannot show the approval prompt, changes are
  refused. Read-only mode and disabled tool groups remain enforced by the server.
- **Hardened outbound requests:** HTTPS origins and API paths are constrained, redirects are refused, and
  direct connections validate and pin resolved addresses. Private addresses are allowed only for the exact
  configured service hostname; loopback, link-local, and cloud-metadata addresses remain blocked.
- **Bounded data handling:** Search and listing results can be limited, issue details can be selected by
  section, and attachment handling has size and content constraints. The server does not expose arbitrary
  filesystem access.
- **Operational visibility:** Rate limits and a JSONL change-attempt audit are available. Audit entries record
  metadata without tool argument values or provider error text.

These are application design features, not a guarantee of security under every deployment. Keep credentials
private, configure only the required tool groups, and use the network and transport controls appropriate to
your environment.

## How it works

```
MCP client (VS Code, an agent app, ...)
   │  stdio, or Host/Origin-checked HTTP(S); optional mTLS
   ▼
secure-jira-mcp ── policy check ── strict argument check ── rate limit
   │                                   │
   │   changes: preview ──► the server asks the user "Approve this change?" ──► run only if approved ──► audit
   │   outbound: DNS resolve ──► validate IP/CIDR ──► connect to that IP with original HTTPS SNI
   ├── Jira REST API (allowed HTTPS origin, Cloud API token or Data Center PAT, no redirects)
   └── optional Confluence REST API (independent allowed HTTPS origin and credentials, no redirects)
```

### Security and approval flow

1. **The administrator configures the boundary.** Environment settings select the Jira and optional
   Confluence sites, enabled tool groups, read-only mode, allowed hosts/networks, and rate limits.
2. **The client supplies or selects credentials.** In stdio mode, credentials come from server settings. In
   HTTP mode, each request supplies credentials in headers; the server validates the requested site against
   its configured origin policy.
3. **The server validates each operation.** It checks the operation group, arguments, and write policy before
   making an Atlassian request. Outbound requests use HTTPS, reject redirects, and apply DNS/IP protections.
4. **Changes require a separate approval step.** The server shows the proposed effect and proceeds only after
   approval. The approval is not supplied as a normal tool argument. `MCP_CONFIRMATION_MODE=client` is an
   explicit alternative for clients that already provide their own approval gate.
5. **The attempt is audited.** The JSONL audit records the operation outcome and metadata without storing
   argument values or provider error text.

For remote HTTP deployments, the server refuses a non-loopback listener unless remote access is explicitly
enabled, TLS is configured, and allowed hosts are set. Optional mTLS authenticates clients to this MCP server;
it does not authenticate the server to Jira or Confluence.

### Core tools

| Tool | Purpose |
|---|---|
| `search_issues(jql, max_results?, extra_fields?, count_only?)` | Every matching item as compact rows (key, summary, type, status, priority, assignee, updated). `extra_fields` adds columns such as `Sprint` or `Story Points`. |
| `get_issue(issue_key, sections?, comment_limit?)` | One work item. Default sections: details, people, dates, agile (sprint, epic, story points), attachments, links, subtasks, comments. Optional: `custom_fields`, `time`, `history`, `all`. |
| `list_projects()` | Projects you can see. |
| `list_operations(group?)` | The operation groups (and which are enabled); with a group, its operations and their argument schemas. |
| `run_read(operation, arguments)` | Run a read operation. |
| `run_change(operation, arguments)` | Run a change. The user approves it first. |
| `preview_change(operation, arguments)` | Show what a change would do, without doing it. |

When a Confluence URL is configured, the server also exposes `search_confluence_pages`,
`get_confluence_page`, `list_confluence_spaces`, `list_page_comments`, `list_page_attachments`, and
`download_confluence_attachment`. `list_confluence_operations` describes the approval-gated changes run through
`run_confluence_change`: create/update pages, add/update comments, and upload or replace attachments.
Confluence page/comment bodies are accepted as plain text and escaped into storage format; attachment bytes are
passed as base64 and capped by `MCP_MAX_ATTACHMENT_BYTES`.

### Token-efficient tool and result design

The server is designed to keep model context focused by not exposing every operation schema for every request:

- The core Jira tools are always available. `list_operations()` first returns the enabled groups; the client
  can then call `list_operations(group)` to retrieve schemas for just the relevant group.
- Operations are invoked through the shared `run_read` and `run_change` tools, rather than exposing every
  operation as a separate tool with its own schema on every request. Confluence change schemas are discovered
  separately with `list_confluence_operations`.
- Jira search returns compact rows and handles pagination on the server. `get_issue` returns a useful default
  set of sections, while callers can request specific sections or fields when they need less or more detail.
  Confluence search and listing tools use capped pages.

This can reduce **model context tokens** spent on tool definitions and unnecessary response data. It does not
change Atlassian API-token authentication, and it does not guarantee a fixed token count or percentage saving:
the result depends on the model, client, task, and requested data. Large result sets or detailed issue history
can still produce large responses. The shared `run_read`/`run_change` interface also means a client may need an
extra discovery call before using a less common operation; that is the trade-off for exposing fewer schemas up
front.

For a connection check, call `list_projects` for Jira or `list_confluence_spaces` for Confluence. Use the MCP
tools directly for Jira and Confluence work; tool availability and selection are ultimately controlled by the
MCP client.

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
| `confluence_read` | Search pages, read pages, list spaces and comments |
| `confluence_write` | Create/update pages, add/update comments* |
| `confluence_attachments` | List/download attachments; upload/replace attachments* |

\* change: needs approval.

### Three layers of control

1. **Administrator** (`.env`): `MCP_TOOL_GROUPS`, `MCP_READ_ONLY` (true by default), `JIRA_ALLOWED_HOSTS`, `JIRA_UPDATABLE_CUSTOM_FIELDS`. The model cannot change these.
2. **The user**: approves or declines every change, seeing its exact effect (old → new values, target status, comment text).
3. **The model**: loads only the operations a task needs (`list_operations(group)`), which keeps each request small and cheap.

### Confirmation modes

- `MCP_CONFIRMATION_MODE=elicit` (default): the server asks the user through **MCP elicitation**. The approval is not a tool argument, so the model cannot fill it in. A client that cannot show the prompt cannot make changes at all.
- `MCP_CONFIRMATION_MODE=client`: for a client that already has its own approval step. The server then runs a change when it is called, still with policy, validation and audit.

## Install

Requires Python 3.11+.

```powershell
cd "C:\path\to\atlassian-mcp-for-copilot"
python -m venv .venv
.venv\Scripts\python -m pip install -e .
Copy-Item .env.example .env
```

Behind a corporate HTTPS proxy, set `$env:HTTPS_PROXY="http://127.0.0.1:9000"` before `pip install`.

## Configure (`.env`)

1. `JIRA_URL=https://jira.example.com`
2. Set `JIRA_DEPLOYMENT=data_center` and `JIRA_PERSONAL_TOKEN=` for a Data Center / Server PAT, or
   `JIRA_DEPLOYMENT=cloud`, `JIRA_USERNAME=` (your Atlassian account email), and `JIRA_API_TOKEN=` for Jira Cloud.
   The Cloud API token must belong to that account email. Set `JIRA_API_TOKEN_SCOPED=true` for a token created
   with scopes; scoped tokens are sent only to Atlassian's Jira API gateway after the public site ID is resolved.
   Otherwise, the default `false` uses a standard API token against the Jira site URL.
3. Keep `MCP_READ_ONLY=true` unless Jira writes are necessary. If enabling writes, retain `MCP_CONFIRMATION_MODE=elicit` and add `delete` only if you really want it.
4. Private Jira deployments that resolve to an RFC1918 or unique-local IP are allowed for the exact hostname in `JIRA_URL`; DNS is still resolved and pinned for each connection. Loopback, link-local, and cloud-metadata IPs remain blocked. If the deployment uses a non-standard non-global range, `JIRA_ALLOWED_NETWORKS` can allow only the network-team-approved CIDR(s).
5. On a corporate network, set `HTTPS_PROXY=http://127.0.0.1:9000` and keep `USE_SYSTEM_CERTIFICATES=true`. Because the proxy resolves Jira's hostname, also set `JIRA_TRUST_HTTPS_PROXY=true` only when that proxy is trusted and restricts outbound destinations.
6. To enable Confluence, set `CONFLUENCE_URL` and its separate credentials. Use `CONFLUENCE_DEPLOYMENT=cloud` with `CONFLUENCE_USERNAME` and `CONFLUENCE_API_TOKEN`, or `data_center` with `CONFLUENCE_PERSONAL_TOKEN`. Set `CONFLUENCE_API_TOKEN_SCOPED=true` for a Cloud token created with scopes; otherwise, the default `false` uses a standard token against the Confluence site URL. Private IPs are allowed only for the exact hostname in `CONFLUENCE_URL`; loopback, link-local, and cloud-metadata IPs remain blocked. For a non-standard non-global range, use only the network-team-approved CIDRs in `CONFLUENCE_ALLOWED_NETWORKS`. With an HTTPS proxy, also set `CONFLUENCE_TRUST_HTTPS_PROXY=true` only for a trusted, restricted proxy.

## Run

**stdio** (one user; the MCP client starts the server):

```powershell
.venv\Scripts\secure-jira-mcp --transport stdio --env-file .env
```

**HTTP** on loopback (each request sends its own Jira token; Host/Origin checks are enabled):

```powershell
.venv\Scripts\secure-jira-mcp --transport http --port 9200 --env-file .env
```

The server listens on `127.0.0.1` only by default. Jira HTTP callers send `Authorization: Bearer <their PAT>`
for Data Center or HTTP Basic `<account-email>:<API-token>` for Cloud. Confluence HTTP callers send
`X-Confluence-Token`, plus `X-Confluence-Username` for Cloud; optionally send `X-Confluence-Url` (its HTTPS
origin, including a non-standard port, must be in `CONFLUENCE_ALLOWED_HOSTS`).

For a remote listener, configure the expected host and use built-in TLS:

```powershell
$env:MCP_HTTP_ALLOWED_HOSTS = "mcp.example.com:9200"
.venv\Scripts\secure-jira-mcp --transport http --host 0.0.0.0 --port 9200 --allow-remote `
  --tls-certfile .\server.pem --tls-keyfile .\server.key --mtls-ca-file .\client-ca.pem --env-file .env
```

`--mtls-ca-file` is optional; when supplied, clients must present a certificate signed by that CA, and still send
their Jira credentials. Protect the server key file. Set `MCP_HTTP_ALLOWED_ORIGINS` if browser-based clients need
specific Origin values. A remote plaintext listener is refused; a reverse proxy can terminate TLS and connect to a
loopback-bound HTTP listener on the same host. If the proxy preserves the public `Host` header, include that
host-and-port value in `MCP_HTTP_ALLOWED_HOSTS`.

### Use it from VS Code (stdio)

For the installable VS Code extension, see the [extension setup guide](vscode-extension/README.md).
The following is the manual setup; use one method or the other.

`.vscode/mcp.json` in any workspace:

```json
{
  "servers": {
    "secure-jira": {
      "type": "stdio",
      "command": "C:\\path\\to\\atlassian-mcp-for-copilot\\.venv\\Scripts\\secure-jira-mcp.exe",
      "args": ["--transport", "stdio", "--env-file", "C:\\path\\to\\atlassian-mcp-for-copilot\\.env"]
    }
  }
}
```

Use absolute paths: the client decides the working folder. Set `MCP_AUDIT_LOG` to an absolute path too.

### Package the VS Code extension

From the project root, with Node.js 22+ installed:

```powershell
cd vscode-extension
npm ci
npx --no-install vsce package --allow-missing-repository --skip-license
```

Verify the `publisher` in `vscode-extension/package.json` matches the public ID in Marketplace
publisher management. Install the `.vsix` from the Extensions view using **Install from VSIX...**
and follow the [extension setup guide](vscode-extension/README.md) before uploading. For updates,
bump the extension version and build a new `.vsix`.
The command above builds a local test package without a license file. Decide on distribution
terms and include the appropriate license in the extension before publishing publicly.

## Try it from the terminal

`examples/try_it.py` starts the server (stdio) with your `.env` and runs one command. Changes show the
server's approval prompt in the terminal.

```powershell
.venv\Scripts\python examples\try_it.py projects
.venv\Scripts\python examples\try_it.py search "project = PROJ AND issuetype = Story ORDER BY updated DESC"
.venv\Scripts\python examples\try_it.py issue PROJ-123
.venv\Scripts\python examples\try_it.py ops agile
.venv\Scripts\python examples\try_it.py read list_boards "{\"project_key\": \"PROJ\"}"
.venv\Scripts\python examples\try_it.py comment PROJ-123 "Test comment"
```

## Test

```powershell
.venv\Scripts\python -m unittest discover -s tests -t .
```

The tests run a real MCP client in-process against fake Jira and Confluence APIs and check: complete Jira paging,
full ticket details, the policy, read-only mode, approval and decline, clients without approval support,
field allow-listing, attachment limits, host allowlist, and that the audit log holds no tokens or text.
Jira Cloud tests cover API-token authentication, REST API v3 routing, token pagination and ADF text conversion.
Confluence coverage checks both authentication modes, REST API v1 routing, approval/read-only behavior, and
attachment bounds. These tests do not replace validation against real Atlassian Cloud or Data Center sites.

## Audit log

`MCP_AUDIT_LOG` (default `audit.jsonl`) gets one line per change attempt. It records operation metadata only—no tool arguments, argument hashes, or provider error text:

```json
{"time": "...", "user": "<hash of site+token>", "site": "https://jira.example.com", "operation": "add_comment",
 "target": "PROJ-123", "outcome": "done|declined|failed", "argument_count": 2}
```

## Limits

- OAuth is not implemented; mTLS here authenticates HTTP clients to this server, not to Jira or Confluence.
- Confluence page and comment writes intentionally accept plain text rather than arbitrary storage-format HTML.
  Attachment uploads are base64-only, capped at `MCP_MAX_ATTACHMENT_BYTES`, and cannot read arbitrary server paths.
- When using `HTTPS_PROXY`, the proxy is a trusted egress boundary and resolves the service hostname; destination-IP pinning
  cannot be enforced through that proxy by this process.
- Data Center operations are tested against a fake Jira following REST API v2 and Agile 1.0 formats. Try it
  on a test project first; field formats can differ between Jira versions and plugins.
- `create_issue` uses the createmeta API (Jira 8.4+ endpoints, with a fallback for older versions).
