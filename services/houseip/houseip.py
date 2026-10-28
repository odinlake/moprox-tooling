#!/usr/bin/env python3
"""houseip — Telegram ping when the house's public IP changes, and when the VPN hostname goes stale.

Asked for 2026-10-28, after the VPN stopped connecting: BT had moved the house from 86.144.178.229
(held since at least 2026-06-18) to 86.185.6.154, and the Nighthawk never pushed the new address to
odinlake.mynetgear.com (free No-IP DDNS), which every VPN profile dials. Nobody knew the IP had changed.

Each run (houseip.timer, every 5 min):
  1. asks three "what is my IP" services and takes the answer at least two agree on. No majority ->
     skip the run (a flaky echo service must never look like a change);
  2. on a change vs. the stored IP, sends one #houseip message and appends to history.jsonl;
  3. resolves the VPN hostname at No-IP's own nameserver (no resolver cache in the way). If it has
     disagreed with the house IP for STALE_AFTER, sends one alert; when they agree again, says so.

The first run only records the IP. Nothing here needs credentials beyond tg.py's.

    houseip.py             one pass (what the timer runs)
    houseip.py --dry-run   print what it sees and would send; touch no state, send nothing
"""
import argparse, datetime, ipaddress, json, os, subprocess, sys, urllib.request
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "forward"))
import errlog  # noqa: E402

STATE_DIR = Path(os.environ.get("HOUSEIP_STATE", Path.home() / ".local/state/houseip"))
ECHOES = ["https://api.ipify.org", "https://icanhazip.com", "https://checkip.amazonaws.com"]
HOST = "odinlake.mynetgear.com"
NAMESERVERS = ["ns5.no-ip.com", "1.1.1.1"]   # authoritative first; a public resolver as fallback
STALE_AFTER = datetime.timedelta(minutes=15)  # the router gets this long to update DDNS itself
AGENT = "houseip"


def _ip(s):
    try:
        return str(ipaddress.IPv4Address(s.strip()))
    except ValueError:
        return None


def public_ip():
    votes = []
    for url in ECHOES:
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                ip = _ip(r.read(64).decode())
            if ip:
                votes.append(ip)
        except Exception as e:
            errlog.warn("houseip: %s failed" % url, e)
    top = Counter(votes).most_common(1)
    if not top or top[0][1] < 2:
        errlog.warn("houseip: no two echo services agree (%s), skipping run" % votes)
        return None
    return top[0][0]


def ddns_ip():
    for ns in NAMESERVERS:
        try:
            out = subprocess.run(["dig", "+short", "+time=5", "+tries=2", HOST, "@" + ns],
                                 capture_output=True, text=True, timeout=30).stdout
        except Exception as e:
            errlog.warn("houseip: dig @%s failed" % ns, e)
            continue
        ips = [i for i in map(_ip, out.split()) if i]
        if ips:
            return ips[0]
    errlog.warn("houseip: %s did not resolve at any of %s" % (HOST, NAMESERVERS))
    return None


def step(st, ip, ddns, now):
    """Pure decision: (state, house ip, ddns ip or None, now) -> (new state, [messages], changed?)."""
    st, msgs, changed = dict(st), [], False
    stamp = now.isoformat(timespec="seconds")
    if st.get("ip") is None:
        st.update(ip=ip, since=stamp)
    elif ip != st["ip"]:
        msgs.append("House IP changed: `%s` -> `%s` (old one held since %s)."
                    % (st["ip"], ip, st.get("since", "?")[:10]))
        st.update(ip=ip, since=stamp)
        changed = True
    if ddns is None:                       # unknown: neither start nor clear the stale clock
        return st, msgs, changed
    if ddns == ip:
        if st.get("stale_alerted"):
            msgs.append("VPN hostname `%s` now points at `%s` again. VPN should connect." % (HOST, ip))
        st.pop("stale_since", None)
        st.pop("stale_alerted", None)
        return st, msgs, changed
    st.setdefault("stale_since", stamp)
    late = now - datetime.datetime.fromisoformat(st["stale_since"]) >= STALE_AFTER
    if late and st.get("stale_alerted") != ddns + ">" + ip:
        msgs.append("VPN won't connect: `%s` points at `%s` but the house is `%s`. "
                    "Fix: edit the A record at my.noip.com, then router Advanced > Dynamic DNS > Apply."
                    % (HOST, ddns, ip))
        st["stale_alerted"] = ddns + ">" + ip
    return st, msgs, changed


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    ip = public_ip()
    if ip is None:
        return
    ddns = ddns_ip()
    sf = STATE_DIR / "state.json"
    st = json.loads(sf.read_text()) if sf.exists() else {}
    now = datetime.datetime.now(datetime.timezone.utc)
    new, msgs, changed = step(st, ip, ddns, now)
    if a.dry_run:
        print("house %s  ddns %s  state %s" % (ip, ddns, st))
        for m in msgs:
            print("would send:", m)
        return
    import tg
    for m in msgs:
        tg.send(m, agent=AGENT)         # raise if Telegram fails: state is not saved, so it retries
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if changed or not st:
        with open(STATE_DIR / "history.jsonl", "a") as f:
            f.write(json.dumps({"ts": new["since"], "ip": ip}) + "\n")
    tmp = sf.with_suffix(".tmp")
    tmp.write_text(json.dumps(new, indent=1))
    tmp.replace(sf)


if __name__ == "__main__":
    main()
