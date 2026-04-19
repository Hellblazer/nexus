# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Per-stage intra-file timing accumulator (nexus-7niu, vatx Gap 4b).

A small, thread-safe helper that attributes wall-clock time inside
per-file indexer functions to four orthogonal buckets:

  chunking   — AST / markdown / PDF chunk extraction
  embed      — Voyage / local embedding calls
  upload     — T3 upsert + chash dual-write + taxonomy assign
  retry      — attributed via delta-snapshot on nexus.retry counters

When the operator passes ``nx index repo --debug-timing``, the CLI
collects one :class:`StageTimers` per file and prints an aggregate
breakdown at end-of-run, so the 89–95 s spikes surfaced by the
parent bead (nexus-vatx) can be decomposed into
"how much was voyage?" vs "how much was chromadb?" vs
"how much was chunking a giant file?"

Silent by default. When ``IndexContext.stage_timers`` is ``None``,
callers pay no overhead and emit nothing.

Scope note: only the ``code_indexer`` site is instrumented in the
first PR (nexus-7niu scaffold). Follow-up PRs wire the same surface
into ``prose_indexer``, ``doc_indexer``, and ``pipeline_stages``.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

_STAGE_NAMES: tuple[str, ...] = ("chunking", "embed", "upload")


@dataclass
class StageTimers:
    """Accumulate per-stage elapsed time for a single file index.

    All values are in seconds. Each call to :meth:`stage` — used as a
    ``with`` block — measures wall-clock elapsed AND snapshots
    ``nexus.retry.get_retry_stats()`` before + after to attribute any
    transient-error backoff sleep that occurred inside the block to
    ``retry_s`` rather than to the stage itself. So "voyage took 90 s
    but 60 s of that was retry sleeps" surfaces as
    ``embed_s=30, retry_s=60`` — not ``embed_s=90``, which would hide
    the real cause of the stall.
    """

    chunking_s: float = 0.0
    embed_s: float = 0.0
    upload_s: float = 0.0
    retry_s: float = 0.0

    # Constructed by __post_init__ so the lock does not leak into the
    # auto-generated __eq__ / __repr__. Pydantic-free: pure dataclass.
    _lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False,
    )

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        """Time the wrapped block and add net-of-retry to *name*.

        ``name`` must be one of ``"chunking"``, ``"embed"``, ``"upload"``.
        Unknown names raise ``ValueError`` at entry so a typo fails
        loudly at first call, not silently at report time.
        """
        if name not in _STAGE_NAMES:
            raise ValueError(
                f"unknown stage {name!r}; valid: {_STAGE_NAMES}"
            )
        from nexus.retry import get_retry_stats

        pre = get_retry_stats()["total_seconds"]
        t0 = time.monotonic()
        try:
            yield
        finally:
            elapsed = time.monotonic() - t0
            retry_delta = max(
                0.0, get_retry_stats()["total_seconds"] - pre,
            )
            # Clamp net to >= 0 — retry_delta can race slightly ahead of
            # elapsed under extreme clock jitter.
            net = max(0.0, elapsed - retry_delta)
            with self._lock:
                if name == "chunking":
                    self.chunking_s += net
                elif name == "embed":
                    self.embed_s += net
                else:  # "upload"
                    self.upload_s += net
                self.retry_s += retry_delta

    def snapshot(self) -> dict[str, float]:
        """Thread-safe snapshot of the four accumulators."""
        with self._lock:
            return {
                "chunking_s": self.chunking_s,
                "embed_s": self.embed_s,
                "upload_s": self.upload_s,
                "retry_s": self.retry_s,
            }

    def total_s(self) -> float:
        """Sum of all four buckets — convenience for reports."""
        s = self.snapshot()
        return sum(s.values())


def aggregate(timers: list[StageTimers]) -> dict[str, float]:
    """Sum a list of per-file :class:`StageTimers` into totals.

    Returns a dict with the same four keys as :meth:`StageTimers.snapshot`
    plus ``total_s``. Empty input returns all zeros (no files recorded).
    """
    totals = {
        "chunking_s": 0.0,
        "embed_s": 0.0,
        "upload_s": 0.0,
        "retry_s": 0.0,
    }
    for t in timers:
        s = t.snapshot()
        for k in totals:
            totals[k] += s[k]
    totals["total_s"] = sum(
        v for k, v in totals.items() if k != "total_s"
    )
    return totals


def format_report(totals: dict[str, float], *, n_files: int) -> str:
    """Human-readable per-stage breakdown for end-of-run stderr.

    Empty totals (no recorded files) return a brief single-line
    message rather than a zero-filled table so the output stays
    signal-dense.
    """
    grand = totals.get("total_s", 0.0)
    if grand <= 0 or n_files <= 0:
        return "[debug-timing] no per-stage samples recorded"
    lines = [f"[debug-timing] per-stage totals across {n_files} files:"]
    for stage in ("chunking_s", "embed_s", "upload_s", "retry_s"):
        seconds = totals.get(stage, 0.0)
        pct = (seconds / grand * 100) if grand > 0 else 0.0
        lines.append(f"  {stage:12} {seconds:>7.1f}s  ({pct:>4.1f}%)")
    lines.append(f"  {'total':12} {grand:>7.1f}s")
    return "\n".join(lines)
