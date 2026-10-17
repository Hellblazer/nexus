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
mkdir -p docs/rdr
rdr() { cat > "docs/rdr/$1"; }
rdr rdr-011-hnsw-defaults.md <<'MD'
---
title: "RDR-011: HNSW defaults for the event-embedding table"
status: closed
date: 2026-03-02
---
## Problem
Recall at k=10 on the 2M-row event-embedding table was 0.89 with pgvector's defaults (m=16, ef_construction=64, ef_search=40).
## Measurements
| m | ef_construction | ef_search | recall@10 | p95 ms | build min |
|---|---|---|---|---|---|
| 16 | 64 | 40 | 0.89 | 4.1 | 38 |
| 16 | 128 | 100 | 0.96 | 6.8 | 61 |
| 24 | 128 | 100 | 0.97 | 7.9 | 84 |
## Decision
Adopt m=16, ef_construction=128, ef_search=100. m=24 gains one recall point for 38% more build time.
MD
rdr rdr-014-ivfflat-for-cold-tenants.md <<'MD'
---
title: "RDR-014: IVFFlat for cold tenants"
status: closed
date: 2026-04-19
---
## Problem
HNSW build time dominates onboarding for tenants under 200k rows that are queried a few times a day.
## Measurements
IVFFlat with lists = rows/1000 and probes=10 gave recall@10 0.93 at p95 3.2 ms, built in 40 s versus 9 min for HNSW at the RDR-011 settings.
## Decision
Tenants under 200k rows use IVFFlat (lists = rows/1000, probes = 10); they move to HNSW when they cross 500k rows.
MD
rdr rdr-017-ef-search-per-query.md <<'MD'
---
title: "RDR-017: Per-query ef_search"
status: closed
date: 2026-06-08
---
## Problem
One global ef_search=100 (RDR-011) over-pays for dashboard queries that only need the top 3.
## Measurements
ef_search=40 for k<=5 keeps recall@5 at 0.95 and cuts p95 from 6.8 to 3.9 ms; ef_search=200 for k=50 export queries lifts recall@50 from 0.91 to 0.97.
## Decision
Set ef_search per transaction: 40 for k<=5, 100 by default, 200 for k>=50.
MD
rdr rdr-019-halfvec.md <<'MD'
---
title: "RDR-019: halfvec storage for 1024-dim embeddings"
status: closed
date: 2026-08-11
---
## Problem
The HNSW index on 1024-dim vector columns no longer fits in shared_buffers (31 GB of 32 GB).
## Measurements
halfvec(1024) with the same HNSW settings: index 15.6 GB, recall@10 0.958 versus 0.961, p95 5.9 ms versus 7.4 ms because the index stays in memory.
## Decision
Store embeddings as halfvec(1024) and index with halfvec_cosine_ops; keep m=16, ef_construction=128.
MD
rdr rdr-022-maintenance-work-mem.md <<'MD'
---
title: "RDR-022: Build settings for HNSW rebuilds"
status: closed
date: 2026-09-27
---
## Problem
Nightly HNSW rebuilds after bulk loads ran 61 min and spilled to disk.
## Measurements
maintenance_work_mem=8GB and max_parallel_maintenance_workers=7 cut the build to 14 min with no spill; the resulting index is identical in recall.
## Decision
Rebuild with maintenance_work_mem=8GB and 7 parallel workers, inside the 02:00 window; recall settings unchanged from RDR-011 and RDR-019.
MD
git add docs/rdr
git commit -qm "Add the pgvector index tuning RDRs"
