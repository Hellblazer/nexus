#!/usr/bin/env python3
"""Audit every href on the web/ pages and the README's site links.

Checks: in-page anchors; relative links between site pages and their
anchors; github.com/Hellblazer/nexus blob/tree links against the working
tree AND against origin/main (the links name main); anchors into repo
Markdown against its headings; other http links by fetch. Prints one line
per failure and exits 1 if any.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request

ROOT = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True).stdout.strip()
os.chdir(ROOT)
PAGES = sorted(p for p in (os.path.join("web", f) for f in os.listdir("web")) if p.endswith(".html"))


def anchors(path: str) -> set[str]:
    t = open(path, encoding="utf-8", errors="ignore").read()
    ids = set(re.findall(r'id="([^"]+)"', t))
    if path.endswith(".md"):
        for h in re.findall(r"^#+\s+(.*)$", t, re.M):
            s = re.sub(r"`", "", h.strip().lower())
            s = re.sub(r"[^\w\s-]", "", s)
            ids.add(re.sub(r"\s+", "-", s))
    return ids


def main() -> int:
    subprocess.run(["git", "fetch", "-q", "origin", "main"], check=False)
    main_tree = set(subprocess.run(["git", "ls-tree", "-r", "--name-only", "origin/main"], capture_output=True, text=True).stdout.split("\n"))
    bad: list[tuple[str, str, str]] = []
    fetched: dict[str, object] = {}
    for p in PAGES:
        t = open(p).read()
        for h in sorted(set(re.findall(r'href="([^"]+)"', t))):
            if h.startswith(("data:", "mailto:")):
                continue
            if h.startswith("#"):
                if h[1:] not in anchors(p):
                    bad.append((p, h, "missing anchor"))
                continue
            if h.startswith("http"):
                m = re.match(r"https://github\.com/Hellblazer/nexus/(blob|tree)/main/([^#]+)(#(.*))?", h)
                if m:
                    rel = urllib.parse.unquote(m.group(2)).rstrip("/")
                    frag = m.group(4)
                    if not os.path.exists(rel):
                        bad.append((p, h, "no such path in working tree"))
                        continue
                    if rel not in main_tree and not any(x.startswith(rel + "/") for x in main_tree):
                        bad.append((p, h, "not on origin/main"))
                    if frag and os.path.isfile(rel) and frag not in anchors(rel):
                        bad.append((p, h, "anchor not in file"))
                    continue
                if h not in fetched:
                    try:
                        req = urllib.request.Request(h, headers={"User-Agent": "Mozilla/5.0"})
                        fetched[h] = urllib.request.urlopen(req, timeout=15).getcode()
                    except Exception as e:  # noqa: BLE001
                        fetched[h] = getattr(e, "code", str(e))
                if fetched[h] != 200:
                    bad.append((p, h, f"HTTP {fetched[h]}"))
                continue
            base, _, frag = h.partition("#")
            base = "index.html" if base in ("", "./") else base
            tgt = os.path.join("web", base) if base != os.path.basename(p) else p
            if not os.path.exists(tgt):
                bad.append((p, h, "no such site file"))
                continue
            if frag and frag not in anchors(tgt):
                bad.append((p, h, "anchor not on target page"))
    readme = open("README.md").read()
    for h in set(re.findall(r"https://hellblazer\.github\.io/nexus/([^)\s]*)", readme)):
        base, _, frag = h.partition("#")
        tgt = os.path.join("web", base or "index.html")
        if not os.path.exists(tgt):
            bad.append(("README.md", h, "no such site file"))
        elif frag and frag not in anchors(tgt):
            bad.append(("README.md", h, "anchor not on target page"))
    for b in bad:
        print("\t".join(b))
    print(f"pages: {len(PAGES)}  bad: {len(bad)}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
