#!/usr/bin/env python3
"""Serve the totolo-search MCP server over a NETWORK transport (FastMCP streamable-http).

Upstream (totolo_search.mcp.server) only ships a stdio runner — fine for a local IDE, but not
reachable across subnets. We import its FastMCP instance directly and run it with the
streamable-http transport bound to 0.0.0.0, so MCP clients on BOTH the LAN (vmbr0) and the
isolated subnet (vmbr1) reach it at  http://<host>:<port>/mcp .

Env: MCP_HOST (default 0.0.0.0), MCP_PORT (default 8765).
"""
import os

from totolo_search.mcp.server import mcp, _get_engine
from mcp.server.transport_security import TransportSecuritySettings

mcp.settings.host = os.environ.get("MCP_HOST", "0.0.0.0")
mcp.settings.port = int(os.environ.get("MCP_PORT", "8765"))

# The SDK's DNS-rebinding guard validates the Host header against an allowlist that defaults to
# localhost, so reaching this server by any other name/IP (totolo.lan, the LAN + isolated addresses)
# returns 421 Misdirected Request. This MCP is consumed by headless agents over the trusted isolated
# subnet — there is no browser in the threat model — so disable the guard. Set MCP_ALLOWED_HOSTS
# (comma-separated host[:port]) to instead keep the guard on with an explicit allowlist.
_hosts = [h for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
mcp.settings.transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=bool(_hosts),
    allowed_hosts=_hosts, allowed_origins=[] if _hosts else ["*"])

# Warm the engine (load the prebuilt index + embedding model) before accepting traffic, so the
# first real search isn't penalised by a cold model load. Non-fatal if it hiccups — the first
# tool call will retry the lazy build.
try:
    _get_engine()
except Exception as e:                       # noqa: BLE001 — never refuse to start over warmup
    print(f"warmup skipped: {e}", flush=True)

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
