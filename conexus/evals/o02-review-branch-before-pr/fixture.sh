#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Scaffold for this case (nexus-dg7i6.7): builds the project the prompt talks
# about in the current, empty directory. Fixed content and fixed commit dates,
# no network, so the outcome graders read the same files on every run.
set -euo pipefail
export GIT_AUTHOR_DATE=2026-10-01T12:00:00Z GIT_COMMITTER_DATE=2026-10-01T12:00:00Z
git init -q
git checkout -q -b main
git config user.name aurora
git config user.email aurora@example.invalid
mkdir -p src
cat > README.md <<'MD'
# aurora

A small stream-processing service.

Run: python -m src.cli --help
MD
cat > CLAUDE.md <<'MD'
# aurora

Aurora is a small stream-processing service. Code is in src/.
MD
cat > src/__init__.py <<'PY'
PY
cat > src/ingest.py <<'PY'
"""Batch ingest for aurora."""
import time


class FixedClock:
    def __init__(self, t: float) -> None:
        self.t = t

    def now(self) -> float:
        return self.t


def batch(events, size, clock=time):
    out, cur = [], []
    for e in events:
        cur.append((clock.now() if hasattr(clock, "now") else clock.time(), e))
        if len(cur) == size:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out
PY
cat > src/cli.py <<'PY'
"""Command line entry point for aurora."""
import argparse


def parse_args(argv=None):
    parser = argparse.ArgumentParser(prog="aurora")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--clock", choices=["wall", "fixed"], default="wall")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    print(args)


if __name__ == "__main__":
    main()
PY
git add -A
git commit -qm "Add ingest batching with an injected clock"
git checkout -qb feature/embed-indexing
cat > src/embed.py <<'PY'
"""Embed ingest batches with the Voyage API."""
import json
import logging
import os
import urllib.request

log = logging.getLogger(__name__)
VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"


def embed(texts, model="voyage-3"):
    key = os.environ.get("VOYAGE_API_KEY", "")
    log.info("embedding %d texts with key %s", len(texts), key)
    body = json.dumps({"input": texts, "model": model}).encode()
    req = urllib.request.Request(
        VOYAGE_URL, data=body,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        return [d["embedding"] for d in json.load(resp)["data"]]


def index(batches, cache_path="/tmp/aurora-embeddings.json"):
    out = []
    for b in batches:
        out.extend(embed([str(e) for _, e in b]))
    with open(cache_path, "w") as f:
        json.dump(out, f)
    os.system("chmod 666 " + cache_path)
    return out
PY
python3 - <<'PY'
p = "src/cli.py"
s = open(p).read()
s = s.replace('    parser.add_argument("--clock", choices=["wall", "fixed"], default="wall")\n',
              '    parser.add_argument("--clock", choices=["wall", "fixed"], default="wall")\n'
              '    parser.add_argument("--index", action="store_true")\n'
              '    parser.add_argument("--cache-path", default="/tmp/aurora-embeddings.json")\n')
open(p, "w").write(s)
PY
git add src/embed.py src/cli.py
git commit -qm "Add Voyage embedding and an --index flag"
