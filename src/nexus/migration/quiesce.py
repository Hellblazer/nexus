# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-159 P1c (nexus-ue6g7.10): the indexing-quiesce pre-gate (S2 + RF-6).

The guided migration's T3 count verification (RF-6) is only meaningful across a
QUIESCENT write window: a background aspect worker writing into a source store
mid-count produces a source-vs-target mismatch that looks like data loss but is
not. Two mechanisms enforce the window:

* **Suspend (S2).** Each aspect worker and ``nx index`` polls the
  ``migration.state`` sentinel and suspends while ``migrating`` — wired in
  ``aspect_worker._run_loop`` and the ``nx index`` command group. That is the
  cross-process suspend the process-local ``drain_worker`` cannot provide.
* **Pre-gate audit (this module).** Suspend is cooperative; a worker that has
  not yet observed the sentinel, or a foreign process that never polls, could
  still hold a write lock. Before T3 starts, :func:`assert_quiescent_for_migration`
  audits the cross-process worker-lock files (the SIG-5 convention owned by
  ``aspect_worker``) and BLOCKS with the offending pids if any live foreign
  worker remains — rather than running into the RF-6 false-failure mismatch.

If a mismatch is nonetheless observed, :func:`explain_count_mismatch` ATTRIBUTES
it loudly (collection, expected vs actual, the foreign pids) — never a silent
rollback.
"""
from __future__ import annotations

from pathlib import Path

import structlog

from nexus.aspect_worker import live_foreign_worker_pids
from nexus.config import nexus_config_dir

_log = structlog.get_logger(__name__)


class MigrationQuiesceBlocked(RuntimeError):
    """Raised by the pre-gate when live foreign aspect workers persist.

    Carries every offending pid (``.pids``) so the operator can stop the
    specific processes — a half-quiesced window would make the RF-6 T3 count
    verification report a false failure.
    """

    def __init__(self, pids: list[int], locks_dir: Path) -> None:
        self.pids = pids
        self.locks_dir = locks_dir
        pid_str = ", ".join(str(p) for p in pids)
        super().__init__(
            f"migration blocked: {len(pids)} live aspect-worker write-lock(s) "
            f"remain across processes (pids: {pid_str}; locks: {locks_dir}). "
            "Stop those processes (or invoke the migration from within the MCP "
            "process) so the T3 count window is quiescent, then re-run. The "
            "upsert is idempotent on (tenant, collection, chash) — re-running "
            "is safe."
        )


def _resolve_locks_dir(locks_dir: Path | None) -> Path:
    return locks_dir if locks_dir is not None else (nexus_config_dir() / "locks")


def assert_quiescent_for_migration(*, locks_dir: Path | None = None) -> None:
    """Pre-gate (RF-6): BLOCK if any live foreign aspect worker remains.

    Sweeps dead lock files and ignores the current process's own lock (the
    migration may be driven from within an MCP process that holds one). Raises
    :class:`MigrationQuiesceBlocked` listing every live foreign pid; returns
    ``None`` when the window is quiescent.
    """
    resolved = _resolve_locks_dir(locks_dir)
    pids = live_foreign_worker_pids(resolved)
    if pids:
        _log.warning("migration_quiesce_blocked", pids=pids, locks_dir=str(resolved))
        raise MigrationQuiesceBlocked(pids, resolved)
    _log.info("migration_quiesce_clear", locks_dir=str(resolved))


def explain_count_mismatch(
    *,
    collection: str,
    expected: int,
    actual: int,
    foreign_pids: list[int],
) -> str:
    """Return a LOUD, attributed explanation of a T3 count mismatch (RF-6).

    Names the collection, the expected-vs-actual counts, and any foreign
    aspect-worker pids that were live during the count window. The migration
    surfaces this rather than silently rolling back — re-running is safe
    because the upsert is idempotent on ``(tenant, collection, chash)``.
    """
    if foreign_pids:
        cause = (
            "a concurrent writer wrote during the migration window "
            f"(live foreign aspect-worker pids: "
            f"{', '.join(str(p) for p in foreign_pids)})"
        )
    else:
        cause = "no live foreign writer was detected — investigate the source store"
    return (
        f"T3 count mismatch for {collection!r}: expected {expected}, got {actual}. "
        f"Likely cause: {cause}. This mismatch is ATTRIBUTED, not silently rolled "
        "back; quiesce the writers and re-run (the upsert is idempotent on "
        "(tenant, collection, chash))."
    )
