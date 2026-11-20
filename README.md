# Atlassian MCP for Copilot

A policy-controlled **MCP server for Jira and Confluence Cloud and Data Center / Server**. Each product has
its own site URL and credentials. Jira Cloud supports standard and scoped API tokens with account-email
authentication and REST API v3; Confluence Cloud supports standard and scoped API tokens with
account-email authentication and REST API v1. OAuth is not implemented.

## Atlassian MCP for Copilot and mcp-atlassian

This is a capability and control comparison, not a claim that either project is universally more secure.
mcp-atlassian's tool count and defaults below refer to its current public documentation/source; installed
versions may differ.

| | mcp-atlassian | Atlassian MCP for Copilot |
|---|---|---|
| Product/tool breadth | 98 documented Jira and Confluence tools; Jira and Confluence Cloud and Server/Data Center | Core Jira operations plus Confluence search/read, pages, comments, and bounded attachment upload/download |
| Tool selection | `TOOLSETS` and `ENABLED_TOOLS`; current source filters both listing and direct tool calls. Unset `TOOLSETS` currently enables all toolsets. | Admin-selected `MCP_TOOL_GROUPS`; operations are listed on demand and checked when called |
| Write safety | `READ_ONLY_MODE` can block writes. Reviewed write guard checks read-only mode; it is not an interactive approval step. | Read-only by default; in `elicit` mode, the server shows a change preview and requires user approval. Explicit `client` mode trusts the calling app instead. |
| Authentication | Cloud API token and OAuth; Server/DC PAT and mTLS; supports per-request credentials in HTTP mode | Cloud API token and Server/DC PAT; OAuth and mTLS are not implemented |
| URL/SSRF controls | URL validation and DNS-pinned outbound connections; request-supplied domains can be restricted with `MCP_ALLOWED_URL_DOMAINS` | HTTPS-origin allowlist (including port), strict API paths, same-origin attachment fetch, redirects refused, DNS resolve/validate/connect pinning; private IPs are accepted only for the exact configured Jira or Confluence hostname, while loopback, link-local, and metadata IPs stay blocked |
| Remote transport | HTTP listener can use TLS; deployments must configure their transport and host protections | Remote binds require built-in TLS and `MCP_HTTP_ALLOWED_HOSTS`; optional mTLS requires a trusted client CA |
| Attachments | Broader Jira/Confluence attachment tools, including uploads; remote file paths are confined to a configured directory | Jira text attachment reads; Confluence uploads/downloads are base64-only, same-site, size-limited; no arbitrary server file paths |
| Change audit | The reviewed docs/source do not establish an equivalent per-change audit record | JSONL metadata for each change attempt; no argument values or provider error text |

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

When a Confluence URL is configured, the server also exposes `search_confluence_pages`,
`get_confluence_page`, `list_confluence_spaces`, `list_page_comments`, `list_page_attachments`, and
`download_confluence_attachment`. `list_confluence_operations` describes the approval-gated changes run through
`run_confluence_change`: create/update pages, add/update comments, and upload or replace attachments.
Confluence page/comment bodies are accepted as plain text and escaped into storage format; attachment bytes are
passed as base64 and capped by `MCP_MAX_ATTACHMENT_BYTES`.

The MCP server instructions ask compatible clients to call Jira and Confluence tools directly rather than search
source code or delegate to subagents. For a live connection check, call `list_projects` for Jira or
`list_confluence_spaces` for Confluence. The client ultimately controls tool selection and delegation.

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
$env:MCP_HTTP_ALLOWED_HOSTS = "mcp.company.com:9200"
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
      "command": "C:\\Agentic AI\\secure-jira-mcp\\.venv\\Scripts\\secure-jira-mcp.exe",
      "args": ["--transport", "stdio", "--env-file", "C:\\Agentic AI\\secure-jira-mcp\\.env"]
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
.venv\Scripts\python examples\try_it.py search "project = GHP AND issuetype = Story ORDER BY updated DESC"
.venv\Scripts\python examples\try_it.py issue GHP-3213
.venv\Scripts\python examples\try_it.py ops agile
.venv\Scripts\python examples\try_it.py read list_boards "{\"project_key\": \"GHP\"}"
.venv\Scripts\python examples\try_it.py comment GHP-1234 "Test comment"
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
{"time": "...", "user": "<hash of site+token>", "site": "https://jira.company.com", "operation": "add_comment",
 "target": "GHP-3213", "outcome": "done|declined|failed", "argument_count": 2}
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
