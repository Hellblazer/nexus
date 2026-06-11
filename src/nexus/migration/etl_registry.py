# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-153 Phase 2→3 seam: the uniform interface over the seven T2 ETLs.

The per-store ``migrate_*`` entry points have heterogeneous signatures
(``memory_etl.migrate_memory_rows(path, store, ...)``,
``aspects_etl.migrate_all(path, http_aspects, http_highlights, http_queue,
..., catalog_db_path=...)``, ``catalog_etl.migrate_catalog(path, client,
...)``). The Phase-3 orchestrator (``nx storage migrate all``) must run
them in the RDR-152 Phase 2 ladder order with one shared
:class:`~nexus.migration.migration_report.IssueCollector` — this module
is the single place that order and that adapter contract live, so the
seven calls are reviewable side by side (P2 critique S5).

Phase 3 registers one :class:`StoreEtl` per store; the ``run`` callable
closes over whatever HTTP store/client construction that store needs and
accepts exactly ``(sources, collector)``.

This module lives in ``src/nexus/migration/`` (NOT ``src/nexus/db/t2/``)
because the RDR-152 P4 deletion of the SQLite subtree takes the ETLs with
it while the report tooling survives; at P4 the registry entries are
deleted alongside their ETLs and this module keeps only the order
constant for the historical record.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

__all__ = ["LADDER_ORDER", "EtlSources", "StoreEtl", "MigrateRunner"]

#: RDR-152 Phase 2 ladder order — memory first (smallest, fastest
#: validation), catalog LAST (graph-heavy; every other store's FK targets
#: must exist before its links land). The Phase-3 orchestrator MUST run
#: stores in exactly this order.
LADDER_ORDER: tuple[str, ...] = (
    "memory",
    "plans",
    "telemetry",
    "taxonomy",
    "aspects",
    "chash",
    "catalog",
)


@dataclass(frozen=True)
class EtlSources:
    """The read-only source paths every ETL draws from.

    ``sqlite_path`` is the T2 ``memory.db``; ``catalog_db_path`` is the
    sibling ``.catalog.db`` (the valid-tumbler source for the aspects/queue
    orphan pre-checks AND the catalog ETL's own source).
    """

    sqlite_path: Path
    catalog_db_path: Path


class MigrateRunner(Protocol):
    """One store's migration: read from *sources*, record into *collector*.

    Returns the store's native result dict (shape varies per store; the
    REPORT is built from the collector, not from this return value).
    """

    def __call__(self, sources: EtlSources, collector: Any) -> dict: ...


@dataclass(frozen=True)
class StoreEtl:
    """Registry entry: a store name (must appear in :data:`LADDER_ORDER`)
    plus its runner."""

    store: str
    run: MigrateRunner

    def __post_init__(self) -> None:
        if self.store not in LADDER_ORDER:
            raise ValueError(
                f"unknown store {self.store!r} — not in LADDER_ORDER "
                f"{LADDER_ORDER}"
            )


def ordered(etls: list[StoreEtl]) -> list[StoreEtl]:
    """Sort registry entries into ladder order; reject duplicates."""
    by_store: dict[str, StoreEtl] = {}
    for etl in etls:
        if etl.store in by_store:
            raise ValueError(f"duplicate StoreEtl for {etl.store!r}")
        by_store[etl.store] = etl
    return [by_store[s] for s in LADDER_ORDER if s in by_store]
