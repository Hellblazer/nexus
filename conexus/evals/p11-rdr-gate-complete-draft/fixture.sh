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
cat > docs/rdr/rdr-003-retry-transient-embedding-failures.md <<'MD'
---
title: "Retry transient embedding failures"
id: RDR-003
type: Feature
status: draft
priority: medium
author: aurora team
reviewed-by: self
created: 2026-09-13
related_issues: []
---

# RDR-003: Retry transient embedding failures

## Problem Statement

One HTTP 503 from the Voyage embedding API stops the whole indexing run.

### Enumerated gaps to close

#### Gap 1: a transient failure ends the batch

A 503 or a timeout on one request aborts every batch after it.

## Context

Indexing runs nightly over about 40k events in batches of 500.

## Research Findings

### Key Discoveries

- **Verified** (source search): `src/embed.py` calls `urlopen` once per batch with no retry.
- **Documented** (docs only): Voyage returns 429 and 503 under load and asks clients to back off.

### Critical Assumptions

- Retrying a batch is safe because embedding is idempotent. Verified: the same input gives the same vector.

## Proposed Solution

### Approach

Wrap the request in a retry with jittered exponential backoff: 5 attempts, 0.5 s base, 8 s cap, on 429, 5xx and timeouts only.

## Alternatives Considered

### Alternative 1: per-event requests

Rejected: 500 times the request count.

## Trade-offs

A run can take up to 30 s longer per failing batch.

## Implementation Plan

### Minimum Viable Validation

Inject a 503 on the second batch; the run completes and logs one retry.

### Phase 1: Code Implementation

Add `src/retry.py` and wrap `embed()`.

## Test Plan

Unit tests for success, transient then success, exhaustion, and a 400 that is not retried.
MD
git add docs/rdr
git commit -qm "Draft RDR-003"
