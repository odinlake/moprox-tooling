#!/usr/bin/env python3
"""mo.lan/claude-login -- broker the interactive Claude login for each estate box, from a phone.

Why it exists (operator, 2026-10-02): `claude auth login` prints a long OAuth URL wrapped in an OSC-8
terminal hyperlink, and the web terminal (mo.lan/tty) wraps and breaks it, so it cannot be copied.
This page runs the SAME command server-side in a pty, extracts the URL, shows it as a real link with
a copy button, and takes the pasted code back to the waiting process.

    GET  /                     the page
    GET  /api/status           per box: logged in?, account, refresh-token ceiling
    POST /api/start {box}      start `claude auth login` on that box; returns {sid, url}
    POST /api/code {sid,code}  hand the code to that login; returns its output + a fresh AUTH-OK probe
    POST /api/cancel {sid}

Each box gets its OWN login (its own grant). Never copy credentials between boxes: a copied grant
dies at the first refresh by the other box (proven 2026-10-01/02, moprox-memory
loop-box-credentials-from-claude-dev).

Behind Authelia one_factor even on trusted networks (like /tty: whoever drives this controls which
account every agent runs on), plus an nftables gate admitting only the web box, plus an Origin check.
"""
import json, os, pty, re, select, signal, subprocess, sys, threading, time, uuid
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "lib"))
import errlog

PORT = int(os.environ.get("CLAUDE_LOGIN_PORT", "8034"))
ORIGINS = {"https://mo.lan", "http://127.0.0.1:%d" % PORT, "http://localhost:%d" % PORT}
SSH = ["ssh", "-i", os.path.expanduser("~/.ssh/claude-dev-ops"), "-o", "ConnectTimeout=8",
       "-o", "BatchMode=yes"]
CLEAN = "env -u ANTHROPIC_API_KEY -u CLAUDE_CODE_OAUTH_TOKEN -u ANTHROPIC_AUTH_TOKEN"

def _q(s):
    return "'" + s.replace("'", "'\\''") + "'"


# How to run a command as mikael on each box. claude-loop is Squid-contained: without the proxy
# variables from /etc/environment the CLI has no route out and simply hangs.
BOXES = {
    "claude-dev": {"label": "claude-dev", "what": "dev sessions, coach, valet, bard, theming, the Telegram agents",
                   "shell": lambda c, tty=False: ["bash", "-lc", c]},
    "claude-loop": {"label": "claude-loop", "what": "the analyst loop",
                    "shell": lambda c, tty=False: SSH + (["-tt"] if tty else []) + ["agent@10.10.10.11",
                        "sudo -n -u mikael env HOME=/home/mikael PATH=/home/mikael/.local/bin:/usr/bin:/bin TERM=xterm "
                        "$(grep -h '^[A-Za-z_]*=' /etc/environment | tr '\\n' ' ') bash -lc " + _q(c)]},
}

STATUS_SH = r"""%s claude auth status --json 2>/dev/null; echo '@@CREDS@@'; python3 -c '
import json,os
p=os.path.expanduser("~/.claude/.credentials.json")
try:
    d=json.load(open(p)).get("claudeAiOauth") or {}
    print(json.dumps({"refresh_exp": d.get("refreshTokenExpiresAt"), "access_exp": d.get("expiresAt"),
                      "mtime": os.path.getmtime(p)}))
except Exception as e:
    print(json.dumps({"error": str(e)}))'""" % CLEAN
PROBE_SH = "cd /tmp && %s timeout 90 claude -p 'Reply with exactly: AUTH-OK' 2>&1 | tail -1" % CLEAN
LOGIN_SH = "cd /tmp && %s BROWSER=/bin/false claude auth login --claudeai" % CLEAN

URL_RE = re.compile(r"https://claude\.(?:com|ai)/[^\s\x07\x1b\"'<>]+")
ANSI_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[A-Za-z]")

SESSIONS = {}           # sid -> {box, pid, fd, out, started}
LOCK = threading.Lock()
TTL = 15 * 60


def run(box, cmd, timeout=60):
    r = subprocess.run(BOXES[box]["shell"](cmd), capture_output=True, text=True, timeout=timeout)
    return r.stdout


