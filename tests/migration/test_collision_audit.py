# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-p9vqa: retroactive target-name collision audit.

The probe core (:func:`audit_collision_groups`) is tested against fake
Chroma read clients + a fake vector service; every verdict class gets an
exact-count fixture. The orchestration wrapper
(:func:`audit_target_collisions`) is tested with the same
monkeypatch-the-engine pattern ``tests/migration/test_driver.py`` uses for
the 5b9v0 guard, pinning that it derives collisions through the guard's
OWN extracted functions (``build_cross_model_target_names`` /
``group_colliding_targets``), not a reimplementation.
"""
from __future__ import annotations

import pytest

from nexus.db.http_vector_client import VectorServiceError

from nexus.migration import collision_audit
from nexus.migration.collision_audit import (
    INDETERMINATE,
    MERGED,
    NEVER_MATERIALIZED,
    PARTIAL,
    SINGLE_SOURCE,
    WORLD_NO_VOYAGE_KEY,
    WORLD_VOYAGE_KEY,
    audit_collision_groups,
    audit_target_collisions,
)
from nexus.migration.detection import CollectionClassification, DetectionReport
from nexus.migration.driver import (
    build_cross_model_target_names,
    group_colliding_targets,
)

_ONNX = "bge-base-en-v15-768"
_TARGET = "code__1-3__bge-base-en-v15-768__v1"


# ── fakes ────────────────────────────────────────────────────────────────────


class _FakeChromaCollection:
    def __init__(self, ids: list[str]) -> None:
        self._ids = list(ids)

    def get(self, *, include, limit, offset):  # noqa: ANN001 — chroma shape
        assert include == []  # the audit must fetch ids only
        return {"ids": self._ids[offset : offset + limit]}


class _FakeReadClient:
    def __init__(self, collections: dict[str, list[str]]) -> None:
        self._collections = collections
        self.closed = False

    def get_collection(self, name: str) -> _FakeChromaCollection:
        return _FakeChromaCollection(self._collections[name])

    def close(self) -> None:
        self.closed = True


class _FakeVectorClient:
    """Service double: collections is target-name -> set of present ids."""

    def __init__(
        self,
        collections: dict[str, set[str]],
        *,
        counts: dict[str, int] | None = None,
        degrade_existing_ids: bool = False,
    ) -> None:
        self._collections = collections
        self._counts = counts or {}
        self._degrade = degrade_existing_ids

    def collection_exists(self, name: str) -> bool:
        return name in self._collections

    def count(self, collection: str) -> int:
        if collection in self._counts:
            return self._counts[collection]
        return len(self._collections[collection])

    def existing_ids(self, collection: str, ids: list[str]) -> set[str]:
        if self._degrade:
            # nexus-ou4tb: the REAL HttpVectorClient.existing_ids raises on a
            # transport failure — it no longer degrades to an empty set. This
            # double returned set() and so asserted a contract the production
            # code had stopped honouring, which is precisely how the retry-heal
            # test kept passing while _probe_source's healing was dead code.
            raise VectorServiceError("simulated per-page transport failure")
        return self._collections.get(collection, set()) & set(ids)


def _cls(
    collection: str,
    leg: str = "local",
    *,
    model: str | None = _ONNX,
    measured_dim: int | None = None,
    support: str = "supported",
    count: int = 3,
) -> CollectionClassification:
    return CollectionClassification(
        collection=collection,
        leg=leg,  # type: ignore[arg-type]
        model=model,
        dim=768,
        support=support,  # type: ignore[arg-type]
        source_count=count,
        has_data=count > 0,
        measured_dim=measured_dim,
    )


def _misnamed_voyage_cls(collection: str, leg: str = "local") -> CollectionClassification:
    """The Steve's-box shape: name claims voyage, vectors measure 768/ONNX."""
    return CollectionClassification(
        collection=collection,
        leg=leg,  # type: ignore[arg-type]
        model="voyage-code-3",
        dim=1024,
        support="unsupported",
        source_count=3,
        has_data=True,
        reason="misnamed pre-RDR-109 collection",
        measured_dim=768,
    )


_HONEST = _TARGET  # the honest sibling's own name IS the collision target
_STALE = "code__1-3__voyage-code-3__v1"

_HONEST_IDS = ["h1", "h2", "h3"]
_STALE_IDS = ["s1", "s2", "h3"]  # h3 overlaps: the overlapping-chash variant


def _collision_group() -> dict[str, list[CollectionClassification]]:
    return {_TARGET: [_misnamed_voyage_cls(_STALE), _cls(_HONEST)]}


