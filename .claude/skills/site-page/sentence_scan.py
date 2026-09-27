#!/usr/bin/env python3
"""Sentence-shape scan for one web/ page (site-page skill, section 5).

Prints the prose word count of <main>, every sentence over 25 words, and every
run of three or more consecutive sentences of 10 words or fewer, with the
rate of such runs per 100 sentences. Calibration (2026-09-27): the ci-board
page Sam found hard to read scored 5.8 (a thin margin over the limit) and its
rewrite 0; rdr 9.8 before its rewrite and 1.6 after; coordination 0.8,
tuple-space 2.1, getting-started 2.4, research 0.6, research-in-nexus 0.9;
index 12.9, where install steps are short by nature. A first version split on
source line breaks and scored the hard-wrapped research-in-nexus page 8.4;
whitespace is collapsed before block boundaries are marked for that reason. Code blocks, SVG, summaries, section labels, lesson headers,
lists and tables are excluded, because their lines are not running prose.
Both findings are prompts for a reader's judgement, not hard failures: a long
sentence can be a list, and a short run can be deliberate.

Usage: python3 .claude/skills/site-page/sentence_scan.py web/<page>.html
"""
from __future__ import annotations

import html
import re
import sys

LONG: int = 25
SHORT: int = 10
RUN: int = 3
RUN_RATE: float = 5.0


def prose(page: str) -> str:
    start = page.find("<main>")
    main = page[start:page.index("</main>")] if start >= 0 else page[page.index("<body"):]
    main = re.sub(r"<(script|style)[\s\S]*?</\1>", "", main)
    main = re.sub(r'<svg[\s\S]*?</svg>|<pre[\s\S]*?</pre>|<summary>[\s\S]*?</summary>|<table[\s\S]*?</table>|<div class="see">[\s\S]*?</div>|<div class="lhead">[\s\S]*?</div>|<div class="topnav">[\s\S]*?</div>|<ul[\s\S]*?</ul>|<ol[\s\S]*?</ol>', "", main)
    # Source line breaks inside a block are wrapping, not sentence ends, so
    # collapse all whitespace first and mark block boundaries afterwards.
    main = re.sub(r"\s+", " ", main)
    main = re.sub(r"<(p|li|h[1-6]|div|blockquote|section|figcaption|dt|dd)\b[^>]*>", "\n", main)
    return html.unescape(re.sub(r"<[^>]+>", "", main))


def main(path: str) -> int:
    text = prose(open(path, encoding="utf-8").read())
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if len(s.split()) > 2]
    print(f"{len(text.split())} prose words, {len(sentences)} sentences")
    run: list[str] = []
    runs = 0
    for s in sentences:
        n = len(s.split())
        if n > LONG:
            print(f"LONG {n}: {s}")
        if n <= SHORT:
            run.append(s)
            continue
        if len(run) >= RUN:
            runs += 1
            print(f"SHORT RUN of {len(run)}: " + " | ".join(run))
        run = []
    if len(run) >= RUN:
        runs += 1
        print(f"SHORT RUN of {len(run)}: " + " | ".join(run))
    rate = 100 * runs / max(len(sentences), 1)
    verdict = "FRAGMENTED" if rate > RUN_RATE else "ok"
    print(f"short runs: {runs} ({rate:.1f} per 100 sentences, limit {RUN_RATE}) {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
