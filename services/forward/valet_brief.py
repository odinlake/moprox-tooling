#!/usr/bin/env python3
"""The valet's morning brief (05:15). Gathers the day's inputs deterministically, hands them to the
valet agent to compose a terse Telegram brief, and sends it.

Inputs: the operator's location (home unless the 3am fix says otherwise) -> weather + 07:30-09:00
rain (Open-Meteo); overnight world + business headlines and serious local news (Google News RSS);
overnight commits to the operator's repos/orgs by anyone who isn't the operator or an agent (gh).
"""
import datetime, email.utils, json, subprocess, sys, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
sys.path.insert(0, str(Path.home() / "projects/moprox-tooling/services/agents"))
sys.path.insert(0, str(Path.home() / "projects/moprox-tooling/services/forward"))
from run import run_agent
import tg, location, location_pull

ME = {"odinlake", "odinlake-ai"}        # operator + agents — their commits aren't news
WMO = {0: "clear", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "rime fog",
       51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
       61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
       71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "rain showers",
       81: "showers", 82: "violent showers", 85: "snow showers", 86: "snow showers",
       95: "thunderstorm", 96: "thunderstorm w/ hail", 99: "severe thunderstorm"}
# emoji per WMO code, for a glanceable weather icon next to the condition
WICON = {0: "☀️", 1: "🌤️", 2: "⛅", 3: "☁️", 45: "🌫️", 48: "🌫️", 51: "🌦️", 53: "🌦️", 55: "🌧️",
         56: "🌧️", 57: "🌧️", 61: "🌦️", 63: "🌧️", 65: "🌧️", 66: "🌧️", 67: "🌧️", 71: "🌨️", 73: "🌨️",
         75: "❄️", 77: "🌨️", 80: "🌦️", 81: "🌧️", 82: "⛈️", 85: "🌨️", 86: "❄️", 95: "⛈️", 96: "⛈️", 99: "⛈️"}

def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "moprox-valet/1"})
    return urllib.request.urlopen(req, timeout=timeout).read()

def weather(lat, lon):
    url = ("https://api.open-meteo.com/v1/forecast?latitude=%.4f&longitude=%.4f"
           "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum"
           "&hourly=precipitation,precipitation_probability&timezone=auto&forecast_days=1" % (lat, lon))
    try: d = json.loads(_get(url))
    except Exception as e: return {"error": str(e)}
    daily, hourly = d.get("daily", {}), d.get("hourly", {})
    code = (daily.get("weather_code") or [0])[0]
    win = []
    for i, t in enumerate(hourly.get("time", [])):
        if t[11:13] in ("07", "08", "09"):
            pr = (hourly.get("precipitation") or [])
            pp = (hourly.get("precipitation_probability") or [])
            win.append({"t": t[11:16], "mm": pr[i] if i < len(pr) else None,
                        "prob": pp[i] if i < len(pp) else None})
    return {"summary": WMO.get(code, "?"), "icon": WICON.get(code, ""),
            "tmax": (daily.get("temperature_2m_max") or [None])[0],
            "tmin": (daily.get("temperature_2m_min") or [None])[0],
            "precip_sum": (daily.get("precipitation_sum") or [0])[0], "commute_window": win}

def rss(url, max_age_h, limit):
    try: root = ET.fromstring(_get(url))
    except Exception: return []
    now, items = time.time(), []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        pub = it.findtext("pubDate")
        if pub:
            try:
                if now - email.utils.parsedate_to_datetime(pub).timestamp() > max_age_h * 3600:
                    continue
            except Exception: pass
        if title:
            items.append({"title": title, "link": link})
        if len(items) >= limit: break
    return items

def news():
    base = "https://news.google.com/rss/headlines/section/topic/%s?hl=en-GB&gl=GB&ceid=GB:en"
    return {"world": rss(base % "WORLD", 14, 12), "business": rss(base % "BUSINESS", 14, 12)}

def local_news(place):
    if not place: return []
    q = urllib.parse.quote("%s (flood OR fire OR closure OR strike OR evacuation OR storm OR crash "
                           "OR incident OR disruption OR police)" % place)
    return rss("https://news.google.com/rss/search?q=%s&hl=en-GB&gl=GB&ceid=GB:en" % q, 16, 8)

def _gh(path):
    r = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=30)
    if r.returncode != 0: return None
    try: return json.loads(r.stdout)
    except Exception: return None

