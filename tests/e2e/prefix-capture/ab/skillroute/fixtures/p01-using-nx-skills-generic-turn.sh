#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# p01 asks for a retry wrapper around the outbound Voyage call: add that call.
set -euo pipefail
cat > src/embed.py <<'PY'
"""Embed ingest batches with the Voyage API."""
import json
import os
import urllib.request

VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"


def embed(texts, model="voyage-3"):
    body = json.dumps({"input": texts, "model": model}).encode()
    req = urllib.request.Request(
        VOYAGE_URL,
        data=body,
        headers={
            "Authorization": "Bearer " + os.environ["VOYAGE_API_KEY"],
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return [d["embedding"] for d in json.load(resp)["data"]]


def index(batches):
    """Embed every batch in turn; one failed request stops the whole run."""
    out = []
    for b in batches:
        out.extend(embed([str(e) for _, e in b]))
    return out
PY
git add src/embed.py
git commit -qm "Embed ingest batches with the Voyage API"