def status(box):
    try:
        out = run(box, STATUS_SH, timeout=40)
        auth, _, creds = out.partition("@@CREDS@@")
        a = json.loads(auth.strip() or "{}")
        c = json.loads(creds.strip().splitlines()[-1]) if creds.strip() else {}
        return {"box": box, "label": BOXES[box]["label"], "what": BOXES[box]["what"],
                "logged_in": bool(a.get("loggedIn")), "email": a.get("email"),
                "refresh_exp": c.get("refresh_exp"), "access_exp": c.get("access_exp"), "mtime": c.get("mtime")}
    except Exception as e:
        errlog.err("claude-login: status of %s" % box, e)
        return {"box": box, "label": BOXES[box]["label"], "what": BOXES[box]["what"], "error": str(e)[:200]}


def _read(fd, until, secs):
    buf, end = b"", time.time() + secs
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.5)
        if not r:
            continue
        try:
            c = os.read(fd, 4096)
        except OSError:
            break
        if not c:
            break
        buf += c
        if until(buf):
            break
    return buf.decode(errors="replace")


def _reap(sid):
    s = SESSIONS.pop(sid, None)
    if not s:
        return
    try:
        os.kill(s["pid"], signal.SIGKILL)
    except ProcessLookupError:
        pass                                    # benign: already exited after the code was accepted
    try:
        os.waitpid(s["pid"], os.WNOHANG)
        os.close(s["fd"])
    except OSError:
        pass                                    # benign: fd/child already gone


def start(box):
    with LOCK:
        for sid in [k for k, v in SESSIONS.items() if v["box"] == box or time.time() - v["started"] > TTL]:
            _reap(sid)                          # one login per box at a time; old ones expire
    pid, fd = pty.fork()
    if pid == 0:
        cmd = BOXES[box]["shell"](LOGIN_SH, tty=True)
        os.execvp(cmd[0], cmd)
    import fcntl, termios, struct
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 1000, 0, 0))
    out = _read(fd, lambda b: b"Paste code" in b, 60)
    m = URL_RE.search(out)
    if not m:
        os.kill(pid, signal.SIGKILL)
        raise RuntimeError("no sign-in URL from %s: %s" % (box, ANSI_RE.sub("", out)[-300:]))
    sid = uuid.uuid4().hex
    with LOCK:
        SESSIONS[sid] = {"box": box, "pid": pid, "fd": fd, "started": time.time()}
    return {"sid": sid, "url": m.group(0)}


def code(sid, text):
    with LOCK:
        s = SESSIONS.get(sid)
    if not s:
        raise RuntimeError("that login has expired or was replaced; start again")
    os.write(s["fd"], text.strip().encode() + b"\r")
    out = _read(s["fd"], lambda b: re.search(rb"(?i)success|logged in|error|invalid|failed", b) is not None, 60)
    time.sleep(1)
    out += _read(s["fd"], lambda b: False, 2)
    with LOCK:
        _reap(sid)
    clean = ANSI_RE.sub("", out).replace("\r", "").strip()
    probe = run(s["box"], PROBE_SH, timeout=120).strip()
    return {"box": s["box"], "output": clean[-600:], "probe": probe[-200:], "ok": "AUTH-OK" in probe,
            "status": status(s["box"])}


class H(BaseHTTPRequestHandler):
    server_version = "claude-login"

    def log_message(self, fmt, *a):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % a))

    def send(self, code_, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code_)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        p = self.path.split("?")[0]
        try:
            if p in ("/", "/index.html"):
                return self.send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
            if p == "/api/status":
                return self.send(200, [status(b) for b in BOXES])
            return self.send(404, {"error": "not found"})
        except Exception as e:
            errlog.err("claude-login GET %s" % p, e, trace=True)
            return self.send(500, {"error": "%s: %s" % (type(e).__name__, e)})

    def do_POST(self):
        p = self.path.split("?")[0]
        if self.headers.get("Origin") not in ORIGINS:
            return self.send(403, {"error": "bad origin"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}") if n else {}
            if p == "/api/start":
                if req.get("box") not in BOXES:
                    return self.send(400, {"error": "unknown box"})
                return self.send(200, start(req["box"]))
            if p == "/api/code":
                return self.send(200, code(str(req.get("sid")), str(req.get("code") or "")))
            if p == "/api/cancel":
                with LOCK:
                    _reap(str(req.get("sid")))
                return self.send(200, {"ok": True})
            return self.send(404, {"error": "not found"})
        except Exception as e:
            errlog.err("claude-login POST %s" % p, e)
            return self.send(500, {"error": str(e)[:300]})


if __name__ == "__main__":
    print("claude-login on :%d" % PORT, flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
