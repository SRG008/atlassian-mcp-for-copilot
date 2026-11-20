"""A minimal async Jira client for Cloud REST API v3 and Data Center / Server REST API v2."""

import asyncio
import urllib.parse
from typing import Any, Callable, Dict, Iterator, Optional
from uuid import UUID

import httpx

from secure_jira_mcp.errors import AuthError, JiraError, ValidationError
from secure_jira_mcp.network import AllowedNetwork, PinnedAsyncHTTPTransport, connection_error_hint

MAX_RETRIES_ON_429 = 2
MAX_RETRY_WAIT_SECONDS = 10.0
MAX_RESPONSE_BYTES = 20_000_000
ALLOWED_PATH_PREFIXES = ("/rest/api/2/", "/rest/api/3/", "/rest/agile/1.0/")


class _NoAuth(httpx.Auth):
    def auth_flow(self, request: httpx.Request) -> Iterator[httpx.Request]:
        yield request


class JiraClient:
    """One user's connection to one Jira site."""

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = 30.0,
        deployment: str = "data_center",
        username: str = "",
        api_token_scoped: bool = False,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        allowed_networks: Optional[list[AllowedNetwork]] = None,
        proxy: Optional[str] = None,
        trust_proxy: bool = False,
        use_system_certificates: bool = True,
    ) -> None:
        if deployment not in ("cloud", "data_center"):
            raise ValidationError("Jira deployment must be 'cloud' or 'data_center'.")
        if api_token_scoped and deployment != "cloud":
            raise ValidationError("Scoped API tokens are supported only for Jira Cloud.")
        parsed_base = urllib.parse.urlsplit(base_url)
        try:
            parsed_base.port
        except ValueError as e:
            raise ValidationError("Jira base URL contains an invalid port.") from e
        if (
            parsed_base.scheme != "https"
            or not parsed_base.hostname
            or base_url != base_url.strip()
            or len(base_url) > 2048
            or any(ord(char) < 32 for char in base_url)
            or parsed_base.username is not None
            or parsed_base.password is not None
            or parsed_base.query
            or parsed_base.fragment
            or "\\" in parsed_base.path
            or "%" in parsed_base.path
            or "//" in parsed_base.path
            or any(segment in (".", "..") for segment in parsed_base.path.split("/"))
        ):
            raise ValidationError("Jira base URL must be a valid HTTPS URL without credentials, query, or fragment.")
        if (
            not token or len(token) > 4096
            or any(char.isspace() or ord(char) < 32 for char in token)
        ):
            raise ValidationError("A non-empty Jira credential without whitespace is required.")
        if proxy and not trust_proxy:
            raise ValidationError(
                "HTTPS proxying moves Jira DNS resolution outside this server. "
                "Set JIRA_TRUST_HTTPS_PROXY=true only for a trusted, access-controlled proxy."
            )
        self.base_url = base_url.rstrip("/")
        self.deployment = deployment
        self.api_token_scoped = api_token_scoped
        self._cloud_id: Optional[str] = None
        self._next_page_tokens: Dict[tuple[str, int], str] = {}
        headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
        auth: Optional[httpx.Auth] = None
        if deployment == "cloud":
            if (
                not username or len(username) > 320 or ":" in username
                or any(char.isspace() or ord(char) < 32 for char in username)
            ):
                raise ValidationError("Jira Cloud requires a Jira account email and API token.")
            auth = httpx.BasicAuth(username, token)
        else:
            headers["Authorization"] = f"Bearer {token}"
        if transport is None:
            transport = PinnedAsyncHTTPTransport(
                allowed_networks or [],
                proxy=proxy,
                use_system_certificates=use_system_certificates,
                trusted_private_hosts=[parsed_base.hostname] if parsed_base.hostname else [],
            )
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            auth=auth,
            timeout=timeout,
            # A redirect usually means a login page: the token was not accepted. Never follow it.
            follow_redirects=False,
            transport=transport,
            trust_env=False,
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

    async def search_page(
        self,
        jql: str,
        start: int,
        limit: int,
        fields: list[str],
    ) -> Any:
        if self.deployment != "cloud":
            return await self.post(
                "/rest/api/2/search",
                {"jql": jql, "startAt": start, "maxResults": limit, "fields": fields},
            )

        body: Dict[str, Any] = {"jql": jql, "maxResults": limit, "fields": fields}
        page_token = self._next_page_tokens.get((jql, start))
        if page_token:
            body["nextPageToken"] = page_token
        page = await self.post("/rest/api/3/search/jql", body) or {}
        next_page_token = page.get("nextPageToken")
        if next_page_token:
            self._next_page_tokens[(jql, start + len(page.get("issues") or []))] = str(next_page_token)
        elif page.get("isLast") is not True:
            raise JiraError("Jira Cloud search did not return a continuation token.")
        return page

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        parsed_path = urllib.parse.urlsplit(path)
        if (
            parsed_path.scheme or parsed_path.netloc or parsed_path.query or parsed_path.fragment
            or "?" in path or "#" in path or "\\" in path or "%" in path
            or any(ord(char) < 32 for char in path)
            or not parsed_path.path.startswith("/")
            or parsed_path.path.startswith("//")
            or any(segment in (".", "..") for segment in parsed_path.path.split("/"))
            or not parsed_path.path.startswith(ALLOWED_PATH_PREFIXES)
        ):
            raise ValidationError(f"Path not allowed: {path}")
        path = parsed_path.path
        if self.deployment == "cloud" and path.startswith("/rest/api/2/"):
            path = path.replace("/rest/api/2/", "/rest/api/3/", 1)

        if self.deployment == "cloud" and isinstance(kwargs.get("json"), dict):
            body = kwargs["json"]
            if path.endswith("/comment") and isinstance(body.get("body"), str):
                kwargs["json"] = {**body, "body": _to_adf(body["body"])}
            elif "/issue" in path:
                fields = body.get("fields")
                if isinstance(fields, dict):
                    converted_fields = {
                        key: _to_adf(value) if key in ("description", "environment") and isinstance(value, str) else value
                        for key, value in fields.items()
                    }
                    kwargs["json"] = {**body, "fields": converted_fields}
            if path.endswith("/transitions"):
                kwargs["json"] = _to_adf_transition_comments(kwargs["json"])
        if self.deployment == "cloud" and path.endswith("/search"):
            path = path[:-len("search")] + "search/jql"
        elif self.deployment == "cloud" and path.endswith("/user/search"):
            path = path[:-len("user/search")] + "user/search"
            params = kwargs.get("params")
            if isinstance(params, dict) and "username" in params:
                kwargs["params"] = {**{key: value for key, value in params.items() if key != "username"},
                                    "query": params["username"]}

        request_url = await self._request_url(path)
        for attempt in range(MAX_RETRIES_ON_429 + 1):
            retry_wait: Optional[float] = None
            try:
                async with self._client.stream(method, request_url, **kwargs) as stream:
                    if stream.status_code == 429 and attempt < MAX_RETRIES_ON_429:
                        retry_wait = _retry_after(stream)
                    else:
                        content_length = stream.headers.get("Content-Length")
                        if content_length:
                            try:
                                if int(content_length) > MAX_RESPONSE_BYTES:
                                    raise JiraError("Jira response exceeds the 20 MB safety limit.")
                            except ValueError as e:
                                raise JiraError("Jira returned an invalid response length.") from e
                        content = bytearray()
                        async for chunk in stream.aiter_bytes(64 * 1024):
                            content.extend(chunk)
                            if len(content) > MAX_RESPONSE_BYTES:
                                raise JiraError("Jira response exceeds the 20 MB safety limit.")
                        resp = httpx.Response(
                            stream.status_code,
                            headers=stream.headers,
                            content=bytes(content),
                            request=stream.request,
                            extensions=stream.extensions,
                        )
            except httpx.TimeoutException as e:
                raise JiraError("Jira did not answer in time. Try again or narrow the request.") from e
            except httpx.ConnectError as e:
                raise JiraError(connection_error_hint(e, "Jira", "JIRA_ALLOWED_NETWORKS")) from e
            except httpx.HTTPError as e:
                raise JiraError(f"Could not reach Jira ({type(e).__name__}).") from e
            if retry_wait is not None:
                await asyncio.sleep(retry_wait)
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
        target = urllib.parse.urlsplit(url)
        base = urllib.parse.urlsplit(self.base_url)
        try:
            target_port = (target.port or 443) if target.scheme == "https" else target.port
            base_port = (base.port or 443) if base.scheme == "https" else base.port
            same_origin = (
                target.scheme == "https"
                and target.hostname == base.hostname
                and target_port == base_port
                and target.username is None
                and target.password is None
            )
        except ValueError:
            same_origin = False
        base_path = base.path.rstrip("/")
        if (
            not same_origin
            or target.fragment
            or (base_path and not target.path.startswith(base_path + "/"))
        ):
            raise ValidationError("The attachment is not stored on this Jira site.")
        try:
            if self.api_token_scoped:
                relative_path = target.path[len(base_path):] if base_path else target.path
                parts = relative_path.split("/")
                if len(parts) < 4 or parts[1] != "secure" or parts[2] != "attachment" or not parts[3].isdigit():
                    raise ValidationError("This scoped Jira Cloud attachment URL is not supported.")
                request_url = await self._scoped_url(f"/rest/api/3/attachment/content/{parts[3]}")
            else:
                request_url = url
            async with self._client.stream("GET", request_url) as resp:
                return await self._read_attachment(resp, max_bytes, accept)
        except httpx.HTTPError as e:
            raise JiraError(f"Could not download the attachment ({type(e).__name__}).") from e

    async def _request_url(self, path: str) -> str:
        if not self.api_token_scoped:
            return path
        return await self._scoped_url(path)

    async def _scoped_url(self, path: str, query: str = "") -> str:
        if self._cloud_id is None:
            try:
                response = await self._client.get("/_edge/tenant_info", auth=_NoAuth())
            except httpx.TimeoutException as e:
                raise JiraError("Jira Cloud site ID lookup timed out.") from e
            except httpx.HTTPError as e:
                raise JiraError(f"Could not resolve the Jira Cloud site ID ({type(e).__name__}).") from e
            if response.status_code != 200:
                raise JiraError(
                    f"Could not resolve the Jira Cloud site ID (HTTP {response.status_code}). "
                    "Check that the Jira Cloud URL is correct."
                )
            try:
                cloud_id = UUID(response.json()["cloudId"])
            except (ValueError, KeyError, TypeError) as e:
                raise JiraError("Jira Cloud did not return a valid site ID.") from e
            self._cloud_id = str(cloud_id)
        url = f"https://api.atlassian.com/ex/jira/{self._cloud_id}{path}"
        if query:
            url = f"{url}?{query}"
        return url

    @staticmethod
    async def _read_attachment(
        response: httpx.Response,
        max_bytes: int,
        accept: Callable[[str], bool],
    ) -> bytes:
        _raise_for_status(response)
        content_type = response.headers.get("content-type", "")
        if not accept(content_type):
            raise ValidationError(f"This attachment type cannot be read as text ({content_type or 'unknown'}).")
        chunks = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > max_bytes:
                raise ValidationError(f"The attachment is larger than the {max_bytes:,} byte limit.")
            chunks.append(chunk)
        return b"".join(chunks)