def git_overnight(hours=18):
    since = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    repos = _gh("/user/repos?sort=pushed&per_page=20&affiliation=owner,organization_member,collaborator") or []
    mine = {m.lower() for m in ME}
    out = []
    for r in repos:
        full = r.get("full_name")
        if not full: continue
        for c in (_gh("/repos/%s/commits?since=%s&per_page=20" % (full, since)) or []):
            login = ((c.get("author") or {}).get("login") or "").lower()
            cm = c.get("commit") or {}
            if login in mine: continue
            who = login or (cm.get("author") or {}).get("name", "?")
            out.append({"repo": full, "who": who, "msg": (cm.get("message") or "").splitlines()[0][:100]})
    return out

def tidy(b):
    """Enforce the dense layout regardless of agent variance: strip a self-written #valet (the
    transport re-adds it inline → one-line header), un-double bracketed link labels, and collapse
    blank lines so the brief stays tight."""
    import re
    b = (b or "").strip()
    b = re.sub(r"^#valet\b[:\s]*", "", b)            # transport prepends the tag inline
    b = re.sub(r"\[\[([^\]]+)\]\]", r"[\1]", b)      # [[BBC]] -> [BBC]
    b = re.sub(r"\n[ \t]*\n+", "\n", b)              # no blank lines
    return b.strip()

def morning():
    location_pull.pull()                       # refresh from the 3am gist
    loc = location.resolve() or {}
    lat, lon, place = loc.get("lat"), loc.get("lon"), loc.get("name")
    bundle = {
        "now": time.strftime("%a %d %b, %H:%M"),
        "location_label": location.label(loc), "where": place, "status": loc.get("status"),
        "weather": weather(lat, lon) if lat else {"error": "no location"},
        "news": news(), "local": local_news(place), "repos": git_overnight(),
    }
    prompt = (
        "Compose Mikael's morning brief as THREE compact blocks, in this order, each opening with a "
        "bold lead-in and separated by a SINGLE newline (no blank lines):\n"
        "1) GENERAL NEWS — top overnight item + 1-2 inlined 'Also' items (from DATA.news), your usual "
        "geopolitics/markets bias.\n"
        "2) LOCAL CONDITIONS — the weather, putting DATA.weather.icon immediately before the condition "
        "word; mention rain ONLY if likely in the 07:30-09:00 window — folded with any SERIOUS local "
        "event (DATA.local). Add the day's repo line here if DATA.repos has any.\n"
        "3) ENGAGEMENTS — Mikael's overnight PERSONAL email + today's calendar via the google MCP: call "
        "search_gmail_messages query 'in:inbox newer_than:1d' (always pass user_google_email "
        "'mikael@odinlake.net'), read the promising ones, and surface ONLY genuinely personal messages a "
        "human wrote FOR Mikael (ignore newsletters, receipts, notifications, automated/marketing, list "
        "mail) as 'sender — gist'. Then today's calendar events via the calendar tools. If there's no "
        "personal mail and no events, say so in a few words; never invent.\n"
        "Lead with the '@ <location>' tag line. Dense, narrow gaps, one subtle wink at most; drop a whole "
        "block only if it's genuinely empty. Start with #valet.\n\nDATA (news/weather/local/repos):\n%s"
        % json.dumps(bundle, ensure_ascii=False))
    brief = tidy(run_agent("valet", prompt, timeout=480))
    tg.send(brief, agent="valet")              # tg logs it to the shared conversation
    print("valet: brief sent (%d chars)" % len(brief))

def review():
    """Afternoon catch-up (16:15): scan ~2 weeks of mail for neglected items; stay SILENT if nothing."""
    import re
    prompt = (
        "Afternoon catch-up for Mikael. Using the google MCP (search_gmail_messages, "
        "get_gmail_message_content; always pass user_google_email 'mikael@odinlake.net'), scan roughly "
        "the last TWO WEEKS of email for anything he may have NEGLECTED: personal messages still awaiting "
        "a reply, requests/asks left hanging, approaching deadlines, things that seem to have slipped. "
        "Judge significance strictly; ignore newsletters / automated / marketing / notifications.\n"
        "If there is nothing genuinely worth flagging, reply with EXACTLY 'NONE' and nothing else. "
        "Otherwise give a SHORT list (max ~5), most-urgent first, each one line: "
        "'sender — what's owed / why it matters'. Be a quiet useful nudge, not noise. Start with #valet.")
    out = (run_agent("valet", prompt, timeout=600) or "").strip()
    core = re.sub(r"(?i)^#valet[:\s]*", "", out).strip().rstrip(".")
    if not core or core.upper() == "NONE":
        print("valet review: nothing significant — staying quiet"); return
    tg.send(tidy(out), agent="valet")
    print("valet review: sent (%d chars)" % len(out))

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "morning"
    (review if mode == "review" else morning)()

if __name__ == "__main__":
    main()