def _read_clients() -> dict[str, object]:
    return {
        "local": _FakeReadClient({_HONEST: _HONEST_IDS, _STALE: _STALE_IDS})
    }


# ── verdicts, one exact fixture each ────────────────────────────────────────


def test_merged_verdict_when_both_sources_fully_present():
    """The silent-merge signature: every id of BOTH sources is in the target
    (union = 5: h1 h2 h3 s1 s2 — the overlapping chash collapses)."""
    vector = _FakeVectorClient({_TARGET: {"h1", "h2", "h3", "s1", "s2"}})
    report = audit_collision_groups(
        _collision_group(), vector_client=vector, clients_by_leg=_read_clients()
    )
    assert not report.clean
    assert len(report.findings) == 1
    f = report.findings[0]
    assert f.verdict == MERGED
    assert f.target == _TARGET
    assert f.target_count == 5
    assert f.union_source_ids == 5
    assert {p.classification.collection: p.present_in_target for p in f.sources} == {
        _STALE: 3,
        _HONEST: 3,
    }
    assert all(p.fully_present for p in f.sources)
    assert "SILENT MERGE CONFIRMED" in f.detail
    assert report.merged_targets == (f,)


def test_single_source_verdict_when_only_honest_sibling_landed():
    """The loud count-mismatch variant's aftermath: only the honest sibling's
    rows are in the target; the stale source's non-overlapping ids (s1, s2)
    are absent — flagged as an unmigrated remainder, not clean."""
    vector = _FakeVectorClient({_TARGET: {"h1", "h2", "h3"}})
    report = audit_collision_groups(
        _collision_group(), vector_client=vector, clients_by_leg=_read_clients()
    )
    f = report.findings[0]
    assert f.verdict == SINGLE_SOURCE
    by_name = {p.classification.collection: p for p in f.sources}
    assert by_name[_HONEST].fully_present
    # the stale source's overlapping id (h3) IS present — 1 of 3
    assert by_name[_STALE].present_in_target == 1
    assert by_name[_STALE].missing_from_target == 2
    assert "UNMIGRATED" in f.detail


def test_never_materialized_verdict_when_target_absent():
    vector = _FakeVectorClient({})
    report = audit_collision_groups(
        _collision_group(), vector_client=vector, clients_by_leg=_read_clients()
    )
    f = report.findings[0]
    assert f.verdict == NEVER_MATERIALIZED
    assert not f.target_exists
    assert f.target_count == 0
    # probing is skipped entirely — no source ids were enumerated
    assert all(p.probed_ids == 0 for p in f.sources)


def test_partial_verdict_when_no_source_fully_present():
    """An interrupted run: fragments of both sources, neither complete."""
    vector = _FakeVectorClient({_TARGET: {"h1", "s1"}})
    report = audit_collision_groups(
        _collision_group(), vector_client=vector, clients_by_leg=_read_clients()
    )
    f = report.findings[0]
    assert f.verdict == PARTIAL
    by_name = {p.classification.collection: p for p in f.sources}
    assert by_name[_HONEST].present_in_target == 1
    assert by_name[_STALE].present_in_target == 1


def test_indeterminate_when_probe_resolves_nothing_against_nonempty_target():
    """never-blind-fill mirror (nexus-r0esi): existing_ids degrades to the
    empty set on transport failure — a non-empty target with ZERO resolved
    source ids must read as an anomaly, never as evidence of anything."""
    vector = _FakeVectorClient(
        {_TARGET: set()}, counts={_TARGET: 42}, degrade_existing_ids=True
    )
    report = audit_collision_groups(
        _collision_group(), vector_client=vector, clients_by_leg=_read_clients()
    )
    f = report.findings[0]
    assert f.verdict == INDETERMINATE
    assert f.target_count == 42
    assert report.indeterminate_targets == (f,)


def test_empty_nonexistent_target_is_never_materialized_not_indeterminate():
    """A target that exists but is EMPTY (count 0, nothing resolved) is not
    an anomaly — with zero rows there is nothing merged; the group reports
    partial-shape facts (all sources fully absent)."""
    vector = _FakeVectorClient({_TARGET: set()})
    report = audit_collision_groups(
        _collision_group(), vector_client=vector, clients_by_leg=_read_clients()
    )
    f = report.findings[0]
    assert f.verdict == PARTIAL
    assert f.target_count == 0


