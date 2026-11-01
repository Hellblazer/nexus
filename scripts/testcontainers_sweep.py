#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Remove orphaned Testcontainers containers before a Java build (nexus-qrldo).

The dev boxes run Testcontainers with Ryuk disabled
(``~/.testcontainers.properties``: ``testcontainers.ryuk.disabled=true``, which
the fenced gates link through for the Maven jar rebuild), so a JVM that exits
without stopping its containers leaves them running. On 2026-11-01 the laptop's
colima VM held 29 such ``pgvector/pgvector:pg17`` containers, up to 39 h old,
its 100 GiB disk filled, and a release leg failed with "No space left on
device". No Java test uses ``withReuse``, so a container labelled
``org.testcontainers=true`` that is older than any test run is an orphan.

This removes those containers, with their anonymous volumes
(``docker rm -f -v``), when they are older than ``NX_TC_SWEEP_MAX_AGE_HOURS``
(default 6, far longer than any Java suite, so a peer's live run on a shared box
is left alone). It never fails the caller: a missing or failing ``docker``, a
timeout, or a ``created`` time it cannot read leaves things as they are, with
one line on stderr. ``NX_TC_SWEEP=0`` turns it off. Stdlib only.

Called by ``leased_build_run`` (``scripts/lib/leased-build.sh``), so every
``scripts/mvnw-leased.sh`` and ``scripts/build-gate-jar.sh`` run sweeps first.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime

DEFAULT_MAX_AGE_HOURS: float = 6.0
DOCKER_TIMEOUT_S: float = 20.0
_LABEL: str = "label=org.testcontainers=true"


def _created_epoch(created: str) -> float | None:
    """``docker ps`` CreatedAt, e.g. ``2026-10-30 12:00:00 -0700 PDT``."""
    try:
        return datetime.strptime(created[:25], "%Y-%m-%d %H:%M:%S %z").timestamp()
    except ValueError:
        return None


def _note(msg: str) -> None:
    sys.stderr.write(f"testcontainers-sweep: {msg}\n")


def sweep(now: float, max_age_hours: float) -> int:
    """Remove stale containers; return how many were removed."""
    docker = shutil.which("docker")
    if docker is None:
        return 0
    try:
        ps = subprocess.run(
            [docker, "ps", "-a", "--filter", _LABEL,
             "--format", "{{.ID}}\t{{.CreatedAt}}\t{{.Names}}"],
            capture_output=True, text=True, timeout=DOCKER_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        _note(f"skipped: docker ps did not answer ({e})")
        return 0
    if ps.returncode != 0:
        _note(f"skipped: docker ps exited {ps.returncode}: {ps.stderr.strip()[:200]}")
        return 0
    cutoff = now - max_age_hours * 3600.0
    stale: list[tuple[str, str]] = []
    for line in ps.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        cid, created, name = parts[0], parts[1], parts[2]
        ts = _created_epoch(created)
        if ts is not None and ts < cutoff:
            stale.append((cid, name))
    removed = 0
    for cid, name in stale:
        try:
            rm = subprocess.run(
                [docker, "rm", "-f", "-v", cid],
                capture_output=True, text=True, timeout=DOCKER_TIMEOUT_S,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            _note(f"could not remove {name} ({cid}): {e}")
            continue
        if rm.returncode == 0:
            removed += 1
        else:
            _note(f"could not remove {name} ({cid}): {rm.stderr.strip()[:200]}")
    if stale:
        _note(f"removed {removed} of {len(stale)} Testcontainers container(s) older than {max_age_hours:g} h")
    return removed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    ap.add_argument("--now", type=float, default=None, help="epoch seconds (tests)")
    args = ap.parse_args(argv)
    if os.environ.get("NX_TC_SWEEP", "1") == "0":
        return 0
    try:
        max_age = float(os.environ.get("NX_TC_SWEEP_MAX_AGE_HOURS", DEFAULT_MAX_AGE_HOURS))
    except ValueError:
        max_age = DEFAULT_MAX_AGE_HOURS
    try:
        sweep(args.now if args.now is not None else time.time(), max_age)
    except Exception as e:  # noqa: BLE001 — a sweep never fails the build it runs ahead of
        _note(f"skipped: {type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
