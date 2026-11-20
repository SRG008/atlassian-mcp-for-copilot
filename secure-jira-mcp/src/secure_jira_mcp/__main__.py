"""Command line: secure-jira-mcp [--transport stdio|http] [--host 127.0.0.1] [--port 9200]."""

import argparse
import ipaddress
import logging
import os
import sys

from secure_jira_mcp import __version__
from secure_jira_mcp.config import Settings
from secure_jira_mcp.errors import SecureJiraError
from secure_jira_mcp.server import build_server

logger = logging.getLogger("secure_jira_mcp")


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="secure-jira-mcp", description="Secure MCP server for Jira Data Center / Server.")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio",
                        help="stdio: one user, token from JIRA_PERSONAL_TOKEN. http: many users, token per request.")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP listen address (default 127.0.0.1).")
    parser.add_argument("--port", type=int, default=9200, help="HTTP port (default 9200).")
    parser.add_argument("--allow-remote", action="store_true",
                        help="Allow a non-local listen address. Put a TLS reverse proxy in front: tokens travel in headers.")
    parser.add_argument("--env-file", default=".env", help="Settings file (default .env in the current folder).")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    # stdout carries the MCP protocol in stdio mode, so logs go to stderr.
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    try:
        settings = Settings(_env_file=args.env_file)
        if settings.https_proxy:
            for name in ("HTTPS_PROXY", "HTTP_PROXY"):
                os.environ.setdefault(name, settings.https_proxy)
            no_proxy = [h for h in os.environ.get("NO_PROXY", "").split(",") if h.strip()]
            os.environ["NO_PROXY"] = ",".join(dict.fromkeys([*no_proxy, "localhost", "127.0.0.1"]))
        if settings.use_system_certificates:
            import truststore

            truststore.inject_into_ssl()
        if args.transport == "http" and not _is_loopback(args.host) and not args.allow_remote:
            raise SecureJiraError(f"Refusing to listen on {args.host}. Use --allow-remote behind a TLS reverse proxy.")
        if args.transport == "stdio" and not settings.jira_personal_token.get_secret_value():
            logger.warning("JIRA_PERSONAL_TOKEN is not set; every tool call will fail until it is.")
        server = build_server(settings)
    except SecureJiraError as e:
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
        logger.info("Listening on http://%s:%s/mcp", args.host, args.port)
        server.run("streamable-http", host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
