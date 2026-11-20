"""A minimal async client for the Jira Data Center / Server REST API (v2) and the Agile API (1.0)."""

import asyncio
from typing import Any, Callable, Dict, Optional

import httpx

from secure_jira_mcp.errors import AuthError, JiraError, ValidationError

MAX_RETRIES_ON_429 = 2
MAX_RETRY_WAIT_SECONDS = 10.0
ALLOWED_PATH_PREFIXES = ("/rest/api/2/", "/rest/agile/1.0/")


class JiraClient:
    """One user's connection to one Jira site. The token is only ever sent to that site."""

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = 30.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=timeout,
            # A redirect usually means a login page: the token was not accepted. Never follow it.
            follow_redirects=False,
            transport=transport,
        )

    async def __aenter__(self) -> "JiraClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self._client.aclose()

    async def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        return await self._request("GET", path, params=params)

    async def post(self, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
        return await self._request("POST", path, json=body)

    async def put(self, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
        return await self._request("PUT", path, json=body)

    async def delete(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        return await self._request("DELETE", path, params=params)

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        if not path.startswith(ALLOWED_PATH_PREFIXES):
            raise ValidationError(f"Path not allowed: {path}")
        for attempt in range(MAX_RETRIES_ON_429 + 1):
            try:
                resp = await self._client.request(method, path, **kwargs)
            except httpx.TimeoutException as e:
                raise JiraError("Jira did not answer in time. Try again or narrow the request.") from e
            except httpx.HTTPError as e:
                raise JiraError(f"Could not reach Jira ({type(e).__name__}).") from e
            if resp.status_code == 429 and attempt < MAX_RETRIES_ON_429:
                await asyncio.sleep(_retry_after(resp))
                continue
            break
        _raise_for_status(resp)
        if resp.status_code == 204 or not resp.content:
            return None
        try:
            return resp.json()
        except ValueError as e:
            raise JiraError("Jira returned a response that is not JSON.") from e

    async def download(self, url: str, max_bytes: int, accept: Callable[[str], bool]) -> bytes:
        """Downloads an attachment from this site only, refusing content types `accept` rejects or that are too large."""
        if not url.startswith(self.base_url + "/"):
            raise ValidationError("The attachment is not stored on this Jira site.")
        try:
            async with self._client.stream("GET", url) as resp:
                _raise_for_status(resp)
                content_type = resp.headers.get("content-type", "")
                if not accept(content_type):
                    raise ValidationError(f"This attachment type cannot be read as text ({content_type or 'unknown'}).")
                chunks = []
                size = 0
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValidationError(f"The attachment is larger than the {max_bytes:,} byte limit.")
                    chunks.append(chunk)
                return b"".join(chunks)
        except httpx.HTTPError as e:
            raise JiraError(f"Could not download the attachment ({type(e).__name__}).") from e


def _retry_after(resp: httpx.Response) -> float:
    try:
        return min(float(resp.headers.get("Retry-After", "2")), MAX_RETRY_WAIT_SECONDS)
    except ValueError:
        return 2.0


def _jira_messages(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    messages = [str(m) for m in body.get("errorMessages") or []]
    messages += [f"{field}: {message}" for field, message in (body.get("errors") or {}).items()]
    return "; ".join(messages)


def _raise_for_status(resp: httpx.Response) -> None:
    status = resp.status_code
    if status < 300:
        return
    if status in (301, 302, 303, 307, 308, 401):
        raise AuthError(
            "Jira rejected the personal access token (it is wrong, expired, or revoked). "
            "Create a new one in Jira: your profile > Personal Access Tokens."
        )
    detail = _jira_messages(resp)
    if status == 403:
        raise JiraError(f"You do not have permission for this in Jira. {detail}".strip())
    if status == 404:
        raise JiraError(f"Not found in Jira. {detail}".strip())
    if status == 400:
        raise JiraError(f"Jira refused the request: {detail or 'invalid input'}")
    if status == 429:
        raise JiraError("Jira is rate limiting requests. Wait a minute and try again.")
    raise JiraError(f"Jira returned HTTP {status}. {detail}".strip())
