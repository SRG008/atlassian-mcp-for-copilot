"""Command line: secure-jira-mcp [--transport stdio|http] [--host 127.0.0.1] [--port 9200]."""

import argparse
import asyncio
import ipaddress
import logging
import ssl
import sys
from pathlib import Path

from secure_jira_mcp import __version__
from secure_jira_mcp.config import Settings
from secure_jira_mcp.errors import AuthError, SecureJiraError
from secure_jira_mcp.confluence_client import ConfluenceClient
from secure_jira_mcp.jira_client import JiraClient
from secure_jira_mcp.server import build_server, resolve_confluence_session, resolve_session
from mcp.server.transport_security import TransportSecuritySettings

logger = logging.getLogger("secure_jira_mcp")


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


async def _check_connection(settings: Settings) -> None:
    session = resolve_session(None, settings, settings.allowed_hosts())
    async with JiraClient(
        session.base_url,
        session.token,
        min(10, settings.mcp_request_timeout_seconds),
        deployment=session.deployment,
        username=session.username,
        api_token_scoped=session.api_token_scoped,
        allowed_networks=settings.allowed_networks(),
        proxy=settings.https_proxy or None,
        trust_proxy=settings.jira_trust_https_proxy,
        use_system_certificates=settings.use_system_certificates,
    ) as client:
        user = await client.get("/rest/api/2/myself")
    if not isinstance(user, dict) or not (user.get("name") or user.get("key") or user.get("accountId")):
        raise SecureJiraError("The site did not return a Jira user.")


async def _check_confluence_connection(settings: Settings) -> None:
    session = resolve_confluence_session(None, settings, settings.confluence_allowed_hosts_list())
    async with ConfluenceClient(
        session.base_url,
        session.token,
        min(10, settings.mcp_request_timeout_seconds),
        deployment=session.deployment,
        username=session.username,
        api_token_scoped=session.api_token_scoped,
        allowed_networks=settings.confluence_allowed_networks_list(),
        proxy=settings.https_proxy or None,
        trust_proxy=settings.confluence_trust_https_proxy,
        use_system_certificates=settings.use_system_certificates,
    ) as client:
        user = await client.get("/rest/api/user/current")
    if not isinstance(user, dict) or not any(user.get(key) for key in ("username", "userKey", "accountId", "displayName")):
        raise SecureJiraError("The site did not return a Confluence user.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="secure-jira-mcp",
        description="Secure MCP server for Jira and Confluence Cloud and Data Center / Server.",
    )
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio",
                        help="stdio: one configured user. http: credentials are supplied per request.")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP listen address (default 127.0.0.1).")
    parser.add_argument("--port", type=int, default=9200, help="HTTP port (default 9200).")
    parser.add_argument("--allow-remote", action="store_true",
                        help="Allow a non-local listen address; requires the built-in TLS listener.")
    parser.add_argument("--tls-certfile", help="TLS server certificate (PEM). Required for non-local HTTP listeners.")
    parser.add_argument("--tls-keyfile", help="TLS server private key (PEM). Protect this file from other users.")
    parser.add_argument("--mtls-ca-file", help="Require a client certificate signed by this CA (PEM).")
    config = parser.add_mutually_exclusive_group()
    config.add_argument("--env-file", default=".env", help="Settings file (default .env in the current folder).")
    config.add_argument("--no-env-file", action="store_true", help="Read settings only from the environment.")
    checks = parser.add_mutually_exclusive_group()
    checks.add_argument("--check-connection", action="store_true", help="Verify the Jira token without starting MCP.")
    checks.add_argument(
        "--check-confluence-connection",
        action="store_true",
        help="Verify the Confluence credential without starting MCP.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    # stdout carries the MCP protocol in stdio mode, so logs go to stderr.
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    try:
        settings = Settings(_env_file=None if args.no_env_file else args.env_file)
        if settings.use_system_certificates:
            import truststore

            truststore.inject_into_ssl()
        if args.check_connection:
            asyncio.run(_check_connection(settings))
            return 0
        if args.check_confluence_connection:
            asyncio.run(_check_confluence_connection(settings))
            return 0
        tls_requested = bool(args.tls_certfile or args.tls_keyfile or args.mtls_ca_file)
        if args.transport != "http" and tls_requested:
            raise SecureJiraError("TLS listener options are only valid with --transport http.")
        if bool(args.tls_certfile) != bool(args.tls_keyfile):
            raise SecureJiraError("Set both --tls-certfile and --tls-keyfile.")
        if args.mtls_ca_file and not args.tls_certfile:
            raise SecureJiraError("--mtls-ca-file requires --tls-certfile and --tls-keyfile.")
        for label, filename in (
            ("TLS certificate", args.tls_certfile),
            ("TLS private key", args.tls_keyfile),
            ("mTLS CA", args.mtls_ca_file),
        ):
            if filename and not Path(filename).is_file():
                raise SecureJiraError(f"The configured {label} file does not exist or is not a file.")
        if args.transport == "http" and not _is_loopback(args.host):
            if not args.allow_remote:
                raise SecureJiraError(f"Refusing to listen on {args.host}; pass --allow-remote to opt in.")
            if not args.tls_certfile:
                raise SecureJiraError("A non-local HTTP listener requires --tls-certfile and --tls-keyfile.")
            if not settings.http_allowed_hosts():
                raise SecureJiraError("Set MCP_HTTP_ALLOWED_HOSTS before exposing the HTTP listener.")
        if args.transport == "stdio":
            if not settings.has_jira_credentials() and not settings.has_confluence_credentials():
                logger.warning("No Jira or Confluence credentials are set; tool calls will fail until configured.")
        server = build_server(settings)
    except SecureJiraError as e:
        if args.check_connection or args.check_confluence_connection:
            service = "Confluence" if args.check_confluence_connection else "Jira"
            if isinstance(e, AuthError):
                print(f"{service} authentication failed: {e}", file=sys.stderr)
                return 3
            print(f"{service} connection could not be verified: {e}", file=sys.stderr)
            return 2
        print(f"secure-jira-mcp: {e}", file=sys.stderr)
        return 2

    groups = ", ".join(sorted(settings.groups()))
    logger.info(
        "secure-jira-mcp %s: transport=%s, groups=[%s], read_only=%s, confirmation=%s, allowed hosts=%s",
        __version__, args.transport, groups, settings.mcp_read_only, settings.mcp_confirmation_mode,
        ", ".join(settings.allowed_hosts()) or "none",
    )
    if args.transport == "stdio":
        server.run("stdio")
    else:
        allowed_hosts = settings.http_allowed_hosts()
        if not allowed_hosts and _is_loopback(args.host):
            allowed_hosts = list(dict.fromkeys([
                f"{args.host}:{args.port}",
                f"localhost:{args.port}",
                f"127.0.0.1:{args.port}",
                f"[::1]:{args.port}",
            ]))
        transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
            allowed_origins=settings.http_allowed_origins(),
        )
        app = server.streamable_http_app(host=args.host, transport_security=transport_security)
        import uvicorn

        config = uvicorn.Config(
            app,
            host=args.host,
            port=args.port,
            log_level="info",
            server_header=False,
            ssl_certfile=args.tls_certfile,
            ssl_keyfile=args.tls_keyfile,
            ssl_ca_certs=args.mtls_ca_file,
            ssl_cert_reqs=ssl.CERT_REQUIRED if args.mtls_ca_file else ssl.CERT_NONE,
        )
        scheme = "https" if args.tls_certfile else "http"
        logger.info("Listening on %s://%s:%s/mcp", scheme, args.host, args.port)
        uvicorn.Server(config).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
