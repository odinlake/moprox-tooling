#!/usr/bin/env python3
"""Forward-feed: plot a training session's HR and push it to Telegram.

A building block for the Polar fetcher — call `send_session(s)` for each new session and the
athlete gets a chart in their DM. Pure stdlib + matplotlib (no `requests`; multipart is built by
hand). Creds come from ~/.config/claude-dev/telegram.env (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID).

  telegram_feed.py demo                 # plot + send the latest session from the dashboard data
  telegram_feed.py session <file.json>  # plot + send one session dict (build.py's shape)
"""
import io, json, os, sys, urllib.request
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

CAT = {"easy": "#3fb950", "tempo": "#d29922", "vo2max": "#f85149",
       "speed": "#a371f7", "trail": "#58a6ff", "other": "#6e7681"}
TG_ENV = Path(os.environ.get("TELEGRAM_ENV", Path.home() / ".config/claude-dev/telegram.env"))

def _creds():
    tok = os.environ.get("TELEGRAM_BOT_TOKEN"); chat = os.environ.get("TELEGRAM_CHAT_ID")
    if (not tok or not chat) and TG_ENV.exists():
        for ln in TG_ENV.read_text().splitlines():
            if ln.startswith("TELEGRAM_BOT_TOKEN="): tok = ln.split("=", 1)[1].strip()
            if ln.startswith("TELEGRAM_CHAT_ID="): chat = ln.split("=", 1)[1].strip()
    if not tok or not chat: sys.exit("missing telegram creds (TELEGRAM_BOT_TOKEN/CHAT_ID)")
    return tok, chat

def plot_session(s):
    tr = s["trace"]; step = s.get("trace_step_s", 1)
    xs = [i * step / 60 for i in range(len(tr))]
    color = CAT.get(s.get("cat"), "#58a6ff")
    fig, ax = plt.subplots(figsize=(8, 3.2), dpi=120)
    fig.patch.set_facecolor("#0d1117"); ax.set_facecolor("#0d1117")
    ax.plot(xs, tr, color=color, lw=1.6)
    for r in s.get("reps", []):           # mark detected interval peaks
        i = int(r["t"] * (len(tr) - 1)); ax.plot(xs[i], r["peak"], "o", color=color, ms=4)
    ax.set_xlabel("minutes", color="#8b949e"); ax.set_ylabel("HR (bpm)", color="#8b949e")
    ax.set_title("%s · %s" % (s.get("cat", "?"), s.get("date", "")[:16].replace("T", " ")), color="#e6edf3")
    ax.tick_params(colors="#8b949e")
    for sp in ax.spines.values(): sp.set_color("#30363d")
    ax.grid(True, alpha=0.15)
    buf = io.BytesIO(); fig.tight_layout(); fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close(fig); return buf.getvalue()

def caption(s):
    reps = " · %d reps" % s["nint"] if s.get("nint") else ""
    return "🏃 %s · %s · %d min · avg %d / max %d bpm%s" % (
        s.get("cat", "?"), s.get("date", "")[:10], round(s.get("dur_min", 0)),
        s.get("hr_avg", 0), s.get("hr_max", 0), reps)

def send_photo(png, cap, tok=None, chat=None):
    if tok is None: tok, chat = _creds()
    boundary = "----moprox%d" % os.getpid()
    body = b""
    for k, v in (("chat_id", chat), ("caption", cap)):
        body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (boundary, k, v)).encode()
    body += ("--%s\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"hr.png\"\r\nContent-Type: image/png\r\n\r\n" % boundary).encode()
    body += png + ("\r\n--%s--\r\n" % boundary).encode()
    req = urllib.request.Request("https://api.telegram.org/bot%s/sendPhoto" % tok, data=body,
                                 headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary})
    return json.load(urllib.request.urlopen(req, timeout=30))

def send_session(s):
    r = send_photo(plot_session(s), caption(s))
    return bool(r.get("ok"))

def _latest_from_dashboard():
    p = Path.home() / ".cache/moprox-dashboard-ghpages/dashboard/data/training/sessions.json"
    if not p.exists(): sys.exit("no dashboard sessions.json — run the updater first")
    return json.loads(p.read_text())["sessions"][0]   # newest first

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "demo"
    if mode == "demo":
        s = _latest_from_dashboard()
    elif mode == "session":
        s = json.loads(Path(sys.argv[2]).read_text())
    else:
        sys.exit("usage: telegram_feed.py [demo | session <file.json>]")
    print("sent:" , send_session(s), "—", caption(s))
