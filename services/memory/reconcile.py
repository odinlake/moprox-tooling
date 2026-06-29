#!/usr/bin/env python3
"""Deterministic memory reconciler for the shared moprox-dev agent memory.

The three moprox-dev sessions share ONE memory dir. Each writes fact files normally and appends a
one-line journal entry per change. This pass (run by a timer, no LLM, no tokens) rebuilds the index
AUTHORITATIVELY from the fact files — which also recovers from any concurrent-write race on the index
itself — and does the housekeeping the agents shouldn't fight over:

  * provenance-preserving merge  — `agents:` lists only ever GROW (never lose who learned a fact)
  * supersession + TTL           — superseded / expired facts move to archive/ (kept, not deleted)
  * stable, budgeted hot index   — MEMORY.md is capped in size, ordered stably (alpha within scope),
                                   with hysteresis so membership only changes on real change — this
                                   protects prompt-caching + attention (the "keep the hot set small
                                   AND stable" lesson)
  * conflict detection           — same slug touched by >=2 agents within a window, divergently,
                                   gets flagged to CONFLICTS.md for the steward (never auto-deleted)
  * CHANGES.md                   — a compact recent-activity feed the agents read at task start
                                   (the "active" cross-pollination pattern)

Scope (`global` vs `project:<name>`) drives SECTIONING + priority in the single shared index — every
agent sees the same curated headlines (that's the point: don't re-explain), details stay cold.

Usage: reconcile.py [--dry-run]   (env MEMORY_DIR overrides the default path)
"""
import json, os, re, sys, datetime, shutil

MEMORY_DIR = os.environ.get("MEMORY_DIR", os.path.expanduser("~/.claude/projects/-home-mikael/memory"))
BUDGET_BYTES = int(os.environ.get("MEMORY_BUDGET", "9000"))   # cap on the always-loaded index
CONFLICT_DAYS = 2
DRY = "--dry-run" in sys.argv
TODAY = datetime.date.fromisoformat(os.environ["TODAY"]) if os.environ.get("TODAY") else None
GLYPH = {"one": "①", "two": "②", "three": "③"}
TIER = {"high": 3, "normal": 2, "low": 1}

def today():
    return TODAY or datetime.date.today()

# ---------- tolerant frontmatter parse (no YAML dep) ----------
def parse(path):
    txt = open(path, encoding="utf-8", errors="ignore").read()
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", txt, re.S)
    if not m: return None
    fm, body = m.group(1), m.group(2)
    d, cur = {}, d if False else {}
    meta = {}
    in_meta = False
    for ln in fm.splitlines():
        if re.match(r"^metadata:\s*$", ln): in_meta = True; continue
        m2 = re.match(r"^(\s*)([\w-]+):\s*(.*)$", ln)
        if not m2: continue
        indent, k, v = m2.group(1), m2.group(2), m2.group(3).strip().strip('"')
        (meta if (in_meta and indent) else d)[k] = v
        if not indent: in_meta = False
    def lst(v): return [x.strip() for x in re.sub(r"[\[\]]", "", v).split(",") if x.strip()] if v else []
    mt = os.path.getmtime(path)
    mtd = datetime.date.fromtimestamp(mt)
    return {
        "file": os.path.basename(path),
        "name": d.get("name") or os.path.splitext(os.path.basename(path))[0],
        "desc": d.get("description", "").strip().strip('"'),
        "type": meta.get("type", "reference"),
        "scope": meta.get("scope") or ("global" if meta.get("type") in ("user", "feedback", "reference") else "global"),
        "agents": lst(meta.get("agents", "")),
        "salience": meta.get("salience", "normal"),
        "supersedes": meta.get("supersedes", "").strip() or None,
        "expires": meta.get("expires", "").strip() or None,
        "last_used": meta.get("last_used", "").strip() or mtd.isoformat(),
        "ts": meta.get("ts", "").strip() or mtd.isoformat(),
        "_raw": txt,
    }

def load_journals():
    jdir = os.path.join(MEMORY_DIR, "journals")
    entries = []
    if os.path.isdir(jdir):
        for f in sorted(os.listdir(jdir)):
            if not f.endswith(".jsonl"): continue
            for ln in open(os.path.join(jdir, f), errors="ignore"):
                ln = ln.strip()
                if not ln: continue
                try: entries.append(json.loads(ln))
                except Exception: pass
    return entries

def archive(file, reason):
    src = os.path.join(MEMORY_DIR, file)
    dst = os.path.join(MEMORY_DIR, "archive", file)
    print(f"  archive {file}  ({reason})")
    if DRY or not os.path.exists(src): return
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.move(src, dst)

