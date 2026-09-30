#!/usr/bin/env python3
"""Hourly Telegram reminders for one appointment, as a systemd timer that survives reboots.

    remind.py add <name> "<YYYY-MM-DD HH:MM>" "<what>" [--hours 8,9,10,11,12]
        install /etc/systemd/system/remind-<name>.{service,timer} (sudo) and send one confirmation
    remind.py send <name> "<YYYY-MM-DD HH:MM>" "<what>"
        one reminder now (what the timer runs); says nothing once the appointment has started
    remind.py list                      the reminder timers and when each fires next
    remind.py remove <name>             stop and delete one

Every message goes through tg.py as #reminder (the operator's rule: every Telegram message carries its
sender's handle) and counts down to the appointment, so the last one before it reads differently from
the first. Timer is Persistent=true: an hour missed while claude-dev was down fires on boot, which is
the failure that matters here ("I missed yesterday and mustn't make the mistake again", 2026-09-30).
Times are Europe/London, the box's zone. The units are generated, not in the repo: remove them with
`remove` once the day has passed.
"""
import datetime, os, subprocess, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HERE = "/opt/moprox-tooling/services/forward/remind.py"   # the units run the deployed copy
UNIT_DIR = "/etc/systemd/system"
AGENT = "reminder"


def when(s):
    return datetime.datetime.strptime(s, "%Y-%m-%d %H:%M")


def text(what, at, now=None):
    now = now or datetime.datetime.now()
    mins = int((at - now).total_seconds() // 60)
    left = "%dh %02dm" % divmod(mins, 60) if mins >= 60 else "%d min" % mins
    day = "today" if at.date() == now.date() else at.strftime("%a %-d %b")
    return "⏰ *%s* at *%s* %s (in %s)" % (what, at.strftime("%H:%M"), day, left)


def send(name, at_s, what):
    import tg
    at = when(at_s)
    if datetime.datetime.now() >= at:
        print("%s: appointment has started, nothing sent" % name)
        return
    msg = text(what, at)
    tg.send(msg, agent=AGENT)
    print("sent: %s" % msg)


def _q(s):
    """systemd ExecStart quoting for one argument."""
    return '"%s"' % s.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")


def add(name, at_s, what, hours):
    at = when(at_s)
    svc = "remind-%s" % name
    unit = ("[Unit]\nDescription=reminder: %s at %s\nAfter=network-online.target\nWants=network-online.target\n"
            "[Service]\nType=oneshot\nUser=mikael\nEnvironment=HOME=/home/mikael\nEnvironment=PYTHONUNBUFFERED=1\n"
            "ExecStart=/usr/bin/python3 %s send %s %s %s\n") % (what, at_s, HERE, _q(name), _q(at_s), _q(what))
    cal = "\n".join("OnCalendar=%s %02d:00:00" % (at.strftime("%Y-%m-%d"), h) for h in hours)
    timer = ("[Unit]\nDescription=reminder timer: %s at %s\n[Timer]\n%s\nPersistent=true\n"
             "[Install]\nWantedBy=timers.target\n") % (what, at_s, cal)
    for fn, body in ((svc + ".service", unit), (svc + ".timer", timer)):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write(body)
        subprocess.run(["sudo", "install", "-o", "root", "-g", "root", "-m0644", f.name,
                        os.path.join(UNIT_DIR, fn)], check=True)
        os.unlink(f.name)
    subprocess.run(["sudo", "systemctl", "daemon-reload"], check=True)
    subprocess.run(["sudo", "systemctl", "enable", "--now", svc + ".timer"], check=True)
    import tg
    tg.send("⏰ Reminder set: *%s* on %s at %s. I'll ping you at %s." % (
        what, at.strftime("%a %-d %b"), at.strftime("%H:%M"),
        ", ".join("%02d:00" % h for h in hours)), agent=AGENT)


def main(a):
    if not a:
        sys.exit(__doc__)
    if a[0] == "send" and len(a) == 4:
        return send(a[1], a[2], a[3])
    if a[0] == "add" and len(a) >= 4:
        hours = [8, 9, 10, 11, 12]
        if "--hours" in a:
            hours = [int(h) for h in a[a.index("--hours") + 1].split(",")]
        return add(a[1], a[2], a[3], hours)
    if a[0] == "list":
        return subprocess.run(["systemctl", "list-timers", "remind-*", "--all", "--no-pager"])
    if a[0] == "remove" and len(a) == 2:
        svc = "remind-%s" % a[1]
        subprocess.run(["sudo", "systemctl", "disable", "--now", svc + ".timer"], check=False)
        for ext in (".service", ".timer"):
            subprocess.run(["sudo", "rm", "-f", os.path.join(UNIT_DIR, svc + ext)], check=True)
        return subprocess.run(["sudo", "systemctl", "daemon-reload"], check=True)
    sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
