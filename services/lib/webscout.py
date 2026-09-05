#!/usr/bin/env python3
"""A minimal client for the webscout MCP (streamable-http JSON-RPC), for non-agent jobs.

webscout (LXC 113, http://webscout.lan:8003/mcp) is normally driven by an agent through its MCP
tool surface. A systemd job has no MCP client, so it speaks the protocol directly: POST initialize,
keep the `mcp-session-id` header the server hands back, send notifications/initialized, then
tools/call. Replies come back as SSE `data:` lines.

One connection per call() is deliberate. These jobs make a handful of calls a few times a day, the
handshake is two cheap round-trips, and a long-lived connection would need reconnect logic for no
gain. Browser SESSIONS are server-global and keyed by the sid webscout returns, so a session opened
under one connection is still usable from the next — which is what makes this safe.
"""
import json
import urllib.request

URL = "http://webscout.lan:8003/mcp"
HDR = {"Content-Type": "application/json",
       "Accept": "application/json, text/event-stream"}


def _post(payload, sid=None, timeout=300):
    h = dict(HDR)
    if sid:
        h["mcp-session-id"] = sid
    req = urllib.request.Request(URL, json.dumps(payload).encode(), h)
    r = urllib.request.urlopen(req, timeout=timeout)
    body = r.read().decode()
    out = None
    for ln in body.splitlines():                      # SSE: the LAST data: line carries the result
        if ln.startswith("data:"):
            out = json.loads(ln[5:].strip())
    if out is None and body.strip():
        out = json.loads(body)
    return out, r.headers.get("mcp-session-id")


def call(tool, args=None, timeout=300):
    """Call one webscout tool and return its text content. Raises on a protocol or tool error."""
    _, sid = _post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                               "clientInfo": {"name": "moprox", "version": "1"}}}, timeout=timeout)
    _post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid, timeout=timeout)
    res, _ = _post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": tool, "arguments": args or {}}}, sid, timeout=timeout)
    if res is None:
        raise RuntimeError("webscout %s: empty response" % tool)
    if res.get("error"):
        raise RuntimeError("webscout %s: %s" % (tool, json.dumps(res["error"])[:300]))
    result = res.get("result") or {}
    if result.get("isError"):
        raise RuntimeError("webscout %s failed: %s" % (tool, json.dumps(result)[:300]))
    for c in result.get("content", []):
        if c.get("type") == "text":
            return c["text"]
    return json.dumps(result)


def js(session, expression, timeout=300):
    """eval_js, unwrapping webscout's JSON-string-in-a-JSON-string result into a Python object."""
    raw = call("eval_js", {"session": session, "expression": expression}, timeout=timeout)
    val = json.loads(raw) if raw and raw.lstrip().startswith(("\"", "{", "[")) else raw
    if isinstance(val, str):
        try:
            val = json.loads(val)
        except ValueError:
            pass
    return val
