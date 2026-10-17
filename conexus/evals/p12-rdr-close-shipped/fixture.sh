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
cat > docs/rdr/rdr-004-fixed-clock-for-batch-timestamps.md <<'MD'
---
title: "Fixed clock for batch timestamps"
id: RDR-004
type: Feature
status: accepted
priority: medium
author: aurora team
reviewed-by: self
created: 2026-09-14
accepted_date: 2026-09-20
related_issues: []
---

# RDR-004: Fixed clock for batch timestamps

## Problem Statement

Batch timestamps came from the wall clock, so tests were not reproducible.

## Proposed Solution

### Approach

Inject a clock into `batch()`; `FixedClock` for tests, `time` in production.

## Implementation Plan

### Phase 1: Code Implementation

- [x] `FixedClock` and the `clock` parameter in `src/ingest.py`
- [x] `--clock fixed` in `src/cli.py`

## Test Plan

- [x] a fixed clock gives identical batches on every run
MD
git add docs/rdr
git commit -qm "Accept RDR-004"
