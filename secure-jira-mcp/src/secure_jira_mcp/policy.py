"""The administrator's policy, the per-user rate limit, and the audit log."""

import hashlib
import json
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from secure_jira_mcp.config import GROUP_DESCRIPTIONS, Settings
from secure_jira_mcp.errors import PolicyError
from secure_jira_mcp.operations import REGISTRY, Operation


def check_allowed(op: Operation, settings: Settings) -> None:
    if op.group not in settings.groups():
        raise PolicyError(f"'{op.name}' belongs to the '{op.group}' group, which the administrator has turned off.")
    if op.kind != "read" and settings.mcp_read_only:
        raise PolicyError("This server is read-only; changes are turned off by the administrator.")


def is_allowed(op: Operation, settings: Settings) -> bool:
    try:
        check_allowed(op, settings)
        return True
    except PolicyError:
        return False


def describe_groups(settings: Settings) -> List[Dict[str, Any]]:
    enabled = settings.groups()
    out = []
    for group, description in GROUP_DESCRIPTIONS.items():
        ops = [op for op in REGISTRY.values() if op.group == group]
        allowed = [op.name for op in ops if is_allowed(op, settings)]
        out.append(
            {
                "group": group,
                "description": description,
                "enabled": group in enabled,
                "operations": allowed,
                **({"note": "changes are off (read-only server)"} if group in enabled and len(allowed) < len(ops) else {}),
            }
        )
    return out


def describe_operations(group: str, settings: Settings) -> List[Dict[str, Any]]:
    if group not in GROUP_DESCRIPTIONS:
        raise PolicyError(f"Unknown group '{group}'. Groups: {', '.join(GROUP_DESCRIPTIONS)}.")
    if group not in settings.groups():
        raise PolicyError(f"The '{group}' group is turned off by the administrator.")
    return [
        {
            "operation": op.name,
            "kind": op.kind,
            "run_with": "run_read" if op.kind == "read" else "run_change",
            "description": op.description,
            "arguments": op.args.model_json_schema(),
        }
        for op in REGISTRY.values()
        if op.group == group and is_allowed(op, settings)
    ]


class RateLimiter:
    """At most `per_minute` tool calls per user in any 60 seconds."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._calls: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, principal: str) -> None:
        now = time.monotonic()
        with self._lock:
            calls = self._calls[principal]
            while calls and now - calls[0] > 60:
                calls.popleft()
            if len(calls) >= self.per_minute:
                raise PolicyError("Too many requests in the last minute. Wait a moment and try again.")
            calls.append(now)


class AuditLog:
    """One JSON line per change attempt. Never contains tokens or the changed text, only a hash of the arguments."""

    def __init__(self, path: str) -> None:
        self.path = Path(path) if path else None
        self._lock = threading.Lock()

    def record(
        self,
        principal: str,
        site: str,
        operation: str,
        target: str,
        outcome: str,
        arguments: Optional[Dict[str, Any]] = None,
        error: str = "",
    ) -> None:
        if self.path is None:
            return
        entry = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "user": principal,
            "site": site,
            "operation": operation,
            "target": target,
            "outcome": outcome,
            "arguments_sha256": hashlib.sha256(json.dumps(arguments or {}, sort_keys=True, default=str).encode()).hexdigest(),
            **({"error": error[:300]} if error else {}),
        }
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
