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

WHY THE ORIGINATING FAILURE IS STILL UNEXPLAINED, and what has been ruled out.
Two hypotheses were recorded for the missing term. The second -- that a coarse
sampler understates the peak, because teardown and startup overlap on a
contended box and a sub-second burst falls between two samples -- was measured
on 2026-09-12 and is REFUTED, with its mechanism visible rather than merely
absent. A 54ms trace of ``tests/db/`` at ``-n 16`` shows the churn is real
during the ~9s boot ramp: the count oscillates 4, 0, 4, 8, 4, 8, 9, 12, 11, 12,
11, 13, 16, 15, 16, with transients as short as 50ms that a 1s sampler would
indeed step over. But every one of those excursions is BELOW the steady state,
never above it, and the peak is bounded by the worker count. Subsampling that
same trace at 0.5s, 1s and 2s returns a peak of 16 in all three cases: the
coarse sampler misses dips, not peaks, so it understates nothing that matters
to a budget. The first hypothesis -- that the release battery was LIVE rather
than merely leaked during the failing run, and that its concurrently-running
legs each provision Postgres -- was measured the same day and is eliminated
too. Sampling at 54ms across a real four-leg battery (lsg, shakedown, mvv,
pkgup at the default max-parallel 4), run from a worktree pinned to a fixed
sha, a LIVE battery PEAKS AT 6 segments -- and only briefly: 83 of 34,143
samples in the gate group sat at 5 or more, about a quarter of one percent of
the window, with 3 the commonest value. Subsampling that same trace at 0.5s,
1s and 2s returns 6 every time, so no burst hides here either. Four legs
costing six also says a leg is not uniformly one cluster: at least two hold a
second one transiently.

So the originating failure is STILL UNEXPLAINED, with both candidates now
eliminated rather than merely unexamined. 16 workers, plus 4 orphaned
clusters, plus a live battery at its peak, is 26 against a ceiling of 32.

WHAT THAT RESIDUAL CAN AND CANNOT DO TO THIS GUARD, since "unexplained" on
its own reads as unbounded. Some consumer of roughly six segments is
unaccounted for. If it is ALREADY holding segments when a run starts, the
guard absorbs it by construction: ``free`` is computed from ``ipcs -m`` read
live at session start, so an unknown consumer is indistinguishable from a
known one and is subtracted either way. The cap is only wrong if the unknown
consumer ARRIVES mid-run, after the count is taken -- and that is true of a
peer's suite and a concurrent gate too, neither of which is mysterious. So
the residual is a reason to keep DEFAULT_RESERVE generous, not a reason to
distrust the derivation. What would change the design is evidence that a
single worker can hold more than one segment; that is why
SEGMENTS_PER_WORKER is a named constant with a test on it rather than a bare
1 in the arithmetic.

What the numbers do change is the MARGIN, and that is the part worth carrying.
"About 21 of 32, comfortable" described a quiet box. A box carrying a battery
and some orphans sits at 26 with six to spare, and any further consumer -- a
peer's suite, a second gate -- crosses it. That is the case this guard exists
for, and it is why DEFAULT_RESERVE is 8: a live battery's measured peak of 6
fits inside it with room.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping

#: Measured, not assumed (2026-09-12, 16-core macOS box): one Postgres cluster
#: holds exactly ONE SysV segment, 56 bytes. Modern PG keeps only a tiny
#: interlock segment there and maps real shared buffers with mmap. At ``-n 4``
#: the box showed 4 postmasters and 4 segments; at ``-n 16`` over ``tests/db/``
#: it peaked at 21 segments including baseline, sampled every second through
#: worker boot to catch a transient. If a future PG allocates more than one,
#: every cap here becomes too generous -- which is why the number is a named
#: constant with a test pinning it rather than a bare 1 in the arithmetic.
#:
#: Re-measured the same day at 54ms resolution, on a box carrying other work,
#: with a ZERO baseline: ``tests/db/`` at ``-n 16`` peaked at exactly 16, so
#: peak == worker count and this constant is 1 on the nose. The earlier 21 was
#: 16 workers plus a baseline of 5 that was not this run's.
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


def set_worker_count(option: object, count: int) -> None:
    """Make xdist run *count* workers, not just say so.

    ``numprocesses`` alone is inert (nexus-vo9qn, measured on xdist 3.8.0): xdist
    builds ``option.tx = ["popen"] * numprocesses`` in its ``pytest_cmdline_main``,
    which runs BEFORE conftest's ``pytest_configure``, and the scheduler spawns
    ``len(tx)`` workers. A clamp applied afterwards printed "clamped 4 -> 2" and
    still created 4 workers, so the SysV cap (nexus-6qp25) was inert the same way.
    ``tx`` is rewritten only when it is xdist's own generated all-``popen`` list;
    a hand-written ``--tx`` is never produced alongside a ``-n`` count."""
    tx = getattr(option, "tx", None)
    generated = isinstance(tx, list) and bool(tx) and all(t == "popen" for t in tx)
    option.numprocesses = count  # type: ignore[attr-defined]
    if generated:
        option.tx = ["popen"] * count  # type: ignore[attr-defined]


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