def test_missing_leg_client_fails_loud():
    vector = _FakeVectorClient({_TARGET: {"h1"}})
    with pytest.raises(RuntimeError, match="no open read client for leg"):
        audit_collision_groups(
            _collision_group(), vector_client=vector, clients_by_leg={}
        )


def test_count_error_propagates_loud():
    """count() is the reachability probe — its failure must not be swallowed
    into a false verdict (existing_ids swallowing is exactly why count runs
    first)."""

    class _ExplodingVector(_FakeVectorClient):
        def count(self, collection: str) -> int:
            raise RuntimeError("service unreachable")

    vector = _ExplodingVector({_TARGET: {"h1"}})
    with pytest.raises(RuntimeError, match="service unreachable"):
        audit_collision_groups(
            _collision_group(), vector_client=vector, clients_by_leg=_read_clients()
        )


def test_three_way_collision_reports_every_source():
    third = "code__1-3__minilm-l6-v2-384__v1"
    group = {
        _TARGET: [
            _misnamed_voyage_cls(_STALE),
            _cls(_HONEST),
            _cls(third, measured_dim=768, support="unsupported", model=None),
        ]
    }
    clients = {
        "local": _FakeReadClient(
            {_HONEST: _HONEST_IDS, _STALE: _STALE_IDS, third: ["t1"]}
        )
    }
    vector = _FakeVectorClient({_TARGET: {"h1", "h2", "h3", "s1", "s2", "t1"}})
    report = audit_collision_groups(
        group, vector_client=vector, clients_by_leg=clients
    )
    f = report.findings[0]
    assert f.verdict == MERGED
    assert len(f.sources) == 3
    assert f.union_source_ids == 6


def test_cross_leg_collision_probes_each_source_via_its_own_leg():
    group = {
        _TARGET: [
            _cls(_HONEST, leg="local"),
            _cls(_HONEST, leg="cloud"),
        ]
    }
    clients = {
        "local": _FakeReadClient({_HONEST: ["l1", "l2", "l3"]}),
        "cloud": _FakeReadClient({_HONEST: ["c1", "c2", "c3"]}),
    }
    vector = _FakeVectorClient({_TARGET: {"l1", "l2", "l3", "c1", "c2", "c3"}})
    report = audit_collision_groups(
        group, vector_client=vector, clients_by_leg=clients
    )
    f = report.findings[0]
    assert f.verdict == MERGED
    assert f.union_source_ids == 6


# ── orchestration wrapper ────────────────────────────────────────────────────


def test_audit_target_collisions_reuses_guard_grouping(monkeypatch):
    """The wrapper must derive collisions through the guard's own extracted
    functions over a real classification pass — pinned by feeding a detection
    whose collision only exists via the remap map (the misnamed source's
    remap target == the honest sibling's name)."""
    detection = DetectionReport(
        classifications=(
            _misnamed_voyage_cls(_STALE),
            _cls(_HONEST),
        ),
        voyage_key_present=False,
    )

    closed: list[str] = []
    classify_worlds: list[bool] = []
    local_client = _FakeReadClient({_HONEST: _HONEST_IDS, _STALE: _STALE_IDS})

    def _classify(**kw):
        classify_worlds.append(kw["voyage_key_present"])
        return detection

    monkeypatch.setattr(
        collision_audit, "_open_audit_legs", lambda local_path, legs: (local_client, None)
    )
    monkeypatch.setattr(collision_audit, "classify_collections", _classify)
    monkeypatch.setattr(
        collision_audit,
        "close_read_client",
        lambda c: closed.append("closed") if c is not None else None,
    )

    # sanity: the shared functions really produce this collision from the
    # detection above (guards the fixture against silent drift).
    targets = build_cross_model_target_names(detection, voyage_key_present=False)
    assert targets == {_STALE: _TARGET}
    assert set(group_colliding_targets(detection.classifications, targets)) == {_TARGET}

    vector = _FakeVectorClient({_TARGET: {"h1", "h2", "h3", "s1", "s2"}})
    seen: list[str] = []
    report = audit_target_collisions(
        vector_client=vector,
        on_progress=seen.append,
    )
    # default = BOTH worlds classified (nexus-772h2), no-key world first
    assert classify_worlds == [False, True]
    assert seen == [_TARGET]  # the union deduped to one probe of one target
    assert len(report.findings) == 1
    f = report.findings[0]
    assert f.verdict == MERGED
    # this fixture's frozen classification collides under BOTH assumptions
    assert f.worlds == (WORLD_NO_VOYAGE_KEY, WORLD_VOYAGE_KEY)
    # sources deduped by (collection, leg) across the two worlds
    assert len(f.sources) == 2
    assert closed == ["closed"]  # the open local leg was closed exactly once


