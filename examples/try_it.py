"""A terminal client for trying the server against your Jira, without an AI client.

Usage (from the project folder, with .env filled in):
  .venv\\Scripts\\python examples\\try_it.py projects
  .venv\\Scripts\\python examples\\try_it.py search "project = GHP AND issuetype = Story ORDER BY updated DESC"
  .venv\\Scripts\\python examples\\try_it.py issue GHP-3213
  .venv\\Scripts\\python examples\\try_it.py groups
  .venv\\Scripts\\python examples\\try_it.py ops agile
  .venv\\Scripts\\python examples\\try_it.py read list_boards "{\\"project_key\\": \\"GHP\\"}"
  .venv\\Scripts\\python examples\\try_it.py comment GHP-1234 "Test comment from secure-jira-mcp"

Every change shows the server's approval prompt here; type y to approve.
"""

import asyncio
import json
import sys
from pathlib import Path

from mcp import Client, StdioServerParameters, types

PROJECT = Path(__file__).resolve().parent.parent


async def approve_in_terminal(_context, params) -> types.ElicitResult:
    print("\n" + "=" * 70 + f"\n{params.message}\n" + "=" * 70)
    answer = input("Approve? (y/n): ").strip().lower()
    if answer == "y":
        return types.ElicitResult(action="accept", content={"approve": True})
    return types.ElicitResult(action="decline")


def show(result: types.CallToolResult) -> None:
    if result.is_error:
        print("ERROR:", result.content[0].text if result.content else result)
        return
    data = result.structured_content or {}
    print(json.dumps(data.get("result", data), indent=2, ensure_ascii=False))


async def main(args: list) -> None:
    if not args:
        print(__doc__)
        return
    command, rest = args[0], args[1:]
    calls = {
        "projects": lambda: ("list_projects", {}),
        "search": lambda: ("search_issues", {"jql": rest[0]}),
        "issue": lambda: ("get_issue", {"issue_key": rest[0]}),
        "groups": lambda: ("list_operations", {}),
        "ops": lambda: ("list_operations", {"group": rest[0]}),
        "read": lambda: ("run_read", {"operation": rest[0], "arguments": json.loads(rest[1]) if len(rest) > 1 else {}}),
        "change": lambda: ("run_change", {"operation": rest[0], "arguments": json.loads(rest[1])}),
        "comment": lambda: ("run_change", {"operation": "add_comment", "arguments": {"issue_key": rest[0], "body": rest[1]}}),
    }
    if command not in calls:
        print(__doc__)
        return
    tool, arguments = calls[command]()
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "secure_jira_mcp", "--transport", "stdio", "--env-file", str(PROJECT / ".env")],
        cwd=str(PROJECT),
    )
    async with Client(server, elicitation_callback=approve_in_terminal) as client:
        show(await client.call_tool(tool, arguments))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