def _to_adf(text: str) -> Dict[str, Any]:
    paragraphs = []
    for line in text.splitlines() or [""]:
        content = [{"type": "text", "text": line}] if line else []
        paragraphs.append({"type": "paragraph", "content": content})
    return {"type": "doc", "version": 1, "content": paragraphs}


def _to_adf_transition_comments(body: Dict[str, Any]) -> Dict[str, Any]:
    update = body.get("update")
    comments = update.get("comment") if isinstance(update, dict) else None
    if not isinstance(comments, list):
        return body
    converted = []
    for comment in comments:
        if not isinstance(comment, dict):
            converted.append(comment)
            continue
        converted.append({
            key: {**action, "body": _to_adf(action["body"])}
            if isinstance(action, dict) and isinstance(action.get("body"), str) else action
            for key, action in comment.items()
        })
    return {**body, "update": {**update, "comment": converted}}


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
        if "/rest/api/3/" in resp.request.url.path:
            raise AuthError(
                "Jira Cloud rejected authentication. Verify that the Atlassian account email and API token "
                "belong together, the token is valid and the correct type (standard or scoped) was selected, "
                "and the URL is the Jira site for that account."
            )
        raise AuthError("Jira rejected the personal access token (it is wrong, expired, or revoked). Create a new one in Jira.")
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
