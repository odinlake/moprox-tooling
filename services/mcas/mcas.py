#!/usr/bin/env python3
"""Pelham Primary's MCAS (MyChildAtSchool) announcements and inbox messages, for the valet.

    mcas.py fetch        read both feeds, store anything new, summarise it; print what is unsent
    mcas.py baseline     fetch and mark EVERYTHING as already reported (first run, or after a gap)
    mcas.py pending      print the unsent block without fetching

valet_brief.py calls block() in BOTH its runs (05:15 brief, 16:15 review), appends what it returns and,
once Telegram has taken it, calls mark_sent(). So each item is reported exactly once, in whichever
update comes first. Operator, 2026-09-30: "Make valet include summary of any new notification /
announcement / message from MCAS in its updates (both)".

MCAS has two feeds, the header's two icons: Announcements (MCSAnnouncements.aspx, the whole archive on
one page as `.timeline-item`s) and Messages (MCSInbox.aspx, conversations as `a.ConversationItem`, the
open one's messages as `#Conversations .chat-user`). Neither carries an id, so an announcement is keyed
on its caption (title + "Posted by ... on <date>") and a message on its text.

Reads go through webscout (services/lib/webscout.py), which holds the login; when the stored session
has expired the page lands on MCSParentLogin, and this re-establishes it once from the vault. A read
must PROVE it read something: the student's name on the page and at least one announcement, or it
raises. An empty feed must never be reported as "nothing new".
"""
import datetime, hashlib, json, os, re, sys, time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "services/lib"))
sys.path.insert(0, str(_ROOT / "services/agents"))
import webscout, errlog

SITE = "mychildatschool.com"
BASE = "https://www.mychildatschool.com/MCAS/"
STATE = Path.home() / ".local/share/moprox/mcas"
ITEMS = STATE / "items.json"            # id -> item, kept for good (the archive the stats/facts use)
SENT = STATE / "valet-sent.json"        # ids the valet has already reported
STUDENT = "Akiko Onsjoe"

ANN_JS = r"""(()=>JSON.stringify([...document.querySelectorAll('.timeline .timeline-item')].map(it=>{
  const cap=it.querySelector('.timeline-body-head-caption'), body=it.querySelector('.timeline-body-content');
  return {caption:cap?cap.innerText.trim():'', body:body?body.innerText.trim():''}})))()"""
MSG_JS = r"""(()=>JSON.stringify([...document.querySelectorAll('#Conversations .chat-user')].map(c=>{
  const t=c.querySelector('small'); const b=c.querySelector('.media-body')||c;
  return {time:t?t.innerText.trim():'', text:b.innerText.replace(t?t.innerText:'','').trim()}})))()"""
CONV_JS = r"""(()=>document.querySelectorAll('#ConversationsDIV a.ConversationItem').length)()"""
WHO_JS = r"""(()=>JSON.stringify({login:location.pathname.includes('Login'), text:document.body.innerText.slice(0,400)}))()"""


def _load(p, default):
    try:
        return json.load(open(p))
    except FileNotFoundError:
        return default


