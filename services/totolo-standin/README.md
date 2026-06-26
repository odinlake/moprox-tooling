# totolo-standin — the themeontology.org search MCP, running on claude-dev (temporary)

A **stand-in** for the planned `totolo` LXC (`moprox-homelab/services/totolo`). It runs the same
`python-totolo-search` MCP server here on claude-dev and exposes it **at the totolo endpoint**
(`http://totolo.lan:8765/mcp`) so the **theming** agent uses it exactly as it will use the real
container — no consumer reconfiguration when the LXC lands.

- **What:** CPU-only torch + the package + a prebuilt index, served via the same `serve_http.py`
  launcher (FastMCP streamable-http, `0.0.0.0:8765`, path `/mcp`). `serve_http.py` here is a copy of
  the homelab one — keep them in sync.
- **"As if in moprox":** `install.sh` adds `10.10.10.10 totolo.lan totolo` to `/etc/hosts`, so the
  totolo *name* resolves to this VM for now. `/etc/hosts` wins over DNS, so when the DNS box later
  serves `totolo -> 10.10.10.4` (the real container), `uninstall.sh` drops the hosts line and the
  name flips to the container with zero changes anywhere else.
- **Sizing:** validated ~460 MB serving / ~1.1 GB during reindex (see the homelab README); here it
  just runs as a `User=mikael` service, no cap.

## Use
```
./install.sh      # idempotent: deps, venv, index, service, hosts entry, verify
./uninstall.sh    # when the real LXC is up
```

This is deliberately **not** wired into the dashboard updater or the installer — it's a bridge until
the container exists, then it goes away.