def main():
    facts = {}
    for f in sorted(os.listdir(MEMORY_DIR)):
        if not f.endswith(".md") or f in ("MEMORY.md", "CHANGES.md", "CONFLICTS.md"): continue
        p = parse(os.path.join(MEMORY_DIR, f))
        if p: facts[p["name"]] = p

    journ = load_journals()
    # 1) fold journal facts into metadata: provenance union, recency, scope/salience hints, supersedes
    touched = {}
    for e in journ:
        slug = e.get("slug");  ag = e.get("agent")
        if not slug: continue
        touched.setdefault(slug, []).append(e)
        fct = facts.get(slug)
        if fct:
            if ag and ag not in fct["agents"]: fct["agents"].append(ag)
            if e.get("ts"): fct["last_used"] = max(fct["last_used"], e["ts"])
            if e.get("scope"): fct["scope"] = e["scope"]
            if e.get("salience"): fct["salience"] = e["salience"]
            if e.get("supersedes"): fct["supersedes"] = e["supersedes"]

    # 2) supersession + 3) TTL expiry -> archive (kept, not deleted)
    for fct in list(facts.values()):
        if fct["supersedes"] and fct["supersedes"] in facts and fct["supersedes"] != fct["name"]:
            old = facts.pop(fct["supersedes"]); archive(old["file"], f"superseded by {fct['name']}")
        if fct["expires"]:
            try:
                if datetime.date.fromisoformat(fct["expires"]) < today():
                    facts.pop(fct["name"], None); archive(fct["file"], "expired " + fct["expires"])
            except ValueError: pass

    # 4) conflict detection: a real conflict is >=2 agents UPDATING the same existing fact within the
    #    window (divergent edits) — not collaboration (one adds, another touches).
    conflicts = []
    cutoff = (today() - datetime.timedelta(days=CONFLICT_DAYS)).isoformat()
    for slug, es in touched.items():
        upd = {e.get("agent") for e in es if e.get("ts", "0") >= cutoff and e.get("action") == "update" and e.get("agent")}
        if len(upd) >= 2 and slug in facts and "resolved" not in facts[slug]["agents"]:
            conflicts.append((slug, sorted(upd)))

    # 5) write provenance back into fact files (only when agents list changed)
    for fct in facts.values():
        want = "agents: [%s]" % ", ".join(fct["agents"]) if fct["agents"] else None
        if want and ("agents:" not in fct["_raw"] or sorted(fct["agents"]) and want not in fct["_raw"]):
            new = re.sub(r"(\n\s+)agents:.*", r"\g<1>" + want, fct["_raw"]) if "agents:" in fct["_raw"] \
                  else re.sub(r"(\n\s+type:.*)", r"\1\n  " + want, fct["_raw"], count=1)
            if new != fct["_raw"] and not DRY:
                open(os.path.join(MEMORY_DIR, fct["file"]), "w", encoding="utf-8").write(new)

    # 6) build the stable, budgeted, scoped index
    state_path = os.path.join(MEMORY_DIR, ".reconcile-state.json")
    prev_hot = set(json.load(open(state_path)).get("hot", [])) if os.path.exists(state_path) else set()

    def priority(f):  # higher = keep. salience, then hysteresis bonus for already-hot, then recency
        return (TIER.get(f["salience"], 2), 1 if f["name"] in prev_hot else 0, f["last_used"])
    ranked = sorted(facts.values(), key=priority, reverse=True)
    hot, used = [], 0
    for f in ranked:
        line_len = 80 + len(f["desc"])
        if used + line_len <= BUDGET_BYTES:
            hot.append(f["name"]); used += line_len
    hotset = set(hot)
    cold = [f for f in facts.values() if f["name"] not in hotset]

    # group hot by scope, stable alpha order within a scope (quiet diffs -> prompt-cache friendly)
    def glyphs(f): return "".join(GLYPH.get(a, "") for a in f["agents"]) or "·"
    scopes = {}
    for f in facts.values():
        if f["name"] in hotset: scopes.setdefault(f["scope"], []).append(f)
    out = ["# Memory index", "",
           "<!-- built by services/memory/reconcile.py — do not hand-edit; add facts as files + journal -->", ""]
    order = ["global"] + sorted(s for s in scopes if s != "global")
    for sc in order:
        if sc not in scopes: continue
        out.append(f"## {'Global' if sc=='global' else sc}")
        for f in sorted(scopes[sc], key=lambda x: x["name"]):
            out.append(f"- [{f['name']}]({f['file']}) — {f['desc']} {glyphs(f)}".rstrip())
        out.append("")
    if cold:
        out.append(f"## Cold ({len(cold)} more — search the archive / recall by name)")
        out.append("")
    idx = "\n".join(out).rstrip() + "\n"

    print(f"reconcile: {len(facts)} facts, {len(hot)} hot ({used}/{BUDGET_BYTES} B), {len(cold)} cold, "
          f"{len(conflicts)} conflicts" + (" [dry-run]" if DRY else ""))
    if not DRY:
        open(os.path.join(MEMORY_DIR, "MEMORY.md"), "w", encoding="utf-8").write(idx)
        json.dump({"hot": sorted(hotset), "ts": today().isoformat()}, open(state_path, "w"))
        # CHANGES.md — recent activity for the agents' task-start "active read"
        recent = sorted(journ, key=lambda e: e.get("ts", ""), reverse=True)[:30]
        ch = ["# Recent memory changes (newest first)", ""]
        for e in recent:
            ch.append(f"- {e.get('ts','?')} {GLYPH.get(e.get('agent'),'·')} **{e.get('action','add')}** "
                      f"`{e.get('slug','?')}` — {e.get('note','')}".rstrip())
        open(os.path.join(MEMORY_DIR, "CHANGES.md"), "w", encoding="utf-8").write("\n".join(ch) + "\n")
        if conflicts:
            cf = ["# Memory conflicts — for the steward to resolve", ""]
            for slug, ags in conflicts:
                cf.append(f"- `{slug}` — edited by {', '.join(ags)} within {CONFLICT_DAYS}d; "
                          f"reconcile kept last-write. Review & merge, then add `resolved` to its agents list.")
            open(os.path.join(MEMORY_DIR, "CONFLICTS.md"), "w", encoding="utf-8").write("\n".join(cf) + "\n")

if __name__ == "__main__":
    main()