def _save(p, obj):
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = str(p) + ".tmp"
    json.dump(obj, open(tmp, "w"), ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def _wait(sess, js, ok, secs=45):
    """Poll an expression until ok(result); MCAS fills its lists by ajax after load."""
    end, val = time.time() + secs, None
    while time.time() < end:
        val = webscout.js(sess, js)
        if ok(val):
            return val
        time.sleep(3)
    return val


def _open():
    for attempt in (1, 2):
        sess = json.loads(webscout.call("open_session", {"site": SITE, "url": BASE + "MCSDashboardPage"}))["session"]
        who = _wait(sess, WHO_JS, lambda v: v and (v["login"] or STUDENT in v["text"]), 30)
        if who and not who["login"] and STUDENT in who["text"]:
            return sess
        webscout.call("close", {"session": sess})
        if attempt == 1:
            webscout.call("establish_session", {"domain": SITE}, timeout=300)
    raise RuntimeError("MCAS: not signed in after re-establishing the session")


def _id(*parts):
    return hashlib.sha1("\x1f".join(parts).encode()).hexdigest()[:16]


def _split_caption(cap):
    m = re.match(r"(?s)(.*?)\s*Posted by\s+(.*?)\s+on\s+(\d{1,2})\w{2}\s+(\w+)\s+(\d{4})\.?\s*$", cap)
    if not m:
        return cap.strip(), None
    title, _, d, mon, y = m.groups()
    try:
        date = datetime.datetime.strptime("%s %s %s" % (d, mon, y), "%d %B %Y").date().isoformat()
    except ValueError:
        date = None
    return title.strip(), date


def read():
    """Both feeds as a list of {id, kind, title, date, body}. Raises unless the read is proven."""
    sess = _open()
    try:
        webscout.call("goto", {"session": sess, "url": BASE + "MCSAnnouncements.aspx"}, timeout=120)
        anns = _wait(sess, ANN_JS, lambda v: isinstance(v, list) and len(v) > 0, 60)
        if not anns:
            raise RuntimeError("MCAS: the announcements page showed no announcements")
        out = []
        for a in anns:
            title, date = _split_caption(a["caption"])
            out.append({"id": _id("ann", a["caption"]), "kind": "announcement", "title": title,
                        "date": date, "body": a["body"]})
        webscout.call("goto", {"session": sess, "url": BASE + "MCSInbox.aspx"}, timeout=120)
        convs = _wait(sess, CONV_JS, lambda v: isinstance(v, int) and v > 0, 45)
        for i in range(int(convs or 0)):
            webscout.js(sess, "(()=>{document.querySelectorAll('#ConversationsDIV a.ConversationItem')[%d].click();return 1})()" % i)
            time.sleep(4)
            for m in webscout.js(sess, MSG_JS) or []:
                if m["text"]:
                    out.append({"id": _id("msg", m["text"]), "kind": "message", "title": m["text"][:80],
                                "date": m["time"], "body": m["text"]})
        return out
    finally:
        webscout.call("close", {"session": sess})


def summarise(items):
    """One line per item, in one agent call. Falls back to the title if the agent fails."""
    if not items:
        return {}
    from run import run_agent
    prompt = ("Summarise each of these school notices from Pelham Primary (the operator's daughter Akiko is in "
              "Reception, class Wrens) in ONE short line: what it is and anything the parent must DO, with "
              "dates and deadlines. Skip greetings. Reply with ONLY a JSON object mapping each id to its line.\n\n"
              + json.dumps([{"id": i["id"], "title": i["title"], "date": i["date"], "body": i["body"][:3000]}
                            for i in items], ensure_ascii=False))
    try:
        raw = run_agent("valet", prompt, timeout=300)
        return json.loads(re.search(r"(?s)\{.*\}", raw).group(0))
    except Exception as e:
        errlog.err("mcas.py summarise", e)
        return {}


def fetch():
    """Read, store new items with a summary, and return the unsent ones (oldest first)."""
    store = _load(ITEMS, {})
    fresh = [i for i in read() if i["id"] not in store]
    lines = summarise(fresh)
    now = datetime.datetime.now().isoformat(timespec="seconds")
    for i in fresh:
        store[i["id"]] = dict(i, first_seen=now, summary=lines.get(i["id"]) or i["title"])
    _save(ITEMS, store)
    return pending(store)


def pending(store=None):
    store = store if store is not None else _load(ITEMS, {})
    sent = set(_load(SENT, []))
    return sorted((v for k, v in store.items() if k not in sent), key=lambda v: v["first_seen"])


def mark_sent(items):
    _save(SENT, sorted(set(_load(SENT, [])) | {i["id"] for i in items}))


def fmt(items):
    if not items:
        return None
    lines = ["🏫 *School (MCAS)*"]
    for i in items[:8]:
        tag = "✉️ " if i["kind"] == "message" else ""
        lines.append("• %s%s" % (tag, i.get("summary") or i["title"]))
    if len(items) > 8:
        lines.append("• …and %d more in the MCAS app" % (len(items) - 8))
    return "\n".join(lines)


def block():
    """For valet: (text or None, items). Never raises; a failed read becomes one visible line."""
    try:
        items = fetch()
    except Exception as e:
        errlog.err("mcas.py fetch", e)
        items = pending()
        txt = fmt(items)
        note = "🏫 _MCAS could not be read this time (%s); check the app._" % str(e)[:80]
        return ((txt + "\n" + note) if txt else note), items
    return fmt(items), items


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "fetch"
    if cmd == "fetch":
        print(fmt(fetch()) or "nothing unsent")
    elif cmd == "baseline":
        store = _load(ITEMS, {})
        now = datetime.datetime.now().isoformat(timespec="seconds")
        for i in read():
            store.setdefault(i["id"], dict(i, first_seen=now, summary=i["title"]))
        _save(ITEMS, store)
        mark_sent(list(store.values()))
        print("baseline: %d items, all marked sent" % len(store))
    elif cmd == "pending":
        print(fmt(pending()) or "nothing unsent")
    else:
        sys.exit(__doc__)
