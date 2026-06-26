#!/usr/bin/env python3
"""Ask each agent to draw its own line-art icon, in a uniform style, monochrome via currentColor (so
one SVG works in both light and dark themes). Stored to private-data/agents/<name>/icon.svg and
surfaced on the dashboard's Agents panel. Run once — icons are stable; re-run to refresh.
"""
import re, sys
from pathlib import Path
sys.path.insert(0, str(Path.home() / "projects/moprox-tooling/services/agents"))
from run import run_agent, AGENTS

PROMPT = (
    "Draw a single minimalist LINE-ART SVG icon that represents you and your role — chosen so it sits "
    "well in a UNIFORM set next to the other agents' icons. Follow this spec EXACTLY:\n"
    "- exactly one <svg> element, viewBox=\"0 0 24 24\", NO width/height attributes\n"
    "- line art only: fill=\"none\" stroke=\"currentColor\" stroke-width=\"1.8\" "
    "stroke-linecap=\"round\" stroke-linejoin=\"round\"\n"
    "- a few clean strokes, simple and iconic, centred with ~2px padding; NO colour, NO text, NO <image>\n"
    "Output ONLY the <svg>…</svg> markup, nothing else.")

def extract(s):
    m = re.search(r"<svg\b.*?</svg>", s or "", re.S | re.I)
    return m.group(0).strip() if m else None

def main():
    for a in AGENTS:
        try:
            svg = extract(run_agent(a, PROMPT, timeout=240))
            if not svg or "currentColor" not in svg or "viewBox" not in svg:
                print(a, "-> no valid icon (kept existing)"); continue
            (Path(AGENTS[a]) / "icon.svg").write_text(svg + "\n")
            print(a, "->", len(svg), "bytes")
        except Exception as e:
            print(a, "err", e)

if __name__ == "__main__":
    main()
