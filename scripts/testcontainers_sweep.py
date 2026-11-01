#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Remove this repo's orphaned Testcontainers containers before a Java build (nexus-qrldo).

The dev boxes run Testcontainers with Ryuk disabled
(``~/.testcontainers.properties``: ``testcontainers.ryuk.disabled=true``, which
the fenced gates link through for the Maven jar rebuild), so a JVM that is
killed or exits without stopping its containers leaves them running. On
2026-11-01 the laptop's colima VM held 29 such ``pgvector/pgvector:pg17``
containers, up to 39 h old; with their json-file logs and tmpfs PGDATA the
100 GiB disk filled and a release leg failed with "No space left on device".

Only this repo's own test Postgres containers are touched: those carrying both
``org.testcontainers=true`` and the ``dev.nexus.test.run`` label that
``PgContainerRecipe`` puts on every container it builds. Another project's
Testcontainers container on the same daemon (a reusable one, a dev-time
database; reuse is enabled in the same properties file) has no such label and
is never removed. Nothing in ``service/src`` uses ``withReuse``, and a surefire
fork is bounded at 45 min (``forkedProcessTimeoutInSeconds=2700``), so one of
ours older than ``NX_TC_SWEEP_MAX_AGE_HOURS`` (default 6) is an orphan, and a
peer's live run on a shared box (chas) is left alone.

Removal is ``docker rm -f -v``: ``-f`` stops it, which frees its tmpfs and its
json-file log; ``-v`` drops an anonymous volume if one exists. Each removal is
logged with name, image and age. The whole sweep stops at
:data:`TOTAL_DEADLINE_S`. It never fails the caller: a missing or failing
``docker``, a timeout, or a created time it cannot read leaves things as they
are, with one line on stderr. ``NX_TC_SWEEP=0`` turns it off; an age bound under
:data:`MIN_AGE_HOURS` (0, a negative, a typo) is refused rather than sweeping
live containers. Stdlib only.

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
MIN_AGE_HOURS: float = 0.5
DOCKER_TIMEOUT_S: float = 20.0
TOTAL_DEADLINE_S: float = 60.0
_FILTERS: tuple[str, ...] = (
    "--filter", "label=org.testcontainers=true",
    "--filter", "label=dev.nexus.test.run",
)


def _created_epoch(created: str) -> float | None:
    """``docker ps`` CreatedAt, e.g. ``2026-10-30 12:00:00 -0700 PDT``."""
    try:
        return datetime.strptime(created[:25], "%Y-%m-%d %H:%M:%S %z").timestamp()
    except ValueError:
        return None


def _note(msg: str) -> None:
    sys.stderr.write(f"testcontainers-sweep: {msg}\n")


def sweep(now: float, max_age_hours: float, deadline_s: float = TOTAL_DEADLINE_S) -> int:
    """Remove stale containers; return how many were removed."""
    docker = shutil.which("docker")
    if docker is None:
        return 0
    stop_at = time.monotonic() + deadline_s

    def _left() -> float:
        return min(DOCKER_TIMEOUT_S, stop_at - time.monotonic())

    try:
        ps = subprocess.run(
            [docker, "ps", "-a", *_FILTERS,
             "--format", "{{.ID}}\t{{.CreatedAt}}\t{{.Names}}\t{{.Image}}"],
            capture_output=True, text=True, timeout=_left(),
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        _note(f"skipped: docker ps did not answer ({e})")
        return 0
    if ps.returncode != 0:
        _note(f"skipped: docker ps exited {ps.returncode}: {ps.stderr.strip()[:200]}")
        return 0
    cutoff = now - max_age_hours * 3600.0
    stale: list[tuple[str, str, str, float]] = []
    rows = unparsed = 0
    for line in ps.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        rows += 1
        cid, created, name, image = parts[0], parts[1], parts[2], parts[3]
        ts = _created_epoch(created)
        if ts is None:
            unparsed += 1
        elif ts < cutoff:
            stale.append((cid, name, image, (now - ts) / 3600.0))
    if rows and unparsed == rows:
        _note(f"kept all {rows}: could not read any created time (docker format changed?)")
    removed = 0
    for cid, name, image, age_h in stale:
        left = _left()
        if left <= 0:
            _note(f"stopped at the {deadline_s:g} s deadline; {len(stale) - removed} left")
            break
        try:
            rm = subprocess.run(
                [docker, "rm", "-f", "-v", cid],
                capture_output=True, text=True, timeout=left,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            _note(f"could not remove {name} ({cid}): {e}")
            continue
        if rm.returncode == 0:
            removed += 1
            _note(f"removed {name} ({cid}, {image}, {age_h:.1f} h old)")
        else:
            _note(f"could not remove {name} ({cid}): {rm.stderr.strip()[:200]}")
    if stale:
        _note(f"removed {removed} of {len(stale)} container(s) older than {max_age_hours:g} h")
    return removed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    ap.add_argument("--now", type=float, default=None, help="epoch seconds (tests)")
    ap.add_argument("--deadline", type=float, default=TOTAL_DEADLINE_S, help="seconds (tests)")
    args = ap.parse_args(argv)
    if os.environ.get("NX_TC_SWEEP", "1") == "0":
        return 0
    raw = os.environ.get("NX_TC_SWEEP_MAX_AGE_HOURS", "")
    try:
        max_age = float(raw) if raw else DEFAULT_MAX_AGE_HOURS
    except ValueError:
        _note(f"skipped: NX_TC_SWEEP_MAX_AGE_HOURS={raw!r} is not a number")
        return 0
    if max_age < MIN_AGE_HOURS:
        _note(f"skipped: NX_TC_SWEEP_MAX_AGE_HOURS={raw} is under {MIN_AGE_HOURS:g} h (use NX_TC_SWEEP=0 to turn it off)")
        return 0
    try:
        sweep(args.now if args.now is not None else time.time(), max_age, args.deadline)
    except Exception as e:  # noqa: BLE001 — a sweep never fails the build it runs ahead of
        _note(f"skipped: {type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