def test_false_clean_regression_merge_only_visible_in_no_key_world(monkeypatch):
    """nexus-772h2 (substantive-critic Critical): a store that merged in
    LOCAL mode (no Voyage key at migration time), audited with a key
    configured. This test hand-builds the classifications via a monkeypatched
    ``classify_collections`` rather than exercising the real classifier, so
    its "key-present world" fixture below is now a HISTORICAL (pre-nexus-x7t5y)
    shape: the real classifier, post-x7t5y, would probe this exact
    voyage-named+key-present+768-measured collection and correctly reclassify
    it "unsupported"/remappable in BOTH worlds, closing this specific false
    clean at the source. The dual-world audit design this test defends
    (never trust a single classify_collections() call pinned to today's env)
    still matters generally — e.g. a caller-supplied `wired=` override, or a
    classification result cached from before a service restart, could still
    reproduce a stale-vs-live divergence — this fixture is kept as a
    synthetic stand-in for that broader class, not as a claim about what the
    real classifier does today.
    """
    # world False: the historical truth — misnamed collection is unsupported,
    # measured 768, remaps onto the honest sibling's name.
    detection_no_key = DetectionReport(
        classifications=(_misnamed_voyage_cls(_STALE), _cls(_HONEST)),
        voyage_key_present=False,
    )
    # world True: a SYNTHETIC stale-classification world, pre-nexus-x7t5y
    # shape (the real classifier no longer produces this — see docstring
    # above) — the voyage-named collection reads supported, never probed,
    # never remapped: NO collision exists in this (now-historical) world.
    supported_stale = CollectionClassification(
        collection=_STALE,
        leg="local",
        model="voyage-code-3",
        dim=1024,
        support="supported-voyage-1024",
        source_count=3,
        has_data=True,
    )
    detection_key = DetectionReport(
        classifications=(supported_stale, _cls(_HONEST)),
        voyage_key_present=True,
    )

    def _classify(**kw):
        return detection_key if kw["voyage_key_present"] else detection_no_key

    local_client = _FakeReadClient({_HONEST: _HONEST_IDS, _STALE: _STALE_IDS})
    monkeypatch.setattr(
        collision_audit, "_open_audit_legs", lambda local_path, legs: (local_client, None)
    )
    monkeypatch.setattr(collision_audit, "classify_collections", _classify)
    monkeypatch.setattr(collision_audit, "close_read_client", lambda c: None)

    # sanity: today's world alone really is collision-free (the false clean)
    key_targets = build_cross_model_target_names(
        detection_key, voyage_key_present=True
    )
    assert group_colliding_targets(detection_key.classifications, key_targets) == {}

    vector = _FakeVectorClient({_TARGET: {"h1", "h2", "h3", "s1", "s2"}})
    report = audit_target_collisions(vector_client=vector)
    assert len(report.findings) == 1
    f = report.findings[0]
    assert f.verdict == MERGED
    assert f.worlds == (WORLD_NO_VOYAGE_KEY,)  # only the historical world saw it


def test_explicit_world_override_classifies_once(monkeypatch):
    """--assume-no-voyage-key: exactly one classification pass, that world."""
    detection = DetectionReport(
        classifications=(_cls(_HONEST),), voyage_key_present=False
    )
    classify_worlds: list[bool] = []

    def _classify(**kw):
        classify_worlds.append(kw["voyage_key_present"])
        return detection

    monkeypatch.setattr(
        collision_audit,
        "_open_audit_legs",
        lambda local_path, legs: (_FakeReadClient({_HONEST: _HONEST_IDS}), None),
    )
    monkeypatch.setattr(collision_audit, "classify_collections", _classify)
    monkeypatch.setattr(collision_audit, "close_read_client", lambda c: None)

    report = audit_target_collisions(
        vector_client=_FakeVectorClient({}), voyage_key_present=False
    )
    assert classify_worlds == [False]
    assert report.clean


