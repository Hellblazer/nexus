# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-152 nexus-fjwxh — T2 HTTP stores must be drop-ins for their SQLite oracles.

Since the storage_backend_for default flipped to ``service``, every CLI/MCP T2
op routes to an ``Http*Store`` instead of the SQLite store. Signature/method
drift silently breaks the surface in service mode (the T3 ``nexus-7zuzz``
incident, generalized to T2). This is the standing tripwire: each HTTP store's
public method set must COVER its SQLite oracle's, and shared-prefix parameter
names must match. Every exclusion carries a written reason.

This is a static signature guard (cheap, no service needed). Return-SHAPE
parity (dict keys) is exercised behaviourally by the ``-m integration``
suites in ``tests/db/test_http_*_integration.py`` against the real service.
"""
from __future__ import annotations

import inspect

import pytest

# (label, sqlite_class_path, http_class_path)
_STORE_PAIRS = [
    ("memory", "nexus.db.t2.memory_store:MemoryStore",
     "nexus.db.t2.http_memory_store:HttpMemoryStore"),
    ("plans", "nexus.db.t2.plan_library:PlanLibrary",
     "nexus.db.t2.http_plan_library:HttpPlanLibrary"),
    ("telemetry", "nexus.db.t2.telemetry:Telemetry",
     "nexus.db.t2.http_telemetry_store:HttpTelemetryStore"),
    ("chash_index", "nexus.db.t2.chash_index:ChashIndex",
     "nexus.db.t2.http_chash_index:HttpChashIndex"),
    ("document_aspects", "nexus.db.t2.document_aspects:DocumentAspects",
     "nexus.db.t2.http_document_aspects_store:HttpDocumentAspectsStore"),
    ("document_highlights", "nexus.db.t2.document_highlights:DocumentHighlights",
     "nexus.db.t2.http_document_highlights_store:HttpDocumentHighlightsStore"),
    ("aspect_queue", "nexus.db.t2.aspect_extraction_queue:AspectExtractionQueue",
     "nexus.db.t2.http_aspect_queue:HttpAspectQueue"),
    ("taxonomy", "nexus.db.t2.catalog_taxonomy:CatalogTaxonomy",
     "nexus.db.t2.http_taxonomy_store:HttpTaxonomyStore"),
    ("scratch", "nexus.db.t1:T1Database",
     "nexus.db.http_scratch_store:HttpScratchStore"),
]

# Per-store method exclusions: the HTTP store legitimately does NOT cover these
# SQLite methods. Every entry needs a written reason.
_EXCLUSIONS: dict[str, dict[str, str]] = {
    "taxonomy": {
        m: (
            "Taxonomy compute/rebuild pipeline — heavy BERTopic/HDBSCAN compute "
            "coupled to raw ChromaDB centroids + the T2 rename lock; not exposed "
            "over HTTP. nx taxonomy discover/rebuild/project/split fail loud in "
            "service mode (taxonomy_cmd guards with _has_raw_access). Tracked as "
            "nexus-1di3r; read-path methods ARE covered."
        )
        for m in (
            "discover_topics", "rebuild_taxonomy", "project_against",
            "compute_assignments",
            "compute_discovered_topics", "compute_rebuild_plan", "compute_split",
            "assign_batch", "assign_single", "persist_assignments",
            "persist_cross_links", "persist_discovered_topics",
            "persist_rebuild_topics", "read_rebuild_old_state",
            "purge_collection", "split_topic",
        )
    },
}

# Stores excused from the strict shared-prefix parameter check. taxonomy's HTTP
# store is a known-incomplete port (nexus-1di3r): beyond the 17 missing
# compute/rebuild methods, several READ methods (get_topics, get_topic_tree,
# get_topics_for_collection, upsert_topic_links, chunk_grounded_in) carry
# DRIFTED signatures vs the SQLite oracle, so nx taxonomy read commands are not
# yet parity-safe in service mode. Locking the param check here would freeze the
# drift in; nexus-1di3r owns bringing the store to full parity.
_PARAM_DRIFT_EXCUSED: dict[str, str] = {
    "taxonomy": "nexus-1di3r — HttpTaxonomyStore read-method signatures drift "
                "from CatalogTaxonomy; full parity is the follow-on's scope.",
}

# Methods present on every store base but not part of the storage contract.
_UNIVERSAL_IGNORE = {"close", "conn", "bootstrap_schema"}


def _load(path: str) -> type:
    mod, _, cls = path.partition(":")
    import importlib

    return getattr(importlib.import_module(mod), cls)


def _public_methods(cls: type) -> set[str]:
    return {
        name
        for name, _ in inspect.getmembers(cls, predicate=inspect.isfunction)
        if not name.startswith("_") and name not in _UNIVERSAL_IGNORE
    }


@pytest.mark.parametrize("label,sqlite_path,http_path", _STORE_PAIRS,
                         ids=[p[0] for p in _STORE_PAIRS])
def test_http_store_covers_sqlite_oracle(label, sqlite_path, http_path):
    """Every SQLite public method (minus documented exclusions) exists on the
    HTTP store — a method the CLI/MCP can call in SQLite mode must not vanish
    in service mode."""
    sqlite_cls = _load(sqlite_path)
    http_cls = _load(http_path)

    sqlite_methods = _public_methods(sqlite_cls)
    http_methods = _public_methods(http_cls)
    excluded = set(_EXCLUSIONS.get(label, {}))

    required = sqlite_methods - excluded
    missing = required - http_methods

    assert not missing, (
        f"{label}: HttpStore is missing SQLite-oracle methods {sorted(missing)}.\n"
        f"  Either implement them on the HTTP store, or add a documented "
        f"exclusion to _EXCLUSIONS['{label}'] explaining why service mode omits "
        f"them (and that the CLI/MCP path fails loud, never silently)."
    )


@pytest.mark.parametrize("label,sqlite_path,http_path", _STORE_PAIRS,
                         ids=[p[0] for p in _STORE_PAIRS])
def test_shared_method_param_prefix_matches(label, sqlite_path, http_path):
    """For methods on BOTH, the HTTP signature's shared parameter prefix must
    match the SQLite oracle (HTTP may add trailing params, never reorder/rename
    the prefix) — the drift that broke git-hook indexing in nexus-7zuzz."""
    if label in _PARAM_DRIFT_EXCUSED:
        pytest.skip(_PARAM_DRIFT_EXCUSED[label])

    sqlite_cls = _load(sqlite_path)
    http_cls = _load(http_path)

    shared = (_public_methods(sqlite_cls) & _public_methods(http_cls))
    mismatches = []
    for m in sorted(shared):
        s_params = [
            p for p in inspect.signature(getattr(sqlite_cls, m)).parameters
            if p != "self"
        ]
        h_params = [
            p for p in inspect.signature(getattr(http_cls, m)).parameters
            if p != "self"
        ]
        prefix = h_params[: len(s_params)]
        if prefix != s_params:
            mismatches.append((m, s_params, h_params))

    assert not mismatches, (
        f"{label}: shared-prefix parameter drift:\n"
        + "\n".join(
            f"  {m}: sqlite={s} http={h}" for m, s, h in mismatches
        )
    )


def test_exclusions_are_real_sqlite_methods():
    """Guard the guard: every excluded name must actually be a method on the
    SQLite oracle, so a typo can't silently neuter the coverage check."""
    for label, excl in _EXCLUSIONS.items():
        sqlite_path = next(p[1] for p in _STORE_PAIRS if p[0] == label)
        methods = _public_methods(_load(sqlite_path))
        bogus = set(excl) - methods
        assert not bogus, (
            f"{label}: _EXCLUSIONS lists names that are not SQLite methods "
            f"(typo / stale?): {sorted(bogus)}"
        )
