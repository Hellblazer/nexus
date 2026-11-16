# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The RDR-192 backfill rung's residual memo: a durable, per-tenant T2 note of the last failed attempt.

Moved out of :mod:`nexus.upgrade_ladder.rungs.rdr192_manifest_backfill` (file-size ratchet, nexus-6pfgb.43), which
re-exports the four public names below.
"""
from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from typing import Any, Protocol

import structlog

from nexus.upgrade_ladder.registry import RUNG_RDR192_MANIFEST_BACKFILL

_log = structlog.get_logger(__name__)

#: T2 note holding the last failed attempt's residual fingerprint.
MEMO_PROJECT = "upgrade_ladder_state"
MEMO_TITLE = f"{RUNG_RDR192_MANIFEST_BACKFILL}.residual"
_MEMO_TTL_DAYS = 30


class ResidualMemo(Protocol):
    """Durable, per-tenant note of the last failed attempt."""

    def load(self) -> dict[str, Any] | None: ...
    def save(self, note: dict[str, Any]) -> None: ...
    def clear(self) -> None: ...


class T2ResidualMemo:
    """The memo as a T2 memory note (engine-backed PG, per tenant). No local
    state file and no new table: T2 memory already carries operational state
    of this kind (``taxonomy_discover_health``). Every failure degrades to
    'no memo', which only costs a retry."""

    @contextlib.contextmanager
    def _store(self) -> Iterator[Any]:
        from nexus.db.t2.http_memory_store import HttpMemoryStore  # noqa: PLC0415 — deferred; keeps cold CLI start cheap

        store = HttpMemoryStore()
        try:
            yield store
        finally:
            store.close()

    def load(self) -> dict[str, Any] | None:
        try:
            with self._store() as store:
                row = store.get(project=MEMO_PROJECT, title=MEMO_TITLE)
            return json.loads(row["content"]) if row else None
        except Exception as exc:  # noqa: BLE001 — a memo that cannot be read is no memo
            _log.debug("rdr192_residual_memo_load_failed", error=str(exc))
            return None

    def save(self, note: dict[str, Any]) -> None:
        try:
            with self._store() as store:
                store.put(
                    MEMO_PROJECT, MEMO_TITLE, json.dumps(note, sort_keys=True),
                    tags="upgrade-ladder,rdr192", ttl=_MEMO_TTL_DAYS, agent="nx-upgrade",
                )
        except Exception as exc:  # noqa: BLE001 — losing the memo costs one retry, never correctness
            _log.warning("rdr192_residual_memo_save_failed", error=str(exc))

    def clear(self) -> None:
        try:
            with self._store() as store:
                store.delete(project=MEMO_PROJECT, title=MEMO_TITLE)
        except Exception as exc:  # noqa: BLE001 — a stale memo is harmless: it is keyed on the residual fingerprint
            _log.debug("rdr192_residual_memo_clear_failed", error=str(exc))
