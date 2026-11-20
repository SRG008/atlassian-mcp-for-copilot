"""Network controls for outbound Jira HTTPS connections."""

import asyncio
import ipaddress
import socket
import ssl
from typing import List, Optional, Union

import httpcore
import httpx
from httpcore._backends.auto import AutoBackend

AllowedNetwork = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]


def connection_error_hint(
    error: BaseException,
    service: str,
    allowed_networks_setting: str,
) -> str:
    messages = []
    seen = set()
    current: Optional[BaseException] = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        message = str(current)
        if message:
            messages.append(message)
        current = current.__cause__ or current.__context__
    details = "\n".join(messages)
    if "non-global IP outside" in details and allowed_networks_setting in details:
        return (
            f"{service} resolves to a non-public IP that is not trusted. Private IPs are allowed "
            "for the exact hostname in the configured service URL. Check the URL, DNS, and VPN; "
            "loopback, link-local, and cloud-metadata addresses remain blocked."
        )
    if "Could not resolve the" in details and "hostname" in details:
        return f"Could not resolve the {service} hostname. Check the URL, DNS, and VPN connection."
    if "CERTIFICATE_VERIFY_FAILED" in details:
        return (
            f"The TLS certificate for {service} could not be verified. "
            "Check the corporate certificate store or trusted HTTPS proxy."
        )
    return (
        f"Could not reach {service} ({type(error).__name__}). "
        "Check the URL, VPN/firewall, and TLS certificate."
    )


class PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Resolve and validate addresses, then connect to those exact IPs."""

    def __init__(
        self,
        allowed_networks: List[AllowedNetwork],
        allow_non_global: bool = False,
        delegate: Optional[httpcore.AsyncNetworkBackend] = None,
        service: str = "Jira",
        allowed_networks_setting: str = "JIRA_ALLOWED_NETWORKS",
        trusted_private_hosts: Optional[List[str]] = None,
    ) -> None:
        self._allowed_networks = allowed_networks
        self._allow_non_global = allow_non_global
        self._delegate = delegate or AutoBackend()
        self._service = service
        self._allowed_networks_setting = allowed_networks_setting
        self._trusted_private_hosts = {
            host.lower().rstrip(".") for host in (trusted_private_hosts or [])
        }

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: Optional[float] = None,
        local_address: Optional[str] = None,
        socket_options=None,
    ) -> httpcore.AsyncNetworkStream:
        addresses = await self._resolve(host, port)
        for address in addresses:
            if not self._allow_non_global and not self._is_allowed(address, host):
                raise httpcore.ConnectError(
                    f"{self._service} resolved to a non-global IP outside {self._allowed_networks_setting}."
                )

        last_error: Optional[httpcore.NetworkError] = None
        for address in addresses:
            try:
                return await self._delegate.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except httpcore.NetworkError as e:
                last_error = e
        if last_error:
            raise last_error
        raise httpcore.ConnectError(f"{self._service} hostname did not resolve to a usable IP address.")

    async def connect_unix_socket(
        self,
        path: str,
        timeout: Optional[float] = None,
        socket_options=None,
    ) -> httpcore.AsyncNetworkStream:
        return await self._delegate.connect_unix_socket(
            path,
            timeout=timeout,
            socket_options=socket_options,
        )

    async def sleep(self, seconds: float) -> None:
        await self._delegate.sleep(seconds)

    async def _resolve(self, host: str, port: int) -> List[str]:
        try:
            address = ipaddress.ip_address(host)
            if isinstance(address, ipaddress.IPv6Address) and address.scope_id is not None:
                raise httpcore.ConnectError(f"Scoped IPv6 addresses are not allowed for {self._service}.")
            return [address.compressed]
        except ValueError:
            pass

        try:
            results = await asyncio.get_running_loop().getaddrinfo(
                host,
                port,
                type=socket.SOCK_STREAM,
            )
        except OSError as e:
            raise httpcore.ConnectError(f"Could not resolve the {self._service} hostname.") from e

        addresses = []
        seen = set()
        for result in results:
            candidate = result[4][0]
            try:
                address = ipaddress.ip_address(candidate)
            except ValueError as e:
                raise httpcore.ConnectError("DNS returned an invalid IP address.") from e
            if isinstance(address, ipaddress.IPv6Address) and address.scope_id is not None:
                raise httpcore.ConnectError(f"Scoped IPv6 addresses are not allowed for {self._service}.")
            if candidate not in seen:
                seen.add(candidate)
                addresses.append(candidate)
        return addresses

    def _is_allowed(self, address: str, host: str = "") -> bool:
        parsed = ipaddress.ip_address(address)
        if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped:
            parsed = parsed.ipv4_mapped
        if parsed.is_global or any(
            network.version == parsed.version and parsed in network
            for network in self._allowed_networks
        ):
            return True
        if host.lower().rstrip(".") not in self._trusted_private_hosts:
            return False
        if parsed in {
            ipaddress.ip_address("100.100.100.200"),
            ipaddress.ip_address("169.254.169.254"),
            ipaddress.ip_address("fd00:ec2::254"),
        }:
            return False
        return (
            parsed.is_private
            and not parsed.is_loopback
            and not parsed.is_link_local
            and not parsed.is_unspecified
            and not parsed.is_multicast
        )


class PinnedAsyncHTTPTransport(httpx.AsyncHTTPTransport):
    """HTTPX transport whose TCP connections use validated, pinned DNS results."""

    def __init__(
        self,
        allowed_networks: List[AllowedNetwork],
        proxy: Optional[str] = None,
        use_system_certificates: bool = True,
        service: str = "Jira",
        allowed_networks_setting: str = "JIRA_ALLOWED_NETWORKS",
        trusted_private_hosts: Optional[List[str]] = None,
    ) -> None:
        verify = ssl.create_default_context() if use_system_certificates else True
        super().__init__(verify=verify, trust_env=False, proxy=proxy)
        backend = PinnedNetworkBackend(
            allowed_networks,
            allow_non_global=proxy is not None,
            service=service,
            allowed_networks_setting=allowed_networks_setting,
            trusted_private_hosts=trusted_private_hosts,
        )
        # HTTPX currently does not expose httpcore's network_backend option.
        pool = self._pool
        if not hasattr(pool, "_network_backend"):
            raise RuntimeError("The installed HTTPX transport does not support DNS pinning.")
        pool._network_backend = backend
