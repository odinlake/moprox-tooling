#!/usr/bin/env python3
"""Durably archive Claude Code conversation transcripts so they survive the 30-day local cleanup and
are searchable later (see search.py). Claude Code keeps transcripts as JSONL under
~/.claude/projects/<proj>/<session>.jsonl but prunes them after `cleanupPeriodDays` (default 30); this
flattens each session to a readable .md (user/assistant turns + a one-line trace of tool actions, no
thinking/tool-output noise) and writes it to a durable dir OUTSIDE ~/.claude.

These contain everything discussed (incl. secrets) — the archive is LOCAL ONLY, never committed.

Incremental: re-flattens only sessions whose source grew/changed (manifest.json). stdlib only.
Env: CLAUDE_PROJECTS (default ~/.claude/projects), THREADS_DIR (default ~/.local/share/moprox/threads).
"""
import json, os, sys, glob, datetime

PROJECTS = os.environ.get("CLAUDE_PROJECTS", os.path.expanduser("~/.claude/projects"))
OUT = os.environ.get("THREADS_DIR", os.path.expanduser("~/.local/share/moprox/threads"))
MANIFEST = os.path.join(OUT, "manifest.json")

def text_of(content):
    if isinstance(content, str): return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""

def flatten(path):
    """Return (meta, lines[]) for one transcript, or None if it has no real turns."""
    title = None; cwd = ""; branch = ""; tss = []; nu = na = 0; out = []
    for ln in open(path, errors="ignore"):
        try: d = json.loads(ln)
        except Exception: continue
        t = d.get("type")
        if t == "ai-title" and not title:
            title = (d.get("title") or d.get("content") or "").strip() or None
        if t not in ("user", "assistant"): continue
        m = d.get("message")
        if not isinstance(m, dict): continue
        cwd = d.get("cwd", cwd); branch = d.get("gitBranch", branch)
        ts = (d.get("timestamp") or "")[:19]
        if ts: tss.append(ts)
        clock = ts[11:16] if len(ts) >= 16 else ""
        if t == "user":
            txt = text_of(m.get("content")).strip()
            # skip tool_result-only user turns (the harness feeding tool output back)
            if not txt or txt.startswith("<") and txt.endswith(">"): continue
            nu += 1; title = title or txt[:90]
            out.append(f"[USER {clock}] {txt}")
        else:
            na += 1
            for b in (m.get("content") if isinstance(m.get("content"), list) else []):
                if not isinstance(b, dict): continue
                if b.get("type") == "text" and b.get("text", "").strip():
                    out.append(f"[ASSISTANT {clock}] {b['text'].strip()}")
                elif b.get("type") == "tool_use":
                    name = b.get("name", "?"); inp = b.get("input", {})
                    hint = inp.get("command") or inp.get("file_path") or inp.get("query") or inp.get("prompt") or inp.get("path") or ""
                    hint = str(hint).replace("\n", " ")[:140]
                    out.append(f"    · {name}: {hint}")
    if nu == 0 and na == 0: return None
    meta = {"title": (title or "untitled").strip()[:120], "cwd": cwd, "branch": branch,
            "start": min(tss)[:10] if tss else "", "end": max(tss)[:10] if tss else "",
            "n_user": nu, "n_assistant": na}
    return meta, out

def main():
    os.makedirs(OUT, exist_ok=True)
    try: manifest = json.load(open(MANIFEST))
    except Exception: manifest = {}
    index = []
    archived = changed = 0
    for path in sorted(glob.glob(os.path.join(PROJECTS, "*", "*.jsonl"))):
        proj = os.path.basename(os.path.dirname(path))
        sess = os.path.splitext(os.path.basename(path))[0]
        key = f"{proj}/{sess}"
        try: st = os.stat(path)
        except OSError: continue
        sig = [int(st.st_mtime), st.st_size]
        dst = os.path.join(OUT, proj, sess + ".md")
        fresh = manifest.get(key, {}).get("sig") != sig or not os.path.exists(dst)
        if fresh:
            res = flatten(path)
            if res:
                meta, lines = res
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                header = (f"# {meta['title']}\n"
                          f"session: {sess} · project: {proj} · {meta['start']}–{meta['end']} · "
                          f"{meta['n_user']}u/{meta['n_assistant']}a · cwd: {meta['cwd']} {('· '+meta['branch']) if meta['branch'] else ''}\n\n")
                open(dst, "w", encoding="utf-8").write(header + "\n".join(lines) + "\n")
                manifest[key] = {"sig": sig, **meta}
                changed += 1
        if key in manifest:
            index.append({"key": key, "path": dst, **{k: manifest[key].get(k) for k in ("title","start","end","n_user","n_assistant","cwd")}})
            archived += 1
    json.dump(manifest, open(MANIFEST, "w"), indent=0)
    json.dump(sorted(index, key=lambda x: x.get("end") or "", reverse=True), open(os.path.join(OUT, "index.json"), "w"), indent=0)
    print(f"archived {archived} sessions ({changed} (re)written) -> {OUT}")

if __name__ == "__main__":
    main()