#: Memory a worker needs from its grant: its engine JVM, its Postgres cluster and
#: the tmpfs pages that cluster's data and WAL occupy (tmpfs is charged to the
#: cgroup). nexus-vo9qn, measured on chas with the labrun sampler (nexus_911
#: proofs, 2026-10-19, tests/db + ddl): lab-l (16 cores, 48 GB) at ``-n auto`` =
#: 16 workers peaked 39.4 GB anon including shmem = 2.46 GB per worker; lab-m
#: (8 cores, 16 GB) at ``-n 8`` peaked 14.5 GB = 1.81 GB per worker. Those were
#: subsets. The FULL suite (2026-10-21, bb434ac96, labrun sampler hand-ad6cd21f) at
#: ``-n auto`` = 16 workers on lab-l peaked 47,795 MiB of the 49,152 MiB grant,
#: 2,987 MiB a worker, 1,357 MiB from an OOM kill (exit 137). The budget is 3264
#: MiB (about 3.2 GiB), so lab-l runs 15 workers (about 44,800 MiB at that peak,
#: about 4.2 GiB free) and lab-m 5. The per-worker model is kept: the 10-19 subset
#: points fit no positive fixed cost, so per-worker cost grows with the run. Without this a grant fills as workers grow, the run used to fall
#: back to disk for later clusters (3.9 GB written in 4 minutes); the session now
#: never does, so the worker count has to keep the grant from filling. Per class,
#: from the grant alone: lab-s (4 GiB) 1, lab-m (16) 5, lab-6c18g (18) 5 (CI's
#: shards run -n 4), lab-l (48) 15, lab-xl (96) 30; on hellmini lab-mac-s (4) 1
#: and lab-mac-m (10) 3 (nexus-pnyfq).
BYTES_PER_WORKER = 3264 * 1024**2


def _lab_mem_bytes(env: Mapping[str, str]) -> int | None:
    """The ``lab-run`` grant's memory (``LAB_MEM_MB``), or None when absent/garbled."""
    raw = (env.get("LAB_MEM_MB") or "").strip()
    try:
        mb = int(raw)
    except ValueError:
        return None
    return mb * 1024**2 if mb > 0 else None


def grant_memory_bytes(
    *, env: Mapping[str, str] | None = None,
    cgroup_limit: int | None = None,
) -> int | None:
    """The tightest memory limit this run can see: the smaller of ``LAB_MEM_MB``
    and the cgroup's ``memory.max`` (own path and ancestors), or None when neither
    is set (a laptop, an unlimited box)."""
    env = os.environ if env is None else env
    limits = [v for v in (_lab_mem_bytes(env), cgroup_limit) if v is not None]
    return min(limits) if limits else None


def cap_from_memory(*, grant_bytes: int) -> int:
    """Workers whose :data:`BYTES_PER_WORKER` fit in the grant; never below 1."""
    return max(1, grant_bytes // BYTES_PER_WORKER)


def effective_memory_cap() -> int | None:
    """The memory cap for this run right now, or None when it does not apply (no
    grant or cgroup limit visible, or ``NX_XDIST_NO_CAP`` set). Never raises."""
    if os.environ.get(NO_CAP_ENV):
        return None
    try:
        from tests._engine_substrate import cgroup_memory_limit  # noqa: PLC0415 — deferred: heavy import, Linux-only data

        grant = grant_memory_bytes(cgroup_limit=cgroup_memory_limit())
    except Exception:  # noqa: BLE001 — a cap that cannot be derived is not enforced
        return None
    return None if grant is None else cap_from_memory(grant_bytes=grant)


def memory_clamp_note(*, requested: int, cap: int, platform: str = sys.platform) -> str:
    head = (f"xdist workers clamped {requested} -> {cap}: this run's memory grant "
            f"fits {cap} workers at {BYTES_PER_WORKER // 1024**2} MiB each ")
    if platform.startswith("linux"):
        why = ("(engine JVM + Postgres cluster + its tmpfs pages). The run keeps every cluster on "
               "the tmpfs it chose at the start rather than spilling to disk, so the "
               "workers must fit the grant (nexus-vo9qn). ")
    else:
        # nexus-pnyfq: a macOS lab grant (lab-mac-s/-m) carries LAB_MEM_MB but no cgroup and
        # no tmpfs clusters: the clamp keeps the run inside what lab-run booked.
        why = ("(engine JVM + Postgres cluster). This platform has no cgroups, so the grant is "
               "admission and accounting only; the clamp keeps the run inside what lab-run booked. ")
    return f"{head}{why}Set {NO_CAP_ENV}=1 to run uncapped."

