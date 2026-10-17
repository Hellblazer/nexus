#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# p02 asks for a review of "the changes on this branch" before a PR: put a
# feature branch with a real diff on top of main.
set -euo pipefail
git branch -M master main
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
