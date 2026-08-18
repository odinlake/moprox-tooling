# webtty — a browser terminal on claude-dev

**LIVE since 2026-08-18** at **https://mo.lan/tty** (Authelia-gated). `ttyd` 1.7.7 serving a
persistent `tmux` session as `mikael`.

## Why it exists
claude-dev is **single-homed on the isolated subnet** (`10.10.10.10`, vmbr1 only) — by design, and
unlike every other box in the estate. Until today nothing on the house LAN could reach it, so the
Proxmox noVNC console was the only way to a shell. noVNC **cannot paste**, which makes the single
most common reason to want a shell here — an interactive `claude` + `/login` when the Max refresh
token expires (see `../sessions/`) — miserable, especially from a phone.

This is not new *access*: it is *usable* access. Paste works, mobile works, and the session persists.

## Install
```bash
services/webtty/install.sh          # on claude-dev; idempotent, uses sudo
```
Pins `ttyd` 1.7.7 and verifies it against upstream's `SHA256SUMS` **before** making it executable
(ttyd is not packaged for Debian). Installs the nftables gate, both units, and enables them.

## How the pieces fit
```
browser ──https──▶ Caddy on web (192.168.1.220 / 10.10.10.12)
                     │  forward_auth ─▶ Authelia   ← the whole boundary
                     ▼
                   claude-dev.lan:7681  (ttyd, bound to eth0)
                     ▼
                   tmux new -A -s web   (as mikael)
```

- **`--base-path /tty`**, and Caddy uses `handle` not `handle_path`. ttyd generates its own asset
  and websocket URLs; stripping the prefix would 404 every one of them.
- **`tmux new -A -s web`** (attach-or-create) is what makes this survivable on a phone: an OAuth
  bounce out to the browser and back lands in the *same* session, not a dead shell. The tmux server
  outlives `webtty.service`, so a restart of the unit does not cost the session.
- **`--check-origin`** is on. Caddy preserves `Host`, so `Origin: https://mo.lan` matches.

## Security posture — read before changing anything
Behind this route is a shell as `mikael`, which holds the `claude-dev-ops` SSH key (→ every guest),
the GitHub push key, the Claude Max credentials, and the private-data working tree. It is the most
privileged surface on mo.lan, and **nothing prompts after Authelia** — the Authelia rule *is* the
boundary.

Two consequences, both deliberate and both easy to undo by accident:

1. **The Authelia rule sits ABOVE the trusted-network bypass** in `private-web/authelia/
   configuration.yml`. Authelia is first-match-wins, and mo.lan carries a `policy: bypass` for the
   phone's DHCP reservation and the `192.168.2.0/24` VPN pool. Listed with the other resources —
   the obvious place — `/tty` would be an *unauthenticated* shell from those networks. Verified
   cookieless after every change: `curl -sk -o /dev/null -w '%{http_code}' https://mo.lan/tty/`
   must be **302**, never 200.
2. **The nftables gate is tighter than mcpgate.** mcpgate allows the whole agent subnet; this drops
   7681 from everything except `10.10.10.12` (the web box's isolated leg). An MCP port exposes
   data; this one exposes a shell. `webtty.service` **`Requires=`** `webtty-gate.service`, so the
   shell can never be listening before the gate is up.

`one_factor`, not two — operator decision 2026-08-18: the hypervisor itself sits on the LAN behind a
single password, so 2FA on this one route would be inconsistent theatre.

## Verify
```bash
systemctl is-active webtty-gate webtty
sudo nft list table inet webtty
curl -sk -o /dev/null -w '%{http_code}\n' https://mo.lan/tty/    # 302 = gated
```
Measured 2026-08-18: reachable from web (`200`), dropped from mcp-search (`curl` rc 28).
