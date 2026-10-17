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
git checkout -qb feature/retry-wrapper
cat > src/retry.py <<'PY'
"""Retry a call on transient failures."""
import random
import time


class TransientError(Exception):
    """A failure worth retrying (HTTP 429, 502, 503, 504, a timeout)."""


def retry(fn, attempts=5, base=0.5, cap=8.0, sleep=time.sleep, rand=random.random):
    """Call fn(); on TransientError wait with jittered exponential backoff and try again.

    Re-raises the last TransientError after `attempts` calls. Any other
    exception propagates at once.
    """
    for i in range(attempts):
        try:
            return fn()
        except TransientError:
            if i == attempts - 1:
                raise
            sleep(min(cap, base * 2 ** i) * (0.5 + rand() / 2))
PY
mkdir -p tests
cat > tests/test_retry.py <<'PY'
from src.retry import retry


def test_returns_the_value():
    assert retry(lambda: 42, sleep=lambda s: None) == 42
PY
git add src/retry.py tests/test_retry.py
git commit -qm "Add a retry wrapper with jittered backoff"
