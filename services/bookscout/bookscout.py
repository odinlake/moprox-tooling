#!/usr/bin/env python3
"""bookscout — books reviewed in The Economist's Culture section, checked against Audible and the
operator's own library; the good ones suggested on Telegram by bard, conservatively.

Asked for 2026-10-06: review new Culture articles, identify book recommendations, check Audible
availability, tag topics, note the review's sentiment, cross-reference the Audible library for
particular interest, and suggest only the good ones.

    bookscout run [--dry] [--days N]   one pass over the Culture feed (the timer runs this)
    bookscout audible "<keywords>" [--title T] [--author A]   Audible UK catalogue search, JSON
    bookscout library "<regex>"        matching titles in the operator's Audible library, JSON
    bookscout profile                  the library's taste profile (genres, authors, recent finishes)
    bookscout recent [N]               the last N reviewed books, with verdicts

HOW ONE ARTICLE IS HANDLED
  1. Culture RSS via webscout's `feed` (one request a day; feeds are allowed traffic, see below).
  2. The review TEXT via webscout `read`, at most one attempt per run and none for 24 h after a
     block. economist.com has been behind a DataDome block on the house IP since 2026-09-28, so
     most reviews are judged on title + abstract only, and say so (`text: false`).
  3. bard in `bard-books` mode (run.py) identifies the book(s), searches Audible with
     `bookscout audible`, tags topics, reads the review's sentiment and weighs the operator's
     library (`bookscout profile` / `library`). It returns JSON; this file never trusts its prose.
  4. Every verdict is appended to private-data/books/economist-culture.jsonl (the record).
  5. Telegram, as #bard, ONLY for books that are on Audible with a confident match, reviewed
     positively, of high interest, and not already owned. At most MAX_SUGGEST per run; nothing
     at all on a quiet week. A book judged on the abstract alone must be named in it to qualify.

ECONOMIST TRAFFIC RULES (operator): automatic traffic is feeds and abstracts; full articles only
when asked. This service IS the ask for Culture reviews, so it reads them, but one at a time and
backing off on a block (see economist-datadome-block in shared memory).
"""
import argparse, collections, datetime, json, os, re, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1] / "lib"))
sys.path.insert(0, str(HERE.parents[1] / "forward"))
sys.path.insert(0, str(HERE.parents[1] / "agents"))
import errlog  # noqa: E402

FEED = "https://www.economist.com/culture/rss.xml"
LIBRARY = Path.home() / "projects/private-data/audible/library.json"
RECORD = Path.home() / "projects/private-data/books/economist-culture.jsonl"
STATE = Path.home() / ".local/state/bookscout/state.json"
AUTH = Path.home() / ".config/claude-dev/audible/auth.json"
MAX_SUGGEST = 2
BLOCK_BACKOFF_S = 24 * 3600
AUDIBLE_PD = "https://www.audible.co.uk/pd/%s"


# --- Audible ------------------------------------------------------------------------------------

def _client():
    import audible
    from tokenlock import token_lock
    with token_lock(AUTH):                       # rotating-token rule: refresh only under the lock
        auth = audible.Authenticator.from_file(AUTH)
        if auth.access_token_expired:
            auth.refresh_access_token()
            auth.to_file(AUTH, encryption=False)
    return audible.Client(auth=auth, timeout=60)


def library():
    return json.loads(LIBRARY.read_text())["titles"]


def audible_search(keywords="", title="", author="", n=5):
    owned = {t["asin"] for t in library()}
    q = {k: v for k, v in (("keywords", keywords), ("title", title), ("author", author)) if v}
    with _client() as c:
        r = c.get("1.0/catalog/products", num_results=n, products_sort_by="Relevance",
                  response_groups="contributors,product_attrs,product_desc,rating,category_ladders,"
                                  "product_extended_attrs", **q)
    out = []
    for p in r.get("products") or []:
        rating = ((p.get("rating") or {}).get("overall_distribution") or {})
        out.append({"asin": p.get("asin"), "title": p.get("title"), "subtitle": p.get("subtitle"),
                    "authors": [a["name"] for a in p.get("authors") or []],
                    "narrators": [a["name"] for a in p.get("narrators") or []],
                    "released": p.get("release_date"), "runtime_min": p.get("runtime_length_min"),
                    "language": p.get("language"), "rating": rating.get("display_average_rating"),
                    "ratings": rating.get("num_ratings"), "owned": p.get("asin") in owned,
                    "url": AUDIBLE_PD % p.get("asin"),
                    "summary": re.sub(r"<[^>]+>", " ", p.get("merchandising_summary") or "")[:300]})
    return out


