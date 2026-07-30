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

from tests.db.t2_store_contract import (
    T2_STORE_RETURNS,
    _UNIVERSAL_IGNORE,
    T2_STORE_CONTRACT,
    T2_SUPPLEMENTAL_CONTRACT,
)


def _required(label: str) -> dict[str, list[str]]:
    """The full surface the Http store owes for *label*.

    The primary contract can only describe methods with a SQLite twin, since it
    was snapshotted from the SQLite oracle. Service-mode-only methods have no
    twin and are invisible to it — so the checks below consume the UNION
    (nexus-tdkg1.1). Merged here rather than at each call site so coverage and
    param-prefix cannot drift apart over which surface they enforce.
    """
    merged = dict(T2_STORE_CONTRACT[label])
    merged.update(T2_SUPPLEMENTAL_CONTRACT.get(label, {}))
    return merged

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
    HTTP store — a method the CLI/MCP can call must not vanish in service mode.

    Covers the primary contract UNION the supplemental one, so service-mode-only
    methods are guarded too (nexus-tdkg1.1)."""
    contract_methods = set(_required(label))
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
    contract = _required(label)

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
        # Against the UNION, not the primary contract alone. _EXCLUSIONS is
        # consumed against _required(label) by both live checks, so validating
        # it against a narrower set would reject a legitimate exclusion for a
        # service-mode-only method as "not a real contract method" (code review,
        # nexus-tdkg1.1). Inert today (_EXCLUSIONS is empty) and it fails loud
        # rather than permitting drift — but the two must agree on one surface.
        methods = set(_required(label))
        bogus = set(excl) - methods
        assert not bogus, (
            f"{label}: _EXCLUSIONS lists names that are not contract methods "
            f"(typo / stale?): {sorted(bogus)}"
        )


@pytest.mark.parametrize("label,http_path", _STORE_PAIRS,
                         ids=[p[0] for p in _STORE_PAIRS])
def test_supplemental_is_service_mode_only(label, http_path):
    """Guard the guard (nexus-tdkg1.1). Two ways the supplemental contract could
    silently cover nothing, both rejected here:

    * a name that is NOT on the Http store — a typo or a rename would otherwise
      sit in the dict forever, looking like protection while guarding a method
      that does not exist;
    * a name that IS in the primary contract — that method has a SQLite twin and
      is already covered, so listing it here is a duplicate whose real effect is
      to imply service-mode-only status it does not have. The bead that
      commissioned this listed exactly one such entry
      (``document_aspects.rename_collection``), which is why this check exists
      rather than a comment asking people to be careful.
    """
    supplemental = T2_SUPPLEMENTAL_CONTRACT.get(label, {})
    if not supplemental:
        pytest.skip(f"{label} declares no service-mode-only methods")

    http_methods = _public_methods(_load(http_path))
    missing = set(supplemental) - http_methods
    assert not missing, (
        f"{label}: T2_SUPPLEMENTAL_CONTRACT names methods absent from the Http "
        f"store {sorted(missing)} — a supplemental entry that matches nothing "
        f"guards nothing. Fix the name, or drop it if the method really went."
    )

    duplicated = set(supplemental) & set(T2_STORE_CONTRACT[label])
    assert not duplicated, (
        f"{label}: {sorted(duplicated)} are in BOTH contracts. The primary "
        f"contract is the SQLite oracle's surface, so anything in it has a "
        f"SQLite twin and is already covered — it is not service-mode-only. "
        f"Remove it from T2_SUPPLEMENTAL_CONTRACT."
    )


def test_supplemental_contract_is_not_empty():
    """Non-vacuity. Every assertion above passes trivially against an empty
    supplemental dict, so an accidental truncation would read as green while
    restoring exactly the blind spot this bead closed."""
    total = sum(len(v) for v in T2_SUPPLEMENTAL_CONTRACT.values())
    assert total >= 8, (
        f"T2_SUPPLEMENTAL_CONTRACT covers {total} methods; 8 permanent "
        "service-mode-only methods were identified across all nine Http* stores "
        "by full enumeration. A drop means coverage was lost, not that the "
        "problem went away — re-enumerate against the Http stores before "
        "lowering this floor.\n"
        "  NOTE this floor is DIRECTIONAL: it catches truncation, and cannot "
        "detect INCOMPLETENESS. The first cut of this dict passed a >= 4 floor "
        "while missing half the real set; only enumerating every store against "
        "`primary | supplemental` found the rest."
    )


@pytest.mark.parametrize(
    "label,victim",
    [(lbl, m) for lbl, ms in sorted(T2_SUPPLEMENTAL_CONTRACT.items())
     for m in sorted(ms)],
    ids=[f"{lbl}.{m}" for lbl, ms in sorted(T2_SUPPLEMENTAL_CONTRACT.items())
         for m in sorted(ms)],
)
def test_removing_a_supplemental_method_is_detected(label, victim):
    """Falsification: the coverage check must actually FAIL when a guarded
    service-mode-only method disappears.

    Without this, the union could be wired up wrongly — reading the supplemental
    dict but never comparing it — and every other test in this file would still
    pass. Runs the SAME predicate the real coverage check uses
    (``required - http_methods``) against a method set with the victim removed,
    and asserts it is reported missing. Set arithmetic, not a monkeypatched or
    subclassed store: an earlier docstring here claimed it shadowed the class,
    which would have sent a reader hunting for a fixture that does not exist
    (code review). Parametrized over EVERY supplemental entry, so adding one
    without wiring cannot pass on the strength of an older entry.

    The real deletion was also exercised by hand once: renaming
    ``rename_collection`` out of ``HttpDocumentHighlightsStore`` failed both
    ``test_http_store_covers_contract[document_highlights]`` and
    ``test_supplemental_is_service_mode_only[document_highlights]``.
    """
    http_cls = _load(dict(_STORE_PAIRS)[label])
    surviving = _public_methods(http_cls) - {victim}
    required = set(_required(label)) - set(_EXCLUSIONS.get(label, {}))

    assert victim in (required - surviving), (
        f"deleting {label}.{victim} went UNDETECTED — the coverage check is not "
        "consuming T2_SUPPLEMENTAL_CONTRACT, so this whole mechanism is "
        "decorative"
    )


# Uncovered by BOTH contracts, and deliberately so: the migration ETL family.
# These meet the mechanical entry criteria for T2_SUPPLEMENTAL_CONTRACT, but they
# are slated for whole-file deletion in the same 7.0.0 wave, alongside their
# ``*_etl.py`` callers and the engine's ``/import`` routes (nexus-i711w recon,
# T2 [21098]). Freezing a contract for methods being retired would manufacture
# work for the bead that deletes them. ``telemetry.probe_ids`` is in the family
# despite its name: its only src/ caller is telemetry_etl.py:725 (verify-fill).
#
# If the wave's disposition changes and any of these survive, MOVE it to
# T2_SUPPLEMENTAL_CONTRACT — do not simply delete the entry here, which would
# leave it silently uncovered.
_ETL_DYING_WITH_THE_WAVE: dict[str, set[str]] = {
    'aspect_queue': {'import_queue_batch', 'import_queue_row'},
    'chash_index': {'import_rows'},
    'document_aspects': {
        'import_aspect', 'import_aspects_batch', 'import_promotion_batch',
    },
    'document_highlights': {'import_highlight', 'import_highlights_batch'},
    'memory': {'build_import_row', 'import_entries_batch', 'import_entry'},
    'plans': {'build_import_row', 'import_plan', 'import_plans_batch'},
    'taxonomy': {
        'import_assignment', 'import_rows_batch', 'import_taxonomy_meta',
        'import_topic', 'import_topic_link',
    },
    'telemetry': {
        'import_frecency_row', 'import_hook_failure', 'import_nx_answer_run',
        'import_relevance_row', 'import_rows_batch', 'import_search_row',
        'import_tier_write', 'probe_ids',
    },
}


@pytest.mark.parametrize("label,http_path", _STORE_PAIRS,
                         ids=[p[0] for p in _STORE_PAIRS])
def test_every_http_method_is_accounted_for(label, http_path):
    """THE ENUMERATION GATE (nexus-tdkg1.1, after critique). Every public method
    on every Http* store must be in the primary contract, the supplemental
    contract, or the dying-ETL allowlist — nothing may be merely unlisted.

    WHY THIS EXISTS AND THE NON-VACUITY FLOOR DOES NOT SUFFICE. That floor is
    DIRECTIONAL: it catches truncation of the supplemental dict and is blind to
    incompleteness. The first cut of this work satisfied a `>= 4` floor, every
    other test in this file, and a full code review — while covering only four
    of the eight permanent service-mode-only methods. It took enumerating all
    nine stores by script to find the rest. This test IS that script, run every
    time, so the next method that appears must be classified rather than
    silently join the blind spot.

    A new Http method therefore fails CI until someone decides which it is:
    contract-covered, service-mode-only, or dying with the wave.
    """
    http_methods = _public_methods(_load(http_path))
    covered = (
        set(T2_STORE_CONTRACT[label])
        | set(T2_SUPPLEMENTAL_CONTRACT.get(label, {}))
        | _ETL_DYING_WITH_THE_WAVE.get(label, set())
    )
    unaccounted = http_methods - covered

    assert not unaccounted, (
        f"{label}: public Http methods classified nowhere: {sorted(unaccounted)}.\n"
        f"  Pick one and record it:\n"
        f"   - has a SQLite twin -> it belongs in T2_STORE_CONTRACT (regenerate);\n"
        f"   - service-mode-only and SURVIVES the 7.0.0 wave -> "
        f"T2_SUPPLEMENTAL_CONTRACT, with its frozen param list;\n"
        f"   - dies with the migration ETL -> _ETL_DYING_WITH_THE_WAVE.\n"
        f"  Leaving it unlisted means it has no detection path once "
        f"nexus-i711w deletes the SQLite oracle."
    )


@pytest.mark.parametrize("label", sorted(_ETL_DYING_WITH_THE_WAVE),
                         ids=sorted(_ETL_DYING_WITH_THE_WAVE))
def test_dying_allowlist_has_no_stale_entries(label):
    """The allowlist must not outlive the methods it excuses.

    A stale name here is a standing exemption that covers nothing — and worse,
    it would silently absorb a FUTURE method that happens to reuse the name.
    """
    http_methods = _public_methods(_load(dict(_STORE_PAIRS)[label]))
    stale = _ETL_DYING_WITH_THE_WAVE[label] - http_methods
    assert not stale, (
        f"{label}: _ETL_DYING_WITH_THE_WAVE names methods no longer on the Http "
        f"store {sorted(stale)}. If the wave deleted them, drop the entries; "
        f"the allowlist should shrink to nothing as the ETL family goes."
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


def _norm_return(ann: object) -> str:
    """Normalize a return annotation for comparison.

    DELIBERATELY SHALLOW: strip quotes (a forward reference and a resolved one
    are the same contract) and whitespace. Nothing else. A normalizer generous
    enough to equate ``list[...]`` with ``dict[...]`` would be GREEN on the very
    bug this check exists for (nexus-ekn9n).
    """
    return str(ann).strip("'\"").replace(" ", "")


@pytest.mark.parametrize("label,http_path", _STORE_PAIRS,
                         ids=[p[0] for p in _STORE_PAIRS])
def test_http_store_matches_contract_return_shapes(label, http_path):
    """The HTTP twin's RETURN SHAPE matches the frozen contract (nexus-b8a5a).

    The parameter tripwire above pins names only, so a twin could diverge on
    what it RETURNS and stay green. One did, for the whole life of the RDR-152
    port: HttpTaxonomyStore.get_topic_link_pairs returned
    ``list[tuple[int,int,int]]`` where the oracle returns
    ``dict[tuple[int,int],int]``. Consumers annotate the mapping, so
    ``apply_topic_boost``'s ``for (a, b) in links`` raised ValueError inside
    ``search_engine``'s best-effort except and the entire topic boost was
    silently dropped in service mode (P1). Both annotations existed and
    differed — a static check was always sufficient.
    """
    expected = T2_STORE_RETURNS.get(label, {})
    http_cls = _load(http_path)
    drift = []
    for method, want in sorted(expected.items()):
        if method in _EXCLUSIONS.get(label, {}):
            continue
        fn = getattr(http_cls, method, None)
        if fn is None:
            continue  # coverage is the sibling test's job, not this one's
        got = _norm_return(inspect.signature(fn).return_annotation)
        if got != want:
            drift.append(f"  {label}.{method}\n      contract: {want}\n      http    : {got}")
    assert not drift, (
        f"{label}: HTTP store return shape diverges from the frozen contract "
        f"(nexus-b8a5a — this is the nexus-ekn9n defect class):\n" + "\n".join(drift)
    )


# ---------------------------------------------------------------------------
# The P4-DELETE-MARKER block (``_SQLITE_ORACLES`` +
# ``test_contract_returns_match_live_sqlite_oracle`` +
# ``test_contract_matches_live_sqlite_oracle``) was deleted per its own
# instructions in the nexus-i711w terminal deletion: with the SQLite store
# classes gone there is no live oracle left to capture from, and the frozen
# T2_STORE_RETURNS / T2_STORE_CONTRACT tables above are the contract of
# record. NOTE (by design): this also retired the last live-oracle check for
# the T1 scratch contract — the frozen table is its contract of record too.
# ---------------------------------------------------------------------------
