"""Derive a safe xdist worker count from the box's SysV shared-memory budget.

nexus-6qp25. See ``tests/test_xdist_worker_cap.py`` for the measurements this
rests on and why the cap is computed from AVAILABLE headroom rather than from
``kern.sysv.shmmni`` alone.

The short version: every pytest process boots its own engine substrate, every
substrate is one Postgres cluster, and every cluster holds one SysV segment.
The segment budget is shared with every OTHER cluster on the box -- a running
gate, or a cluster orphaned by a killed battery -- so a cap that assumes the
budget is yours alone hands out the same parallelism on a busy box as on a
quiet one.

This is a HEADROOM GUARD and not a reproduction of the incident that prompted
it. Measured: 16 workers cost about 20 of 32 segments on the FULL suite and
pass cleanly, with no extra clusters per worker, so parallelism alone was
never the cause and no worker-count cap would have prevented that failure
without halving everyone's throughput. What it does is bind when the budget
genuinely is low, whatever consumed it, and do nothing on a quiet box. The
primary fix for the one contributor actually observed -- orphaned clusters
from a battery killed mid-flight -- is to reap them, not to cap here.

Exhaustion is invisible as a test failure: ``initdb`` dies with
``shmget: No space left on device`` and every substrate-backed test errors at
SETUP, so the run reports thousands of setup errors that read as a code
regression rather than as resource contention.

Non-macOS boxes have no ``kern.sysv.*`` and are not subject to this limit in
the same way, so the cap is simply not applied there.
"""

from __future__ import annotations

import os
import subprocess

#: Measured, not assumed (2026-09-12, 16-core macOS box): one Postgres cluster
#: holds exactly ONE SysV segment, 56 bytes. Modern PG keeps only a tiny
#: interlock segment there and maps real shared buffers with mmap. At ``-n 4``
#: the box showed 4 postmasters and 4 segments; at ``-n 16`` over ``tests/db/``
#: it peaked at 21 segments including baseline, sampled every second through
#: worker boot to catch a transient. If a future PG allocates more than one,
#: every cap here becomes too generous -- which is why the number is a named
#: constant with a test pinning it rather than a bare 1 in the arithmetic.
SEGMENTS_PER_WORKER = 1

#: Segments held back for things that are not this run's workers: a concurrent
#: gate's cluster, a peer session's suite, a cluster orphaned by a killed
#: battery. Four orphans were live when the 2026-09-12 run failed, so this is
#: sized to absorb that case with room rather than to be minimal.
DEFAULT_RESERVE = 8

#: Set to any non-empty value to skip the clamp entirely. The named escape
#: hatch for a box whose real capacity this derivation gets wrong.
NO_CAP_ENV = "NX_XDIST_NO_CAP"


def _sysctl_int(name: str) -> int | None:
    """Read an integer sysctl, or None when it does not exist or does not parse."""
    try:
        out = subprocess.run(
            ["sysctl", "-n", name],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    try:
        return int(out.stdout.strip())
    except ValueError:
        return None


def segments_in_use() -> int | None:
    """Count SysV shared-memory segments currently allocated on this box.

    None when ``ipcs`` is unavailable or unparseable — the caller must treat
    that as "cannot determine" and skip the clamp rather than assume zero,
    since assuming zero is assuming the budget is free, which is the exact
    wrong direction.
    """
    try:
        out = subprocess.run(
            ["ipcs", "-m"], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    # Rows for shared-memory segments start with "m " in the ipcs table.
    return sum(1 for line in out.stdout.splitlines() if line.startswith("m "))


def cap_from_headroom(
    *, shmmni: int, in_use: int, reserve: int, cpu_count: int
) -> int:
    """Workers that fit in the segment budget that is actually free.

    Never returns less than 1: a fully-consumed budget still has to run the
    suite, just serially.
    """
    free = shmmni - in_use - reserve
    by_segments = free // SEGMENTS_PER_WORKER
    return max(1, min(by_segments, cpu_count))


def clamp_numprocesses(
    *, requested: int | None, cap: int
) -> tuple[int | None, str | None]:
    """Clamp a requested worker count to *cap*, with a reason when it binds.

    Returns ``(value, note)``. ``note`` is None when nothing was changed.

    ``None`` (no ``-n`` at all) and ``0`` (``-n 0``, xdist explicitly off) are
    both left exactly as they are: raising either would silently ADD
    parallelism, and this is a ceiling, never a floor.
    """
    if requested is None or requested <= 0 or requested <= cap:
        return requested, None
    note = (
        f"xdist workers clamped {requested} -> {cap}: this box's SysV shared memory "
        f"budget cannot support {requested} concurrent Postgres substrates (one per "
        f"worker), counting what is already allocated. Past the limit initdb fails "
        f"with 'No space left on device' and every substrate-backed test errors at "
        f"SETUP, which reads as a code regression rather than as contention "
        f"(nexus-6qp25). Set {NO_CAP_ENV}=1 to run uncapped."
    )
    return cap, note


def effective_cap() -> int | None:
    """The cap for this box right now, or None when it does not apply.

    None means: not a box with a ``kern.sysv.shmmni`` limit, or the budget
    could not be read. Either way the caller applies no clamp — a cap that
    cannot be derived honestly is not guessed at.
    """
    if os.environ.get(NO_CAP_ENV):
        return None
    shmmni = _sysctl_int("kern.sysv.shmmni")
    if shmmni is None:
        return None
    in_use = segments_in_use()
    if in_use is None:
        return None
    return cap_from_headroom(
        shmmni=shmmni,
        in_use=in_use,
        reserve=DEFAULT_RESERVE,
        cpu_count=os.cpu_count() or 1,
    )