def test_probe_retry_heals_single_pass_degradation():
    """The missing-id re-check: a transient one-page transport failure must
    not flip a genuinely-merged target to single-source/partial.

    nexus-ou4tb: the flaky double used to RETURN an empty set on the first
    pass, because that is what existing_ids did on failure. It now RAISES,
    matching the real client — so this test actually exercises _probe_source's
    explicit per-page catch rather than a degradation the production code no
    longer performs.
    """

    class _FlakyVectorClient(_FakeVectorClient):
        def __init__(self, collections):
            super().__init__(collections)
            self.calls = 0

        def existing_ids(self, collection: str, ids: list[str]) -> set[str]:
            self.calls += 1
            if self.calls == 1:  # the first source's first-pass page degrades
                raise VectorServiceError("transient page failure")
            return super().existing_ids(collection, ids)

    vector = _FlakyVectorClient({_TARGET: {"h1", "h2", "h3", "s1", "s2"}})
    report = audit_collision_groups(
        _collision_group(), vector_client=vector, clients_by_leg=_read_clients()
    )
    f = report.findings[0]
    assert f.verdict == MERGED
    assert all(p.fully_present for p in f.sources)


def test_audit_target_collisions_clean_store(monkeypatch):
    """No collision groups -> clean report, no probing, legs closed."""
    detection = DetectionReport(
        classifications=(_cls(_HONEST),), voyage_key_present=False
    )
    local_client = _FakeReadClient({_HONEST: _HONEST_IDS})
    closed: list[str] = []
    monkeypatch.setattr(
        collision_audit, "_open_audit_legs", lambda local_path, legs: (local_client, None)
    )
    monkeypatch.setattr(collision_audit, "classify_collections", lambda **kw: detection)
    monkeypatch.setattr(
        collision_audit,
        "close_read_client",
        lambda c: closed.append("closed") if c is not None else None,
    )

    class _NeverCalledVector:
        def __getattr__(self, name: str):  # noqa: ANN204
            raise AssertionError(f"vector client must not be touched on a clean store ({name})")

    report = audit_target_collisions(
        vector_client=_NeverCalledVector(), voyage_key_present=False
    )
    assert report.clean
    assert report.findings == ()
    assert closed == ["closed"]


# ── leg handling (nexus-ovbmb) ───────────────────────────────────────────────


def _patch_leg_openers(monkeypatch, *, local=None, cloud=None,
                       local_raises=None, cloud_raises=None):
    def _local(path):
        if local_raises is not None:
            raise local_raises
        return local

    def _cloud():
        if cloud_raises is not None:
            raise cloud_raises
        return cloud

    monkeypatch.setattr(
        "nexus.migration.chroma_read.open_local_read_client", _local
    )
    monkeypatch.setattr(
        "nexus.migration.chroma_read.open_cloud_read_client", _cloud
    )


def test_legs_local_skips_cloud_and_reports_partial_scope(monkeypatch):
    """--legs local: the cloud opener must never be touched, and the report
    must be LOUDLY partial-scope even when clean."""
    local_client = _FakeReadClient({_HONEST: _HONEST_IDS})

    def _cloud_never():
        raise AssertionError("cloud leg must not be opened under --legs local")

    monkeypatch.setattr(
        "nexus.migration.chroma_read.open_local_read_client",
        lambda path: local_client,
    )
    monkeypatch.setattr(
        "nexus.migration.chroma_read.open_cloud_read_client", _cloud_never
    )
    monkeypatch.setattr(
        collision_audit,
        "classify_collections",
        lambda **kw: DetectionReport(
            classifications=(_cls(_HONEST),), voyage_key_present=False
        ),
    )
    monkeypatch.setattr(collision_audit, "close_read_client", lambda c: None)

    report = audit_target_collisions(
        vector_client=_FakeVectorClient({}), legs="local",
        voyage_key_present=False,
    )
    assert report.clean
    assert report.requested_legs == "local"
    assert report.audited_legs == ("local",)
    assert report.partial_scope


def test_cloud_open_failure_wraps_into_actionable_runtime_error(monkeypatch):
    """The dogfood failure shape: ChromaCloud creds present but rejected —
    must surface as a clean RuntimeError naming the --legs remedy, never a
    raw chromadb traceback."""
    _patch_leg_openers(
        monkeypatch,
        local=_FakeReadClient({}),
        cloud_raises=ValueError("Permission denied."),
    )
    with pytest.raises(RuntimeError, match="--legs local"):
        collision_audit._open_audit_legs(None, "both")


def test_local_open_failure_wraps_into_actionable_runtime_error(monkeypatch):
    _patch_leg_openers(
        monkeypatch,
        local_raises=ValueError("corrupt sqlite header"),
        cloud=_FakeReadClient({}),
    )
    with pytest.raises(RuntimeError, match="local Chroma read leg failed"):
        collision_audit._open_audit_legs(None, "both")


