# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-152 nexus-fjwxh / RDR-158 P2 nexus-jkzyq — T2 HTTP stores must satisfy the
frozen T2 store contract.

Since the storage_backend_for default flipped to ``service``, every CLI/MCP T2
op routes to an ``Http*Store``. Signature/method drift silently breaks the
surface in service mode (the T3 ``nexus-7zuzz`` incident, generalized to T2).
This is the standing tripwire: each HTTP store's public method set must COVER
the contract's, and shared-prefix parameter names must match. Every exclusion
carries a written reason.

RDR-158 P2 (D2) re-grounded the oracle. The tripwire formerly compared each
``Http*`` store against its LIVE SQLite store class. That oracle is deleted in
RDR-158 P4 (nexus-i711w), which would take the tripwire down with it. The oracle
is now the FROZEN, machine-generated contract in ``t2_store_contract.py``
(``T2_STORE_CONTRACT``) — a snapshot of the nine SQLite stores' public surface,
captured while they were still live. The durable tripwire below imports NO
SQLite store class; it survives P4. A separate, P4-deletable faithfulness guard
(``test_contract_matches_live_sqlite_oracle``) cross-checks the frozen snapshot
against the live SQLite classes for as long as they exist.

This is a static signature guard (cheap, no service needed): method names and
shared-prefix parameter names, not return-VALUE shape. Return-SHAPE parity
(dict keys) is NOT currently exercised anywhere -- the ``-m integration``
suites in ``tests/db/test_http_*_integration.py`` call the real service but
do not assert on result dict key-sets. A caller that assumes a key present in
one backend's row shape survives a backend swap (e.g. nexus-c4143-adjacent:
``search_cmd``'s 2026-07-11 ``r['distance']`` KeyError, ``T1Database.search``
vs ``HttpScratchStore.search``) is undefended by this file. See nexus-w2q0s.
"""
from __future__ import annotations

import inspect

import pytest

from tests.db.t2_store_contract import _UNIVERSAL_IGNORE, T2_STORE_CONTRACT

# (label, http_class_path). The SQLite oracle path is no longer needed at
# tripwire time — the frozen contract IS the oracle. The mapping from label to
# the (now-doomed) SQLite class lives in _SQLITE_ORACLES below, used ONLY by the
# P4-deletable faithfulness guard.
_STORE_PAIRS = [
    ("memory", "nexus.db.t2.http_memory_store:HttpMemoryStore"),
    ("plans", "nexus.db.t2.http_plan_library:HttpPlanLibrary"),
    ("telemetry", "nexus.db.t2.http_telemetry_store:HttpTelemetryStore"),
    ("chash_index", "nexus.db.t2.http_chash_index:HttpChashIndex"),
    ("document_aspects",
     "nexus.db.t2.http_document_aspects_store:HttpDocumentAspectsStore"),
    ("document_highlights",
     "nexus.db.t2.http_document_highlights_store:HttpDocumentHighlightsStore"),
    ("aspect_queue", "nexus.db.t2.http_aspect_queue:HttpAspectQueue"),
    ("taxonomy", "nexus.db.t2.http_taxonomy_store:HttpTaxonomyStore"),
    ("scratch", "nexus.db.http_scratch_store:HttpScratchStore"),
]

# Per-store method exclusions: the HTTP store legitimately does NOT cover these
# contract methods. Every entry needs a written reason.
#
# RDR-152 nexus-1di3r Phase 6: the taxonomy compute/persist/orchestrator pipeline
# is now FULLY service-backed on HttpTaxonomyStore (delegate-thin compute statics +
# centroid-port ANN + Java relational persist), so there are NO taxonomy exclusions
# left — the tripwire is strict for every taxonomy method. RF-158-1 established
# zero exemptions across all nine pairs; re-adding any entry here is a documented,
# auditable act. RDR-182 P1.2 (nexus-ykzbj.6) is the first re-addition:
# ``record_consent`` is deliberately SQLite-only per the bead's explicit "raw
# sqlite3" scope — the Java engine has no
# ``claude_assisted_remediation_consents`` table nor a
# ``/v1/telemetry/consents/record`` endpoint yet. In service mode a caller
# invoking ``db.telemetry.record_consent(...)`` gets a loud ``AttributeError``
# (HttpTelemetryStore has no such method), never a silently-dropped consent
# row — the same fail-loud posture ``record_hook_failure`` closed for
# tier_writes. Service-mode parity is bead nexus-ng2sy; until it lands,
# the RDR-182 consent audit only has coverage on local (non-service) T2.
# RF-158-1: zero exemptions across all nine pairs. The RDR-182 record_consent/
# list_consents exclusions were removed 2026-07-13 when nexus-ng2sy landed the
# engine-side consent table + /consents/{record,list} routes + the
# HttpTelemetryStore twins — the parity tripwire is strict again.
_EXCLUSIONS: dict[str, dict[str, str]] = {}

# Per-(store, method) param-drift exemptions: the method exists on both the
# contract and the HTTP store and is genuinely USED in service mode, but with a
# different (working) signature than the contract. Every entry needs a written
# reason.
#
# RDR-152 nexus-1di3r Phase 6: get_topics was reconciled to the contract's
# (*, parent_id=None) signature (Phase 4.3), so the taxonomy param-drift exemption
# is gone — the tripwire enforces signature-prefix parity for it too.
_PARAM_DRIFT_OK: dict[tuple[str, str], str] = {}


def _load(path: str) -> type:
    mod, _, cls = path.partition(":")
    import importlib

    return getattr(importlib.import_module(mod), cls)


def _is_method_like(x: object) -> bool:
    # Classmethods bind as ``method`` objects, not ``function`` objects, so a bare
    # ``inspect.isfunction`` predicate silently drops them (e.g.
    # ``CatalogTaxonomy.compute_cross_links``, a production-called @classmethod).
    # Include both so the surface is complete. Staticmethods are already functions.
    return inspect.isfunction(x) or inspect.ismethod(x)


def _public_methods(cls: type) -> set[str]:
    return {
        name
        for name, _ in inspect.getmembers(cls, predicate=_is_method_like)
        if not name.startswith("_") and name not in _UNIVERSAL_IGNORE
    }


@pytest.mark.parametrize("label,http_path", _STORE_PAIRS,
                         ids=[p[0] for p in _STORE_PAIRS])
def test_http_store_covers_contract(label, http_path):
    """Every contract public method (minus documented exclusions) exists on the
    HTTP store — a method the CLI/MCP can call must not vanish in service mode."""
    contract_methods = set(T2_STORE_CONTRACT[label])
    http_methods = _public_methods(_load(http_path))
    excluded = set(_EXCLUSIONS.get(label, {}))

    required = contract_methods - excluded
    missing = required - http_methods

    assert not missing, (
        f"{label}: HttpStore is missing contract methods {sorted(missing)}.\n"
        f"  Either implement them on the HTTP store, or add a documented "
        f"exclusion to _EXCLUSIONS['{label}'] explaining why service mode omits "
        f"them (and that the CLI/MCP path fails loud, never silently)."
    )


@pytest.mark.parametrize("label,http_path", _STORE_PAIRS,
                         ids=[p[0] for p in _STORE_PAIRS])
def test_shared_method_param_prefix_matches(label, http_path):
    """For methods on BOTH the contract and the HTTP store, the HTTP signature's
    shared parameter prefix must match the contract (HTTP may add trailing
    params, never reorder/rename the prefix) — the drift that broke git-hook
    indexing in nexus-7zuzz."""
    http_cls = _load(http_path)
    contract = T2_STORE_CONTRACT[label]

    # Only methods absent from the HTTP store (the _EXCLUSIONS set) are excused
    # from the param check — they literally cannot be param-compared. Methods
    # present on BOTH MUST match: signature drift on a shared method is the exact
    # nexus-7zuzz crash class. An earlier per-STORE skip here over-broadly
    # silenced that drift (critic catch).
    excused = set(_EXCLUSIONS.get(label, {}))
    shared = (set(contract) & _public_methods(http_cls)) - excused
    mismatches = []
    for m in sorted(shared):
        if (label, m) in _PARAM_DRIFT_OK:
            continue
        s_params = contract[m]
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
            f"  {m}: contract={s} http={h}" for m, s, h in mismatches
        )
    )


def test_exclusions_are_real_contract_methods():
    """Guard the guard: every excluded name must actually be a method in the
    frozen contract, so a typo can't silently neuter the coverage check."""
    for label, excl in _EXCLUSIONS.items():
        methods = set(T2_STORE_CONTRACT[label])
        bogus = set(excl) - methods
        assert not bogus, (
            f"{label}: _EXCLUSIONS lists names that are not contract methods "
            f"(typo / stale?): {sorted(bogus)}"
        )


