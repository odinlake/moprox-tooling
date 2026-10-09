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
    bookscout table [--subject S] [--min-interest high|medium] [--audible] [--days N] [--json]
                                       every recorded book as a table (latest verdict per book)
    bookscout run --backfill N         judge Culture articles from the last N days of the index

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
# What the operator said they want (2026-10-06): "all kinds of history, biographies and the best
# polemics". Polemics only when the review is strong. Shown to the judge and in `profile`.
STATED = ("all kinds of history (any period, any region), biographies and memoirs of consequential "
          "people, and the BEST polemics (only when the review is strongly positive)")
SUBJECTS = ["history", "biography", "memoir", "polemic", "politics", "economics", "science",
            "technology", "nature", "health", "philosophy", "religion", "society", "arts", "music",
            "food", "travel", "sport", "business", "war", "literary fiction", "genre fiction",
            "poetry", "true crime"]
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


def owned_asins():
    """The asins already in the library, or None if the library could not be read.

    worthy() uses this instead of trusting the judge's `interest: "owned"` string. None means
    "cannot certify", and worthy() then admits nothing: an unreadable library must not quietly
    reinstate the possibility of suggesting the operator a book off their own shelf."""
    try:
        return {t["asin"] for t in library()}
    except Exception as exc:
        errlog.err("bookscout: cannot read the Audible library at %s, so no suggestion can be "
                   "certified unowned — none will be made this pass" % LIBRARY, exc)
        return None


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
    return {"stated_interests": STATED, "titles": len(t), "finished": len(fin), "top_genres": g.most_common(25),
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


def index_items(days):
    """Culture articles from the local Economist index (webscout search_articles), for backfill. The
    index matches words in title/abstract only, so a few broad terms are unioned."""
    import webscout
    cut = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    seen, out = set(), []
    for q in ("book", "books", "novel", "novels", "memoir", "biography", "history", "author",
              "writer", "historian", "argues", "polemic", "read"):
        r = json.loads(webscout.call("search_articles", {"query": q, "limit": 100}, timeout=120))
        for h in r.get("hits") or []:
            if h.get("section") == "culture" and h["date"] >= cut and h["url"] not in seen:
                seen.add(h["url"])
                out.append({"url": h["url"], "title": h["title"], "abstract": h.get("abstract") or "",
                            "date": h["date"]})
    return sorted(out, key=lambda a: a["date"])


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
4. Sentiment of the REVIEW toward the book: rave | positive | mixed | negative | unclear, with a
   <=20-word note in your own words. On abstract alone be careful: "less of Agrippa than previous
   works" is mixed, and most abstracts only support "unclear".
5. Tags, two kinds:
   - subjects: 1-3 from EXACTLY this list: {subjects}
     A book arguing a case against something is "polemic" as well as its field.
   - topics: 3-6 free lowercase tags, specific (e.g. "roman republic", "soviet gulag", "soil ecology").
   - period and place when it is history or biography (e.g. "1st century BC", "Rome"); else null.
6. Interest for THIS listener. Their stated interests: {stated}. Then their library, from `bookscout
   profile` (and `library` for the author/series): high | medium | low with a <=20-word reason that
   cites the stated interest or the library (an author they own, a genre they finish). A polemic is
   "high" only with a rave. Already owned => interest "owned".

Reply with ONLY minified JSON:
{{"books":[{{"title":"","author":"","name_source":"text|abstract|title|inferred","asin":null,
"audible_title":null,"runtime_min":null,"rating":null,"match":"exact|likely|none","sentiment":"",
"sentiment_note":"","subjects":[],"topics":[],"period":null,"place":null,
"interest":"high|medium|low|owned","interest_reason":""}}]}}"""


def judge(art, text):
    import run
    body = ("Review text:\n" + text) if text else "(no review text: judge from title and abstract only)"
    out = run.run_agent("bard-books", PROMPT.format(has_text=bool(text), body=body, stated=STATED,
                                                    subjects=", ".join(SUBJECTS), **art), timeout=900)
    i, j = out.find("{"), out.rfind("}")
    if i < 0:
        raise ValueError("no JSON in bard's answer: %r" % out[:200])
    return json.loads(out[i:j + 1]).get("books") or []


def _norm(s):
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", (s or "").lower().replace("’", "'")).split())


def named_in(art, b):
    """Does the ARTICLE name this book? Returns "title", "abstract" or None.

    Provenance computed from the article instead of asked of the judge. The Economist puts a book
    title in typographic quotes, so the question is decidable: the main title (everything before a
    ':') appearing as a quoted span in the article's own title or abstract.

    This replaces `name_source` in the gate below. Measured over the 56 book entries of the
    2026-10-09 record -- where `text` is false on all 128 articles, so the provenance conjunct is
    the only thing standing between an abstract-only verdict and Telegram -- it reproduces the
    judge's `title` (3/3, each quoted in the article title and in no abstract) and `abstract`
    (10/10) labels exactly, including WHICH of the two, and returns None on 38 of the 40
    `inferred`. It disagrees on 5: 3 labelled `text`, a provenance the judge cannot have had, and
    2 labelled `inferred` -- and in all 5 the abstract does quote the title. None of the 5 flips
    this gate today (no entry in the record passes all four non-provenance conjuncts), so this is
    the same verdicts from a field that cannot contradict its own input.
    See moprox-memory/bookscout-provenance-is-computable.
    """
    main = _norm((b.get("title") or "").split(":")[0])
    if not main:
        return None
    for field in ("title", "abstract"):
        if main in [_norm(q) for q in re.findall(r'[“"]([^“”"]+)[”"]', art.get(field) or "")]:
            return field
    return None


def worthy(b, art, has_text, owned):
    """`owned` is owned_asins(): a set of asins, or None for "could not be read".

    Ownership is checked here and not left to `interest == "high"`. The judge is told to answer
    `interest: "owned"` for a book the operator already has, so until now the docstring's "not
    already owned" was one token of model output deep -- `Flesh` (B0DKG91X7C) is in the library
    and is recorded exact/positive/title, i.e. every other conjunct already satisfied.

    Provenance is likewise computed (`named_in`) and not taken from the judge's `name_source`,
    which stays in the record as description only."""
    return (b.get("asin") and owned is not None and b["asin"] not in owned
            and b.get("match") == "exact" and b.get("sentiment") in ("rave", "positive")
            and b.get("interest") == "high"
            and (has_text or named_in(art, b) is not None))


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


def run_pass(days, dry, backfill=0):
    st = load_state()
    owned = owned_asins()                        # once per pass, not once per book
    src = index_items(backfill) if backfill else culture_items(days)
    arts = [a for a in src if a["url"] not in st["seen"]]
    quiet = bool(backfill)                       # a backfill fills the record; it never pings Telegram
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
            if worthy(b, art, bool(text), owned) and b["asin"] not in st["suggested"]:
                picks.append((b, art))
        save_state(st)
    picks = picks[:MAX_SUGGEST]
    if picks and not dry and not quiet:
        send("From The Economist's culture pages, worth a listen:\n\n" +
             "\n\n".join(suggestion(b, a) for b, a in picks))
        st["suggested"] += [b["asin"] for b, _ in picks]
        save_state(st)
    print("bookscout: %d new article(s), %d book(s), %d suggested%s"
          % (len(arts), n_books, len(picks), " (dry run)" if dry else ""), flush=True)


RANK = {"high": 0, "owned": 1, "medium": 2, "low": 3}


def rows():
    """Latest verdict per (book, author): a re-judged article supersedes its earlier record."""
    if not RECORD.exists():
        return []
    best = {}
    for ln in RECORD.read_text().splitlines():
        r = json.loads(ln)
        for b in r.get("books") or []:
            k = ((b.get("title") or "").lower(), (b.get("author") or "").lower())
            best[k] = {**b, "date": r["date"], "article": r["title"], "url": r["url"], "text": r["text"]}
    return list(best.values())


def table(subject=None, min_interest=None, audible=False, days=None, as_json=False):
    rs = rows()
    if subject:
        rs = [b for b in rs if subject.lower() in [x.lower() for x in (b.get("subjects") or []) + (b.get("topics") or [])]]
    if min_interest:
        rs = [b for b in rs if RANK.get(b.get("interest"), 9) <= RANK[min_interest]]
    if audible:
        rs = [b for b in rs if b.get("asin") and b.get("match") in ("exact", "likely")]
    if days:
        cut = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
        rs = [b for b in rs if b["date"] >= cut]
    rs.sort(key=lambda b: (RANK.get(b.get("interest"), 9), b["date"]), reverse=False)
    if as_json:
        return json.dumps(rs, ensure_ascii=False, indent=1)
    if not rs:
        return "No recorded books match."
    def aud(b):
        if not b.get("asin") or b.get("match") == "none":
            return "-"
        h = "%dh%02d" % divmod(b["runtime_min"], 60) if b.get("runtime_min") else ""
        return " ".join(x for x in (h, ("★%s" % b["rating"]) if b.get("rating") else "",
                                    "?" if b.get("match") == "likely" else "") if x) or "yes"
    head = "| Book | Author | Subjects | Topics | Review | Interest | Audible | Reviewed |\n|---|---|---|---|---|---|---|---|"
    lines = ["| %s | %s | %s | %s | %s%s | %s | %s | %s |" % (
        b.get("audible_title") or b.get("title"), b.get("author"), ", ".join(b.get("subjects") or []),
        ", ".join((b.get("topics") or [])[:3]), b.get("sentiment"), "" if b.get("text") else "*",
        b.get("interest"), aud(b), b["date"]) for b in rs]
    return head + "\n" + "\n".join(lines) + "\n\n* judged on the abstract only. Audible ? = likely match."


def main():
    ap = argparse.ArgumentParser(prog="bookscout")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("--dry", action="store_true"); r.add_argument("--days", type=int, default=10)
    r.add_argument("--backfill", type=int, default=0)
    t = sub.add_parser("table"); t.add_argument("--subject"); t.add_argument("--min-interest", choices=["high", "medium"])
    t.add_argument("--audible", action="store_true"); t.add_argument("--days", type=int); t.add_argument("--json", action="store_true")
    a = sub.add_parser("audible"); a.add_argument("keywords", nargs="?", default="")
    a.add_argument("--title", default=""); a.add_argument("--author", default="")
    l = sub.add_parser("library"); l.add_argument("rx")
    sub.add_parser("profile")
    rc = sub.add_parser("recent"); rc.add_argument("n", nargs="?", type=int, default=15)
    x = ap.parse_args()
    if x.cmd == "run":
        return run_pass(x.days, x.dry, x.backfill)
    if x.cmd == "table":
        return print(table(x.subject, x.min_interest, x.audible, x.days, x.json))
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