def test_zero_open_legs_fails_loud_never_clean(monkeypatch):
    """A deleted source must never read as 'clean' — zero legs is a hard
    error (absent-leg sentinels: local FileNotFoundError, cloud RuntimeError)."""
    _patch_leg_openers(
        monkeypatch,
        local_raises=FileNotFoundError("no local store"),
        cloud_raises=RuntimeError("cloud leg unconfigured"),
    )
    with pytest.raises(RuntimeError, match="no Chroma source leg found"):
        collision_audit._open_audit_legs(None, "both")


def test_unknown_legs_selector_rejected(monkeypatch):
    _patch_leg_openers(monkeypatch, local=_FakeReadClient({}))
    with pytest.raises(RuntimeError, match="unknown legs selector"):
        collision_audit._open_audit_legs(None, "everything")


def test_cloud_open_failure_closes_the_already_opened_local_leg(monkeypatch):
    """code-review High (nexus-ovbmb): the local leg opens FIRST; a cloud
    open failure must close it before raising, or the handle leaks past
    chroma_read's single-opener discipline on exactly the failure path this
    function exists to handle."""
    local_client = _FakeReadClient({})
    _patch_leg_openers(
        monkeypatch, local=local_client, cloud_raises=ValueError("Permission denied.")
    )
    with pytest.raises(RuntimeError, match="--legs local"):
        collision_audit._open_audit_legs(None, "both")
    assert local_client.closed, (
        "the opened local read client must be closed when the cloud open fails"
    )


def test_legs_cloud_skips_local_and_reports_partial_scope(monkeypatch):
    """Symmetry pin for --legs cloud (critic Significant 2): the local opener
    must never be touched."""
    cloud_client = _FakeReadClient({_HONEST: _HONEST_IDS})

    def _local_never(path):
        raise AssertionError("local leg must not be opened under --legs cloud")

    monkeypatch.setattr(
        "nexus.migration.chroma_read.open_local_read_client", _local_never
    )
    monkeypatch.setattr(
        "nexus.migration.chroma_read.open_cloud_read_client", lambda: cloud_client
    )
    monkeypatch.setattr(
        collision_audit,
        "classify_collections",
        lambda **kw: DetectionReport(
            classifications=(_cls(_HONEST, leg="cloud"),), voyage_key_present=False
        ),
    )
    monkeypatch.setattr(collision_audit, "close_read_client", lambda c: None)

    report = audit_target_collisions(
        vector_client=_FakeVectorClient({}), legs="cloud",
        voyage_key_present=False,
    )
    assert report.clean
    assert report.audited_legs == ("cloud",)
    assert report.partial_scope


def test_both_legs_open_is_full_scope(monkeypatch):
    """partial_scope must be False when both legs actually opened — the
    only case where a bare 'clean' speaks for the whole store."""
    monkeypatch.setattr(
        collision_audit,
        "_open_audit_legs",
        lambda local_path, legs: (
            _FakeReadClient({_HONEST: _HONEST_IDS}),
            _FakeReadClient({}),
        ),
    )
    monkeypatch.setattr(
        collision_audit,
        "classify_collections",
        lambda **kw: DetectionReport(
            classifications=(_cls(_HONEST),), voyage_key_present=False
        ),
    )
    monkeypatch.setattr(collision_audit, "close_read_client", lambda c: None)
    report = audit_target_collisions(
        vector_client=_FakeVectorClient({}), voyage_key_present=False
    )
    assert report.clean
    assert report.audited_legs == ("local", "cloud")
    assert not report.partial_scope


def test_requested_both_but_one_leg_absent_is_partial_scope(monkeypatch):
    """The realistic production shape (critic observation): --legs both, the
    cloud leg is naturally absent (unconfigured) — partial_scope must fire
    WITHOUT the user having narrowed anything."""
    monkeypatch.setattr(
        collision_audit,
        "_open_audit_legs",
        lambda local_path, legs: (_FakeReadClient({_HONEST: _HONEST_IDS}), None),
    )
    monkeypatch.setattr(
        collision_audit,
        "classify_collections",
        lambda **kw: DetectionReport(
            classifications=(_cls(_HONEST),), voyage_key_present=False
        ),
    )
    monkeypatch.setattr(collision_audit, "close_read_client", lambda c: None)
    report = audit_target_collisions(
        vector_client=_FakeVectorClient({}), voyage_key_present=False
    )
    assert report.clean
    assert report.requested_legs == "both"
    assert report.audited_legs == ("local",)
    assert report.partial_scope