def test_contract_covers_all_store_pairs():
    """The frozen contract and the live HTTP pair list must enumerate the same
    nine stores — neither side may drift a store in or out unnoticed."""
    contract_labels = set(T2_STORE_CONTRACT)
    pair_labels = {p[0] for p in _STORE_PAIRS}
    assert contract_labels == pair_labels, (
        f"contract vs _STORE_PAIRS store-set drift: "
        f"only-in-contract={sorted(contract_labels - pair_labels)} "
        f"only-in-pairs={sorted(pair_labels - contract_labels)}"
    )


# ---------------------------------------------------------------------------
# P4-DELETE-MARKER (nexus-i711w): faithfulness guard. This is the ONLY thing in
# this file that references a SQLite store class. It confirms the frozen contract
# still matches the live SQLite oracle for as long as the SQLite classes exist.
#
# When RDR-158 P4 deletes the SQLite store classes, DELETE this whole block: the
# `_SQLITE_ORACLES` map and `test_contract_matches_live_sqlite_oracle`. The
# references are STRING-LITERAL module paths (e.g. "nexus.db.t2.memory_store"),
# NOT `import` statements — grep for the literal `# P4-DELETE-MARKER` token and
# for the SQLite module-path strings in `_SQLITE_ORACLES`, not for `import`
# lines. Leaving this behind after the modules are gone causes nine
# ModuleNotFoundError at parametrize/collection time, not a quiet skip. The
# durable tripwire above does NOT depend on this block.
# ---------------------------------------------------------------------------
_SQLITE_ORACLES = {
    "memory": "nexus.db.t2.memory_store:MemoryStore",
    "plans": "nexus.db.t2.plan_library:PlanLibrary",
    "telemetry": "nexus.db.t2.telemetry:Telemetry",
    "chash_index": "nexus.db.t2.chash_index:ChashIndex",
    "document_aspects": "nexus.db.t2.document_aspects:DocumentAspects",
    "document_highlights": "nexus.db.t2.document_highlights:DocumentHighlights",
    "aspect_queue": "nexus.db.t2.aspect_extraction_queue:AspectExtractionQueue",
    "taxonomy": "nexus.db.t2.catalog_taxonomy:CatalogTaxonomy",
    "scratch": "nexus.db.t1:T1Database",
}


@pytest.mark.parametrize("label", sorted(_SQLITE_ORACLES),
                         ids=sorted(_SQLITE_ORACLES))
def test_contract_matches_live_sqlite_oracle(label):
    """P4-DELETE: the frozen contract must reproduce the live SQLite oracle's
    public surface (method names + ordered non-self params) exactly, so the
    freeze is provably faithful while the SQLite classes still exist."""
    sqlite_cls = _load(_SQLITE_ORACLES[label])
    live = {}
    for name, fn in inspect.getmembers(sqlite_cls, predicate=_is_method_like):
        if name.startswith("_") or name in _UNIVERSAL_IGNORE:
            continue
        live[name] = [
            p for p in inspect.signature(fn).parameters if p != "self"
        ]
    assert live == T2_STORE_CONTRACT[label], (
        f"{label}: frozen contract drifted from the live SQLite oracle. "
        f"Regenerate t2_store_contract.py from the live classes (this guard "
        f"is deleted in RDR-158 P4 once the SQLite classes go)."
    )