def profile():
    t = library()
    g = collections.Counter(x for r in t for x in r.get("genres") or [])
    a = collections.Counter(x for r in t for x in r.get("authors") or [])
    fin = sorted((r for r in t if r.get("finished")), key=lambda r: r.get("purchased") or "", reverse=True)
    return {"titles": len(t), "finished": len(fin), "top_genres": g.most_common(25),
            "top_authors": a.most_common(30),
            "recent_finished": ["%s — %s" % (r["title"], ", ".join(r.get("authors") or [])) for r in fin[:40]],
            "recent_purchases": ["%s — %s" % (r["title"], ", ".join(r.get("authors") or [])) for r in t[:25]]}


def library_grep(rx):
    p = re.compile(rx, re.I)
    return [{k: r.get(k) for k in ("asin", "title", "authors", "genres", "finished", "percent_complete")}
            for r in library() if p.search(r["title"] + " " + " ".join(r.get("authors") or []))][:30]


# --- Economist ----------------------------------------------------------------------------------

def load_state():
    try:
        return json.loads(STATE.read_text())
    except FileNotFoundError:
        return {"seen": {}, "suggested": [], "blocked_until": 0}


def save_state(s):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=1, ensure_ascii=False))
    tmp.replace(STATE)


def culture_items(days):
    import webscout
    items = json.loads(webscout.call("feed", {"url": FEED, "limit": 40}, timeout=120))
    cut = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    out = []
    for it in items:
        try:
            d = datetime.datetime.strptime(it["date"], "%a, %d %b %Y %H:%M:%S %z")
        except Exception:
            d = cut
        if d >= cut and "/culture/" in it["link"]:
            out.append({"url": it["link"], "title": it["title"], "abstract": it.get("summary") or "",
                        "date": d.date().isoformat()})
    return out


def review_text(url, st):
    """The article body, or None. One try, and none at all inside the block back-off."""
    if time.time() < st.get("blocked_until", 0):
        return None
    import webscout
    try:
        t = webscout.call("read", {"url": url}, timeout=200)
        if t and len(t) > 1500 and "DataDome" not in t:
            return t[:20000]
        return None
    except Exception as e:
        if "DataDome" in str(e) or "bot check" in str(e):
            st["blocked_until"] = time.time() + BLOCK_BACKOFF_S
            print("economist still behind DataDome; no text fetches for 24 h", flush=True)
        else:
            errlog.warn("bookscout: read failed for %s: %s" % (url, e))
        return None


PROMPT = """BOOK SCOUT (see "The operator's own entertainment" in your CLAUDE.md). Judge one Economist
Culture article. You may run `bookscout audible`, `bookscout library` and `bookscout profile`.

Article: {title}
Date: {date}   URL: {url}
Abstract: {abstract}
Full text available: {has_text}
{body}

Do this:
1. Decide whether it reviews or recommends specific BOOKS. Film/TV/food/music pieces with no book:
   return {{"books": []}}. A round-up ("eight best novels") can have several.
2. For each book: exact title and author. If you only have the abstract and it does not NAME the
   book, you may identify it through `bookscout audible` (a new book on that subject released within
   ~4 months of the article date), but then name_source must be "inferred" and match "likely" at
   best. Never guess silently.
3. Search Audible for it (`bookscout audible "<title> <author>" --title "<title>" --author "<author>"`).
   match = "exact" only if title AND author agree; "likely"; or "none" (not on Audible UK).
4. Sentiment of the REVIEW toward the book: positive | mixed | negative | unclear, with a <=20-word
   note in your own words. On abstract alone be careful: "less of Agrippa than previous works" is mixed.
5. 3-6 lowercase topic tags (e.g. "roman history", "biography", "neuroscience", "literary fiction").
6. Interest for THIS listener from `bookscout profile` (and `library` for the author/series):
   high | medium | low with a <=20-word reason that cites the library (an author they own, a genre they
   finish). Already owned => interest "owned".

Reply with ONLY minified JSON:
{{"books":[{{"title":"","author":"","name_source":"text|abstract|title|inferred","asin":null,
"audible_title":null,"runtime_min":null,"rating":null,"match":"exact|likely|none","sentiment":"",
"sentiment_note":"","topics":[],"interest":"high|medium|low|owned","interest_reason":""}}]}}"""


