# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-186 P2 (nexus-146xx.11): the in-process completion holder.

RF-186-2's explicit P2 obligation: the ladder runs when the engine may be
absent (its own rungs install/converge it), so pre-engine completion
state is held in-process and the holder serves ``verified_rungs()`` for
later rungs within the SAME walk while the durable backend is down.
``_converge_preconditions()`` normally brings the engine up before
``_run_ladder()``; this covers only the engine-defer window.

Degradation contract (RF-186-2, deliberate — NOT the silent-fallback
class): completion records are position bookkeeping, never truth. A
record that misses the backend costs one redundant read-only ``verify()``
in a later process, and a crash inside the window costs an idempotent
re-derivation (RDR-142) — never correctness. Backend unavailability is
therefore warned and tolerated, and every miss stays visible in
:meth:`unflushed` (the flush-once-engine-up seam, nexus-146xx.12).

No position surface here at all: ladder position stays DERIVED from
``verified_rungs()`` through the single ``completion.derive_ladder_position``
algorithm (RDR-185 Gap-4 mechanism 1) — the holder never grows a second
copy of that derivation.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import TypeVar

import structlog

from nexus.upgrade_ladder.completion import CompletionRecord
from nexus.upgrade_ladder.protocol import CompletionLedger

_log = structlog.get_logger(__name__)

_T = TypeVar("_T")


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class InProcessCompletionHolder:
    """In-memory overlay over a durable :class:`CompletionLedger` backend.

    Writes land in memory ALWAYS and write through to the backend when it
    is up; reads serve the union (memory wins for a rung recorded in this
    process — it is the newest fact). Reads never flush: flushing owed
    records once the engine is up is nexus-146xx.12's job, kept off the
    read path so a read-only sweep stays read-only.

    ``now_fn`` is the injectable clock seam (deterministic tests) — the
    house shape every completion surface uses.

    NOT thread-safe by design: the holder lives inside one ladder walk in
    one process (the ``_run_ladder`` invocation) — plain dicts, no locks.
    """

    def __init__(
        self,
        backend: CompletionLedger,
        *,
        now_fn: Callable[[], str] | None = None,
    ) -> None:
        self._backend = backend
        self._now_fn = now_fn if now_fn is not None else _utc_now_iso
        self._memory: dict[str, CompletionRecord] = {}
        self._unflushed: dict[str, CompletionRecord] = {}

    # ── recording ────────────────────────────────────────────────────────────

    def record_verified(self, rung_name: str, *, package_version: str, detail: str = "") -> None:
        """Hold the verified fact in-process; write through when the backend
        is up. Callers (the runner) invoke this ONLY after ``Rung.verify()``
        passed — the holder adds no unverified-completion shape."""
        record = CompletionRecord(
            rung_name=rung_name,
            verified_at=self._now_fn(),
            package_version=package_version,
            detail=detail,
        )
        self._memory[rung_name] = record
        try:
            self._backend.record_verified(
                rung_name, package_version=package_version, detail=detail
            )
        except Exception as exc:  # noqa: BLE001 — engine-defer window: bookkeeping loss costs one redundant verify (RF-186-2), never correctness; stays owed in unflushed()
            _log.warning(
                "ladder_completion_writethrough_deferred",
                rung=rung_name,
                error=str(exc),
            )
            self._unflushed[rung_name] = record
        else:
            self._unflushed.pop(rung_name, None)

    # ── reading / derivation ─────────────────────────────────────────────────

    def verified_rungs(self) -> frozenset[str]:
        return frozenset(self._memory) | self._read_backend(
            self._backend.verified_rungs, frozenset()
        )

    def completions(self) -> dict[str, CompletionRecord]:
        """Backend facts overlaid by this process's records (newest wins)."""
        return {**self._read_backend(self._backend.completions, {}), **self._memory}

    def unflushed(self) -> dict[str, CompletionRecord]:
        """Records held in-process that have NOT reached the backend — what
        the engine-up flush (nexus-146xx.12) owes durability."""
        return dict(self._unflushed)

    def flush(self) -> int:
        """Retry every owed record against the backend — the end-of-walk
        flush (nexus-146xx.12): the walk's own rungs may have brought the
        engine up AFTER earlier records missed it.

        Re-recording is a safe idempotent upsert; the engine re-stamps
        ``verified_at`` at flush time by design (RF-186-2 lossy audit
        metadata — see ``http_store``'s module docstring). Records that
        still miss stay owed (warned per record by the same degradation
        contract as the write-through); a record lost with the process
        costs one idempotent re-derivation, never correctness.

        Returns the number of records STILL owed after the attempt.
        """
        for rung_name, record in list(self._unflushed.items()):
            try:
                self._backend.record_verified(
                    rung_name,
                    package_version=record.package_version,
                    detail=record.detail,
                )
            except (AttributeError, TypeError):
                raise  # contract violation — never masked as an outage
            except Exception as exc:  # noqa: BLE001 — still down: stays owed; loss costs a redundant verify (RF-186-2)
                _log.warning(
                    "ladder_completion_flush_deferred", rung=rung_name, error=str(exc)
                )
            else:
                self._unflushed.pop(rung_name, None)
        remaining = len(self._unflushed)
        if remaining:
            _log.warning("ladder_completion_flush_incomplete", owed=remaining)
        return remaining

    def _read_backend(self, reader: Callable[[], _T], fallback: _T) -> _T:
        """One read through the backend with the engine-defer degradation.

        The degradation is ONLY for genuine backend unavailability. A
        contract violation — the backend missing a ``CompletionLedger``
        method (``AttributeError`` at bind time) or a signature mismatch
        (``TypeError``) — propagates LOUD: masking a programming error as
        a transient outage is the silent-fallback class the reviewers
        flagged (2026-07-18) and Hal's directives ban.
        """
        try:
            return reader()
        except (AttributeError, TypeError):
            raise
        except Exception as exc:  # noqa: BLE001 — engine down: serve in-process state; a stale union under-reports at worst, costing a redundant read-only verify
            _log.warning("ladder_completion_backend_read_deferred", error=str(exc))
            return fallback
