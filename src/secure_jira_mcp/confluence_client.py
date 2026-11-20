"""A bounded async Confluence REST client for Cloud and Data Center / Server."""

import urllib.parse
from typing import Any, Dict, Iterator, Optional
from uuid import UUID

import httpx

from secure_jira_mcp.errors import AuthError, JiraError, ValidationError
from secure_jira_mcp.network import AllowedNetwork, PinnedAsyncHTTPTransport, connection_error_hint

MAX_RESPONSE_BYTES = 20_000_000
ALLOWED_PATH_PREFIX = "/rest/api/"


class _NoAuth(httpx.Auth):
    def auth_flow(self, request: httpx.Request) -> Iterator[httpx.Request]:
        yield request


class ConfluenceClient:
    """One user's connection to one Confluence site."""

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = 30.0,
        deployment: str = "data_center",
        username: str = "",
        transport: Optional[httpx.AsyncBaseTransport] = None,
        allowed_networks: Optional[list[AllowedNetwork]] = None,
        proxy: Optional[str] = None,
        trust_proxy: bool = False,
        use_system_certificates: bool = True,
        api_token_scoped: bool = False,
    ) -> None:
        if deployment not in ("cloud", "data_center"):
            raise ValidationError("Confluence deployment must be 'cloud' or 'data_center'.")
        if api_token_scoped and deployment != "cloud":
            raise ValidationError("Scoped API tokens are supported only for Confluence Cloud.")
        parsed_base = urllib.parse.urlsplit(base_url)
        try:
            port = parsed_base.port
        except ValueError as e:
            raise ValidationError("Confluence base URL contains an invalid port.") from e
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
            or port == 0
        ):
            raise ValidationError("Confluence base URL must be a valid HTTPS URL without credentials, query, or fragment.")
        if not token or len(token) > 4096 or any(char.isspace() or ord(char) < 32 for char in token):
            raise ValidationError("A non-empty Confluence credential without whitespace is required.")
        if proxy and not trust_proxy:
            raise ValidationError(
                "HTTPS proxying moves Confluence DNS resolution outside this server. "
                "Set CONFLUENCE_TRUST_HTTPS_PROXY=true only for a trusted, access-controlled proxy."
            )

        self.base_url = base_url.rstrip("/")
        self.deployment = deployment
        self.api_token_scoped = api_token_scoped
        self._cloud_id: Optional[str] = None
        headers = {"Accept": "application/json"}
        auth: Optional[httpx.Auth] = None
        if deployment == "cloud":
            if (
                not username
                or len(username) > 320
                or ":" in username
                or any(char.isspace() or ord(char) < 32 for char in username)
            ):
                raise ValidationError("Confluence Cloud requires an account email and API token.")
            auth = httpx.BasicAuth(username, token)
        else:
            headers["Authorization"] = f"Bearer {token}"

        if transport is None:
            transport = PinnedAsyncHTTPTransport(
                allowed_networks or [],
                proxy=proxy,
                use_system_certificates=use_system_certificates,
                service="Confluence",
                allowed_networks_setting="CONFLUENCE_ALLOWED_NETWORKS",
                trusted_private_hosts=[parsed_base.hostname] if parsed_base.hostname else [],
            )
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            auth=auth,
            timeout=timeout,
            follow_redirects=False,
            transport=transport,
            trust_env=False,
        )

    async def __aenter__(self) -> "ConfluenceClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self._client.aclose()

    async def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        return await self._request("GET", path, params=params)

    async def post(self, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
        return await self._request("POST", path, json_body=body)

    async def put(self, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
        return await self._request("PUT", path, json_body=body)

    async def upload_attachment(
        self,
        path: str,
        filename: str,
        content: bytes,
        content_type: str,
    ) -> Any:
        return await self._request(
            "POST",
            path,
            files={"file": (filename, content, content_type)},
            headers={"X-Atlassian-Token": "no-check"},
        )

    async def download_attachment(self, url: str, max_bytes: int) -> tuple[bytes, str]:
        link = urllib.parse.urlsplit(url)
        base = urllib.parse.urlsplit(self.base_url)
        if (
            not link.scheme
            and not link.netloc
            and base.path
            and link.path.startswith("/download/attachments/")
        ):
            target_url = urllib.parse.urlunsplit((
                base.scheme,
                base.netloc,
                base.path.rstrip("/") + link.path,
                link.query,
                link.fragment,
            ))
        else:
            target_url = urllib.parse.urljoin(self.base_url + "/", url)
        target = urllib.parse.urlsplit(target_url)
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
        path_prefix = base.path.rstrip("/")
        decoded_path = urllib.parse.unquote(target.path)
        allowed_download_path = (
            decoded_path.startswith("/download/attachments/")
            or (path_prefix and decoded_path.startswith(path_prefix + "/download/attachments/"))
        )
        if (
            not same_origin
            or target.fragment
            or not allowed_download_path
            or "\\" in decoded_path
            or any(marker in decoded_path.lower() for marker in ("%2e", "%2f", "%5c"))
            or any(ord(char) < 32 or ord(char) == 127 for char in decoded_path)
            or any(segment in (".", "..") for segment in decoded_path.split("/"))
        ):
            raise ValidationError("The attachment is not stored on this Confluence site.")

        try:
            if self.api_token_scoped:
                scoped_path = target.path
                if path_prefix and scoped_path.startswith(path_prefix + "/"):
                    scoped_path = scoped_path[len(path_prefix):]
                request_url = await self._scoped_url(scoped_path, target.query)
            else:
                request_url = target_url
            async with self._client.stream("GET", request_url) as response:
                _raise_for_status(response)
                length = response.headers.get("Content-Length")
                if length:
                    try:
                        if int(length) > max_bytes:
                            raise ValidationError(f"The attachment is larger than the {max_bytes:,} byte limit.")
                    except ValueError as e:
                        raise JiraError("Confluence returned an invalid attachment length.") from e
                chunks = []
                size = 0
                async for chunk in response.aiter_bytes(64 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValidationError(f"The attachment is larger than the {max_bytes:,} byte limit.")
                    chunks.append(chunk)
                return b"".join(chunks), response.headers.get("content-type", "application/octet-stream")
        except httpx.TimeoutException as e:
            raise JiraError("Confluence did not answer in time while downloading the attachment.") from e
        except httpx.ConnectError as e:
            raise JiraError(connection_error_hint(e, "Confluence", "CONFLUENCE_ALLOWED_NETWORKS")) from e
        except httpx.HTTPError as e:
            raise JiraError(f"Could not download the Confluence attachment ({type(e).__name__}).") from e

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        files: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Any:
        parsed_path = urllib.parse.urlsplit(path)
        if (
            parsed_path.scheme
            or parsed_path.netloc
            or parsed_path.query
            or parsed_path.fragment
            or "?" in path
            or "#" in path
            or "\\" in path
            or "%" in path
            or any(ord(char) < 32 for char in path)
            or not parsed_path.path.startswith(ALLOWED_PATH_PREFIX)
            or parsed_path.path.startswith("//")
            or any(segment in (".", "..") for segment in parsed_path.path.split("/"))
        ):
            raise ValidationError(f"Confluence API path is not allowed: {path}")

        request_url = await self._scoped_url(parsed_path.path) if self.api_token_scoped else parsed_path.path
        try:
            async with self._client.stream(
                method,
                request_url,
                params=params,
                json=json_body,
                files=files,
                headers=headers,
            ) as response:
                _raise_for_status(response)
                content_length = response.headers.get("Content-Length")
                if content_length:
                    try:
                        if int(content_length) > MAX_RESPONSE_BYTES:
                            raise JiraError("Confluence response exceeds the 20 MB safety limit.")
                    except ValueError as e:
                        raise JiraError("Confluence returned an invalid response length.") from e
                content = bytearray()
                async for chunk in response.aiter_bytes(64 * 1024):
                    content.extend(chunk)
                    if len(content) > MAX_RESPONSE_BYTES:
                        raise JiraError("Confluence response exceeds the 20 MB safety limit.")
                if response.status_code == 204 or not content:
                    return None
                try:
                    return httpx.Response(
                        response.status_code,
                        headers=response.headers,
                        content=bytes(content),
                        request=response.request,
                    ).json()
                except ValueError as e:
                    raise JiraError("Confluence returned a response that is not JSON.") from e
        except httpx.TimeoutException as e:
            raise JiraError("Confluence did not answer in time. Try again or narrow the request.") from e
        except httpx.ConnectError as e:
            raise JiraError(connection_error_hint(e, "Confluence", "CONFLUENCE_ALLOWED_NETWORKS")) from e
        except httpx.HTTPError as e:
            raise JiraError(f"Could not reach Confluence ({type(e).__name__}).") from e

    async def _scoped_url(self, path: str, query: str = "") -> str:
        if self._cloud_id is None:
            base = urllib.parse.urlsplit(self.base_url)
            tenant_url = urllib.parse.urlunsplit((base.scheme, base.netloc, "/_edge/tenant_info", "", ""))
            try:
                response = await self._client.get(tenant_url, auth=_NoAuth())
            except httpx.TimeoutException as e:
                raise JiraError("Confluence Cloud site ID lookup timed out.") from e
            except httpx.HTTPError as e:
                raise JiraError(f"Could not resolve the Confluence Cloud site ID ({type(e).__name__}).") from e
            if response.status_code != 200:
                raise JiraError(
                    f"Could not resolve the Confluence Cloud site ID (HTTP {response.status_code}). "
                    "Check that the Confluence Cloud URL is correct."
                )
            try:
                self._cloud_id = str(UUID(response.json()["cloudId"]))
            except (ValueError, KeyError, TypeError) as e:
                raise JiraError("Confluence Cloud did not return a valid site ID.") from e
        url = f"https://api.atlassian.com/ex/confluence/{self._cloud_id}/wiki{path}"
        if query:
            url = f"{url}?{query}"
        return url


def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code < 300:
        return
    if response.status_code in (301, 302, 303, 307, 308, 401):
        raise AuthError(
            "Confluence rejected authentication. Verify the account email and token, select the correct "
            "standard or scoped token type, and include the Confluence scopes required for this request."
        )
    if response.status_code == 403:
        raise JiraError("You do not have permission for this in Confluence.")
    if response.status_code == 404:
        raise JiraError("The Confluence page or attachment was not found or is not visible to this account.")
    if response.status_code == 400:
        raise JiraError("Confluence refused the request because it was invalid.")
    if response.status_code == 429:
        raise JiraError("Confluence is rate limiting requests. Wait a minute and try again.")
    raise JiraError(f"Confluence returned HTTP {response.status_code}.")