def judge(art, text):
    import run
    body = ("Review text:\n" + text) if text else "(no review text: judge from title and abstract only)"
    out = run.run_agent("bard-books", PROMPT.format(has_text=bool(text), body=body, **art), timeout=900)
    i, j = out.find("{"), out.rfind("}")
    if i < 0:
        raise ValueError("no JSON in bard's answer: %r" % out[:200])
    return json.loads(out[i:j + 1]).get("books") or []


def worthy(b, has_text):
    return (b.get("asin") and b.get("match") == "exact" and b.get("sentiment") == "positive"
            and b.get("interest") == "high"
            and (has_text or b.get("name_source") in ("abstract", "title")))


def suggestion(b, art):
    hrs = "%d h %02d m" % divmod(b["runtime_min"], 60) if b.get("runtime_min") else ""
    meta = ", ".join(x for x in (hrs, ("★%s" % b["rating"]) if b.get("rating") else "") if x)
    return ("📚 *%s* by %s%s\n_%s_ — %s\nWhy you: %s\n[Audible](%s) · [review](%s)"
            % (b.get("audible_title") or b["title"], b["author"], " (%s)" % meta if meta else "",
               art["title"], b["sentiment_note"], b["interest_reason"], AUDIBLE_PD % b["asin"], art["url"]))


def send(text):
    """Through tg.py as #bard. tg needs telegramify-markdown, which lives in the system python's user
    site and not in the audible venv this runs under, so it goes via a subprocess."""
    code = ("import sys; sys.path.insert(0, %r); import tg; tg.send(sys.stdin.read(), agent='bard')"
            % str(HERE.parents[1] / "forward"))
    subprocess.run(["/usr/bin/python3", "-c", code], input=text, text=True, check=True, timeout=60)


def run_pass(days, dry):
    st = load_state()
    arts = [a for a in culture_items(days) if a["url"] not in st["seen"]]
    picks, n_books = [], 0
    for art in arts:
        text = review_text(art["url"], st) if not dry else None
        try:
            books = judge(art, text)
        except Exception as e:
            errlog.err("bookscout: judging %r failed" % art["title"], e)
            continue                             # unseen, so retried next run
        n_books += len(books)
        rec = {"ts": datetime.datetime.now().isoformat(timespec="seconds"), **art, "text": bool(text),
               "books": books}
        print(json.dumps({"article": art["title"], "books": [(b.get("title"), b.get("match"), b.get("sentiment"),
                          b.get("interest")) for b in books]}, ensure_ascii=False), flush=True)
        if dry:
            continue
        RECORD.parent.mkdir(parents=True, exist_ok=True)
        with open(RECORD, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        st["seen"][art["url"]] = {"ts": rec["ts"], "text": bool(text), "books": len(books)}
        for b in books:
            if worthy(b, bool(text)) and b["asin"] not in st["suggested"]:
                picks.append((b, art))
        save_state(st)
    picks = picks[:MAX_SUGGEST]
    if picks and not dry:
        send("From The Economist's culture pages, worth a listen:\n\n" +
             "\n\n".join(suggestion(b, a) for b, a in picks))
        st["suggested"] += [b["asin"] for b, _ in picks]
        save_state(st)
    print("bookscout: %d new article(s), %d book(s), %d suggested%s"
          % (len(arts), n_books, len(picks), " (dry run)" if dry else ""), flush=True)


def main():
    ap = argparse.ArgumentParser(prog="bookscout")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("--dry", action="store_true"); r.add_argument("--days", type=int, default=10)
    a = sub.add_parser("audible"); a.add_argument("keywords", nargs="?", default="")
    a.add_argument("--title", default=""); a.add_argument("--author", default="")
    l = sub.add_parser("library"); l.add_argument("rx")
    sub.add_parser("profile")
    rc = sub.add_parser("recent"); rc.add_argument("n", nargs="?", type=int, default=15)
    x = ap.parse_args()
    if x.cmd == "run":
        return run_pass(x.days, x.dry)
    if x.cmd == "audible":
        out = audible_search(x.keywords, x.title, x.author)
    elif x.cmd == "library":
        out = library_grep(x.rx)
    elif x.cmd == "profile":
        out = profile()
    else:
        rows = [json.loads(l) for l in RECORD.read_text().splitlines()] if RECORD.exists() else []
        out = [{"date": r["date"], "article": r["title"], "text": r["text"], "books": r["books"]}
               for r in rows[-x.n:]]
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
