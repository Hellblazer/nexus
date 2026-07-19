# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-159 P4 (nexus-ue6g7.24) / RDR-180 (nexus-jxizy.10.7) — the guided-
upgrade engine entry point.

``run_guided_upgrade`` is the ONE function both the nexus CLI and the deferred
conexus veneer call. These tests pin its LIFECYCLE contract with the
land-then-transform sequencer injected (monkeypatched on the ``driver``
module), so the sequencing + open/close discipline is verified without a live
service / Chroma:

* fresh-user (no data) → clean no-op, no rollback offered;
* a target-name collision → BLOCKED before the sequencer is ever invoked;
* the detection read legs are CLOSED before landing is reopened (the local
  WAL single-opener invariant);
* the reopened landing legs are closed afterward, even on a raising
  land-then-transform call or a raising reopen itself;
* a two-leg migration reopens both legs for landing;
* a clean land-then-transform run synthesizes an ``unlocked`` validation;
* ANY block (RDR-180: census / land / dirty-T2 / a failed collection /
  finalize / verify / clear-staging all collapse to one outcome shape) leaves
  ``validation=None`` and ``rollback_available=False`` — there is no more
  separate "T3 copied but failed validation, rollback offered" middle state
  (design Q4: rollback is SIMPLIFIED to an idempotent re-run against retained
  staging, never an explicit rollback command).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus.migration import driver
from nexus.migration.detection import CollectionClassification, DetectionReport
from nexus.migration.orchestrator import EtlSources
from nexus.migration.sequencer import LandThenTransformOutcome

_ONNX = "bge-base-en-v15-768"
_VOYAGE = "voyage-context-3"


@pytest.fixture(autouse=True)
def _isolate_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate any state-sentinel touch off the real ~/.config/nexus."""
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    return tmp_path


def _cls(
    collection: str, leg: str, *, model: str = _ONNX, dim: int = 384, has_data: bool = True
) -> CollectionClassification:
    return CollectionClassification(
        collection=collection,
        leg=leg,  # type: ignore[arg-type]
        model=model,
        dim=dim,
        support="supported-onnx" if model == _ONNX else "supported-voyage-1024",
        source_count=10 if has_data else 0,
        has_data=has_data,
        reason="",
    )


def _detection(*classifications: CollectionClassification) -> DetectionReport:
    return DetectionReport(classifications=tuple(classifications))


def _outcome(
    *, ok: bool, phase: str, collections: tuple[str, ...] = ("code__o__m__v1",)
) -> LandThenTransformOutcome:
    return LandThenTransformOutcome(
        ok=ok,
        phase=phase,
        collections_total=len(collections) or 1,
        collections_done=len(collections) if ok else 0,
        t2_total_failed=0,
        collections_attempted=collections,
        collections_ok=collections if ok else (),
        blocked_reason=None if ok else "blocked",
        t2_report={"summary": {"total_failed": 0}},
        finalize_report=(
            {"residual_mismatched": 0, "dangling_manifest": 0} if ok else None
        ),
    )


class _FakeClient:
    """A read/vector client stub that records close() calls."""

    def __init__(self, name: str, *, closables: list[str]) -> None:
        self.name = name
        self._closables = closables

    def get_collection(self, name: str) -> object:  # pragma: no cover - routing only
        return object()

    def close(self) -> None:
        self._closables.append(self.name)


@pytest.fixture
def _sources(tmp_path) -> EtlSources:
    return EtlSources(
        sqlite_path=tmp_path / "memory.db",
        catalog_db_path=tmp_path / ".catalog.db",
    )


class _FakeStagingStore:
    """Stand-in for ``HttpStagingStore`` — its real ``__init__`` resolves a
    live service endpoint, which unit tests never have. These orchestration-
    level tests replace ``run_land_then_transform_migration`` wholesale, so
    none of these methods are ever actually invoked; this class exists only
    so ``driver.HttpStagingStore()`` (the module-global constructor call the
    driver makes unconditionally, once landing legs are open) does not try to
    resolve a real endpoint."""

    def load(self, store: str, rows: list) -> int:  # pragma: no cover - unused by these tests
        return len(rows)

    def embed_fill(self, collection: str) -> dict:  # pragma: no cover
        return {}

    def promote(self, collection: str) -> dict:  # pragma: no cover
        return {}

    def finalize(self, orphan_policy: str = "drop") -> dict:  # pragma: no cover
        return {}

    def clear(self) -> dict:  # pragma: no cover
        return {}

    def counts(self) -> dict:  # pragma: no cover
        return {}


def _patch_engine(
    monkeypatch: pytest.MonkeyPatch,
    *,
    detection: DetectionReport,
    outcome: LandThenTransformOutcome,
    closables: list[str],
    order: list[str],
    seq_capture: dict | None = None,
    voyage_key: bool = False,
):
    """Patch the driver's engine globals; record the detect→land-then-transform
    ordering. A default fake ``_default_reopen_leg`` is wired so any test with
    a data-bearing collection (which now always reopens its leg for LANDING,
    not just on a clean validate) does not need to pass ``reopen_leg``
    explicitly unless it wants to observe the reopened clients."""
    local = _FakeClient("detect-local", closables=closables)
    cloud = _FakeClient("detect-cloud", closables=closables)

    def _open_read_legs(local_path=None):
        return local, cloud

    def _classify(*, local_client, cloud_client, voyage_key_present):
        order.append("classify")
        return detection

    def _run_land_then_transform(
        det, *, sources, census_check, land, embed_fill, promote, finalize,
        verify, clear_staging, voyage_key_present, run_t2=None, model_gate=None,
        on_progress=None, cross_model_targets=None,
    ):
        order.append("land_then_transform")
        # The detection legs MUST be closed before landing is reopened.
        assert "detect-local" in closables and "detect-cloud" in closables
        if seq_capture is not None:
            seq_capture["cross_model_targets"] = cross_model_targets
        return outcome

    def _default_reopen(leg, local_path):
        return _FakeClient(f"reopen-{leg}", closables=closables)

    monkeypatch.setattr(driver, "open_read_legs", _open_read_legs)
    monkeypatch.setattr(driver, "classify_collections", _classify)
    monkeypatch.setattr(driver, "run_land_then_transform_migration", _run_land_then_transform)
    monkeypatch.setattr(driver, "_default_reopen_leg", _default_reopen)
    monkeypatch.setattr(driver, "voyage_key_available", lambda: voyage_key)
    monkeypatch.setattr(driver, "HttpStagingStore", _FakeStagingStore)


def test_fresh_user_noop_clean_success(monkeypatch, _sources):
    order: list[str] = []
    closables: list[str] = []
    _patch_engine(
        monkeypatch,
        detection=_detection(),  # no data-bearing collections
        outcome=_outcome(ok=True, phase="not-migrating", collections=()),
        closables=closables,
        order=order,
    )
    result = driver.run_guided_upgrade(
        sources=_sources,
        vector_client=object(),
        catalog_client=object(),
        t2_db_path=_sources.sqlite_path,
    )
    assert result.ok is True
    assert result.validation is None
    assert result.rollback_available is False
    # Detection clients closed; nothing was reopened (no data-bearing leg).
    assert order == ["classify", "land_then_transform"]
    assert set(closables) == {"detect-local", "detect-cloud"}


def test_run_t2_passthrough_only_when_provided(monkeypatch, _sources):
    """RDR-178 Gap 7 (nexus-1sx01): ``run_t2`` is forwarded to the sequencer
    ONLY when the caller explicitly supplies it — omitting it preserves the
    sequencer's own default, never an explicit ``None`` overriding it (which
    would break every caller unaware of this seam)."""
    captured: dict[str, object] = {}

    def _open_read_legs(local_path=None):
        return _FakeClient("l", closables=[]), _FakeClient("c", closables=[])

    def _classify(*, local_client, cloud_client, voyage_key_present):
        return _detection()

    def _run_land_then_transform(
        det, *, sources, census_check, land, embed_fill, promote, finalize,
        verify, clear_staging, voyage_key_present, run_t2="UNSET", model_gate=None,
        on_progress=None, cross_model_targets=None,
    ):
        captured["run_t2"] = run_t2
        return _outcome(ok=True, phase="not-migrating", collections=())

    monkeypatch.setattr(driver, "open_read_legs", _open_read_legs)
    monkeypatch.setattr(driver, "classify_collections", _classify)
    monkeypatch.setattr(
        driver, "run_land_then_transform_migration", _run_land_then_transform
    )

    driver.run_guided_upgrade(
        sources=_sources, vector_client=object(), catalog_client=object(),
        t2_db_path=_sources.sqlite_path,
    )
    assert captured["run_t2"] == "UNSET"  # kwarg omitted entirely

    def _fake_run_t2(sources):  # noqa: ANN001, ANN202
        return {}

    driver.run_guided_upgrade(
        sources=_sources, vector_client=object(), catalog_client=object(),
        t2_db_path=_sources.sqlite_path, run_t2=_fake_run_t2,
    )
    assert captured["run_t2"] is _fake_run_t2


def test_land_then_transform_block_no_validation(monkeypatch, _sources):
    order: list[str] = []
    closables: list[str] = []
    _patch_engine(
        monkeypatch,
        detection=_detection(_cls("code__o__m__v1", "local")),
        outcome=_outcome(ok=False, phase="migrated-failed"),
        closables=closables,
        order=order,
    )
    result = driver.run_guided_upgrade(
        sources=_sources,
        vector_client=object(),
        catalog_client=object(),
        t2_db_path=_sources.sqlite_path,
    )
    assert result.ok is False
    assert result.validation is None
    assert result.rollback_available is False
    assert order == ["classify", "land_then_transform"]


def test_clean_land_then_transform_synthesizes_unlocked_validation(monkeypatch, _sources):
    order: list[str] = []
    closables: list[str] = []
    reopened: list[str] = []
    _patch_engine(
        monkeypatch,
        detection=_detection(_cls("code__o__m__v1", "local")),
        outcome=_outcome(ok=True, phase="migrated"),
        closables=closables,
        order=order,
    )

    def _reopen(leg: str):
        reopened.append(leg)
        return _FakeClient(f"reopen-{leg}", closables=closables)

    monkeypatch.setattr(driver, "_default_reopen_leg", lambda leg, lp: _reopen(leg))

    result = driver.run_guided_upgrade(
        sources=_sources,
        vector_client=object(),
        catalog_client=object(),
        t2_db_path=_sources.sqlite_path,
    )
    assert result.ok is True
    assert result.validation is not None and result.validation.unlocked
    assert result.rollback_available is False
    assert order == ["classify", "land_then_transform"]
    assert reopened == ["local"]
    # Reopened leg (for landing) closed after the sequencer returns.
    assert "reopen-local" in closables


def test_block_after_data_bearing_run_offers_no_rollback(monkeypatch, _sources):
    """RDR-180 contract change: a block that occurs AFTER real work started
    (census/land/promote/finalize/verify all ran server-side against staging)
    still leaves ``validation=None`` / ``rollback_available=False`` — there is
    no more "copied but unvalidated, rollback offered" middle state. Recovery
    is re-run (idempotent landing + promote), never an explicit rollback."""
    order: list[str] = []
    closables: list[str] = []
    _patch_engine(
        monkeypatch,
        detection=_detection(_cls("code__o__m__v1", "local")),
        outcome=_outcome(ok=False, phase="migrated-failed"),
        closables=closables,
        order=order,
    )
    result = driver.run_guided_upgrade(
        sources=_sources,
        vector_client=object(),
        catalog_client=object(),
        t2_db_path=_sources.sqlite_path,
    )
    assert result.ok is False
    assert result.validation is None
    assert result.rollback_available is False


def test_two_leg_reopens_both_legs_for_landing(monkeypatch, _sources):
    """A local+cloud migration reopens BOTH legs for landing (RDR-180: the
    old per-leg validation-only reopen is now the landing reopen — the
    two-leg composition itself is unit-covered here; the real cross-leg
    read/land wiring is exercised by the hermetic e2e oracle)."""
    order: list[str] = []
    closables: list[str] = []
    detection = _detection(
        _cls("code__o__bge-base-en-v15-768__v1", "local", model=_ONNX, dim=768),
        _cls("docs__o__voyage-context-3__v1", "cloud", model=_VOYAGE, dim=1024),
    )
    _patch_engine(
        monkeypatch,
        detection=detection,
        outcome=_outcome(
            ok=True, phase="migrated",
            collections=(
                "code__o__bge-base-en-v15-768__v1", "docs__o__voyage-context-3__v1",
            ),
        ),
        closables=closables,
        order=order,
    )
    legs_clients = {}

    def _reopen(leg: str):
        client = _FakeClient(f"reopen-{leg}", closables=closables)
        legs_clients[leg] = client
        return client

    monkeypatch.setattr(driver, "_default_reopen_leg", lambda leg, lp: _reopen(leg))

    result = driver.run_guided_upgrade(
        sources=_sources,
        vector_client=object(),
        catalog_client=object(),
        t2_db_path=_sources.sqlite_path,
    )
    assert result.ok is True
    assert set(legs_clients) == {"local", "cloud"}
    # Both reopened legs closed.
    assert {"reopen-local", "reopen-cloud"} <= set(closables)


def _cross_model_cls(collection: str, leg: str) -> CollectionClassification:
    """A legacy minilm-384 (unsupported) collection — cross-model-remappable."""
    return CollectionClassification(
        collection=collection,
        leg=leg,  # type: ignore[arg-type]
        model="minilm-l6-v2-384",
        dim=384,
        support="unsupported",
        source_count=10,
        has_data=True,
        reason="wired by no service embedder",
    )


def test_cross_model_target_is_bge_in_local_mode(monkeypatch, _sources):
    """nexus-gilf2: local mode (no voyage key) remaps minilm-384 → bge-768."""
    capture: dict = {}
    _patch_engine(
        monkeypatch,
        detection=_detection(
            _cross_model_cls("knowledge__o__minilm-l6-v2-384__v1", "local"),
            _cross_model_cls("code__o__minilm-l6-v2-384__v1", "local"),
        ),
        outcome=_outcome(ok=False, phase="migrated-failed"),
        closables=[],
        order=[],
        seq_capture=capture,
        voyage_key=False,
    )
    driver.run_guided_upgrade(
        sources=_sources,
        vector_client=object(),
        catalog_client=object(),
        t2_db_path=_sources.sqlite_path,
    )
    assert capture["cross_model_targets"] == {
        "knowledge__o__minilm-l6-v2-384__v1": "knowledge__o__bge-base-en-v15-768__v1",
        "code__o__minilm-l6-v2-384__v1": "code__o__bge-base-en-v15-768__v1",
    }


def test_cross_model_target_is_voyage_in_cloud_mode(monkeypatch, _sources):
    """nexus-gilf2: cloud mode (voyage key) remaps minilm-384 to the
    content-type-appropriate voyage model — the mixed-migrant fix."""
    capture: dict = {}
    _patch_engine(
        monkeypatch,
        detection=_detection(
            _cross_model_cls("knowledge__o__minilm-l6-v2-384__v1", "local"),
            _cross_model_cls("code__o__minilm-l6-v2-384__v1", "local"),
        ),
        outcome=_outcome(ok=False, phase="migrated-failed"),
        closables=[],
        order=[],
        seq_capture=capture,
        voyage_key=True,
    )
    driver.run_guided_upgrade(
        sources=_sources,
        vector_client=object(),
        catalog_client=object(),
        t2_db_path=_sources.sqlite_path,
    )
    assert capture["cross_model_targets"] == {
        "knowledge__o__minilm-l6-v2-384__v1": "knowledge__o__voyage-context-3__v1",
        "code__o__minilm-l6-v2-384__v1": "code__o__voyage-code-3__v1",
    }


def test_reopened_legs_closed_when_land_then_transform_raises(monkeypatch, _sources):
    order: list[str] = []
    closables: list[str] = []
    detection = _detection(_cls("code__o__m__v1", "local"))
    local = _FakeClient("detect-local", closables=closables)
    cloud = _FakeClient("detect-cloud", closables=closables)

    monkeypatch.setattr(driver, "open_read_legs", lambda local_path=None: (local, cloud))
    monkeypatch.setattr(
        driver, "classify_collections",
        lambda **kw: (order.append("classify"), detection)[1],
    )
    monkeypatch.setattr(driver, "voyage_key_available", lambda: False)

    def _boom(*a, **k):
        raise RuntimeError("land-then-transform exploded")

    monkeypatch.setattr(driver, "run_land_then_transform_migration", _boom)
    monkeypatch.setattr(
        driver, "_default_reopen_leg",
        lambda leg, lp: _FakeClient(f"reopen-{leg}", closables=closables),
    )

    with pytest.raises(RuntimeError, match="land-then-transform exploded"):
        driver.run_guided_upgrade(
            sources=_sources,
            vector_client=object(),
            catalog_client=object(),
            t2_db_path=_sources.sqlite_path,
        )
    # The landing-reopened leg is closed even though the sequencer raised.
    assert "reopen-local" in closables


def test_reopen_for_landing_raises_wrapped_as_runtimeerror(monkeypatch, _sources):
    """RDR-180: reopening now happens BEFORE the sequencer ever sets the
    sentinel to ``migrating`` (``run_land_then_transform_migration``'s own
    first data-bearing step is ``begin_migration``) — a reopen failure here
    is therefore a PRE-WRITE block, same footing as
    ``TargetNameCollisionBlocked``, not a stranded ``migrated`` sentinel.
    There is no more ``mark_failed`` call to observe at this point (driver.py
    no longer imports it — the sequencer owns every sentinel transition from
    here on). The CLI-friendliness wrap (any exception → RuntimeError, so
    ``migrate_cmd.py``'s ``except RuntimeError`` catches it) is preserved."""
    order: list[str] = []
    closables: list[str] = []
    detection = _detection(_cls("code__o__m__v1", "local"))
    local = _FakeClient("detect-local", closables=closables)
    cloud = _FakeClient("detect-cloud", closables=closables)

    monkeypatch.setattr(driver, "open_read_legs", lambda local_path=None: (local, cloud))
    monkeypatch.setattr(
        driver, "classify_collections",
        lambda **kw: (order.append("classify"), detection)[1],
    )
    monkeypatch.setattr(driver, "voyage_key_available", lambda: False)

    original = FileNotFoundError(
        "local Chroma store not found at /tmp/gone — nothing to migrate"
    )

    def _reopen_vanished(leg, lp):
        raise original

    monkeypatch.setattr(driver, "_default_reopen_leg", _reopen_vanished)

    with pytest.raises(RuntimeError) as exc_info:
        driver.run_guided_upgrade(
            sources=_sources,
            vector_client=object(),
            catalog_client=object(),
            t2_db_path=_sources.sqlite_path,
        )
    assert type(exc_info.value) is RuntimeError
    assert not isinstance(exc_info.value, FileNotFoundError)
    assert exc_info.value.__cause__ is original
    assert "local Chroma store not found" in str(exc_info.value)
    assert "land_then_transform" not in order


def test_composite_read_client_unknown_collection_raises():
    composite = driver._CompositeReadClient({})
    with pytest.raises(RuntimeError, match="no source read leg"):
        composite.get_collection("missing__o__m__v1")


def _misnamed_voyage_cls(collection: str, leg: str) -> CollectionClassification:
    """nexus-5b9v0: a pre-RDR-109 collection misnamed with a voyage-model token
    (nexus-59vl / GH#667) — indexed pre-v4.32.0 in local mode, so its vectors are
    actually 768-dim ONNX despite the name carrying a voyage token. The
    measured-dim override makes this cross-model-remappable onto ``bge-768``
    (its ACTUAL content's model), same as a genuinely-unsupported collection."""
    return CollectionClassification(
        collection=collection,
        leg=leg,  # type: ignore[arg-type]
        model="voyage-code-3",
        dim=1024,
        support="unsupported",
        source_count=10,
        has_data=True,
        reason=(
            f"collection uses voyage model 'voyage-code-3' but no "
            "NX_VOYAGE_API_KEY is configured"
        ),
        measured_dim=768,
    )


def test_target_name_collision_blocked_before_land_then_transform(monkeypatch, _sources):
    """nexus-5b9v0 (Steve's real failure): a pre-RDR-109 misnamed-voyage
    collection measures as bge-768 and cross-model-remaps onto the EXACT same
    target name as its honest, non-remapped bge sibling. Both would write into
    one pgvector target under one name — the guard must block this BEFORE any
    landing, naming both colliding source collections and the shared target."""
    order: list[str] = []
    closables: list[str] = []
    detection = _detection(
        _misnamed_voyage_cls("code__1-3__voyage-code-3__v1", "local"),
        _cls("code__1-3__bge-base-en-v15-768__v1", "local", model=_ONNX, dim=768),
    )
    _patch_engine(
        monkeypatch,
        detection=detection,
        outcome=_outcome(ok=True, phase="migrated"),
        closables=closables,
        order=order,
        voyage_key=False,
    )
    with pytest.raises(driver.TargetNameCollisionBlocked) as exc_info:
        driver.run_guided_upgrade(
            sources=_sources,
            vector_client=object(),
            catalog_client=object(),
            t2_db_path=_sources.sqlite_path,
        )
    message = str(exc_info.value)
    assert "code__1-3__voyage-code-3__v1" in message
    assert "code__1-3__bge-base-en-v15-768__v1" in message
    # 360-sweep Dimension A (2026-07-10): the block must point at the
    # purpose-built forensics command, not leave the operator to find it.
    assert "nx migration-audit" in message
    # The exception's structured payload names the collision precisely — and
    # (nexus-5b9v0 Fix 3) carries the FULL classification per source, not just
    # the bare name, so an operator can tell which one is the stale mislabel.
    collisions = exc_info.value.collisions
    assert set(collisions.keys()) == {"code__1-3__bge-base-en-v15-768__v1"}
    sources = collisions["code__1-3__bge-base-en-v15-768__v1"]
    assert {c.collection for c in sources} == {
        "code__1-3__voyage-code-3__v1",
        "code__1-3__bge-base-en-v15-768__v1",
    }
    # The guard runs BEFORE land-then-transform is ever invoked —
    # classification ran (it needs the classifications), but no landing work
    # began.
    assert order == ["classify"]
    assert "land_then_transform" not in order


def test_target_name_collision_between_two_remapped_collections(monkeypatch, _sources):
    """Two DIFFERENT mislabeled collections that both measure/remap to the same
    target — neither is a non-remapped 'honest' collection. Confirms the guard
    catches remapped-vs-remapped collisions, not just remapped-vs-honest."""
    order: list[str] = []
    closables: list[str] = []
    detection = _detection(
        _misnamed_voyage_cls("code__1-3__voyage-code-3__v1", "local"),
        _cross_model_cls("code__1-3__minilm-l6-v2-384__v1", "local"),
    )
    _patch_engine(
        monkeypatch,
        detection=detection,
        outcome=_outcome(ok=True, phase="migrated"),
        closables=closables,
        order=order,
        voyage_key=False,
    )
    with pytest.raises(driver.TargetNameCollisionBlocked) as exc_info:
        driver.run_guided_upgrade(
            sources=_sources,
            vector_client=object(),
            catalog_client=object(),
            t2_db_path=_sources.sqlite_path,
        )
    message = str(exc_info.value)
    assert "code__1-3__voyage-code-3__v1" in message
    assert "code__1-3__minilm-l6-v2-384__v1" in message
    assert "code__1-3__bge-base-en-v15-768__v1" in message  # the shared target
    assert order == ["classify"]


def test_target_name_no_collision_when_targets_distinct(monkeypatch, _sources):
    """No false positive: distinct remap/same-name targets across collections
    must NOT trip the guard — migration proceeds through land-then-transform."""
    order: list[str] = []
    closables: list[str] = []
    detection = _detection(
        _cls("code__o__bge-base-en-v15-768__v1", "local", model=_ONNX, dim=768),
        _cls("docs__o__voyage-context-3__v1", "cloud", model=_VOYAGE, dim=1024),
    )
    _patch_engine(
        monkeypatch,
        detection=detection,
        outcome=_outcome(
            ok=True, phase="migrated",
            collections=(
                "code__o__bge-base-en-v15-768__v1", "docs__o__voyage-context-3__v1",
            ),
        ),
        closables=closables,
        order=order,
    )
    result = driver.run_guided_upgrade(
        sources=_sources,
        vector_client=object(),
        catalog_client=object(),
        t2_db_path=_sources.sqlite_path,
    )
    assert result.ok is True
    assert order == ["classify", "land_then_transform"]


def test_target_name_collision_across_different_legs(monkeypatch, _sources):
    """The grouping is leg-agnostic (previously untested): two honest,
    non-remapped, IDENTICALLY-named collections on DIFFERENT legs (local +
    cloud) are two genuinely distinct data sources landing on one pgvector
    target — this must collide, same as two same-leg sources."""
    order: list[str] = []
    closables: list[str] = []
    detection = _detection(
        _cls("code__o__bge-base-en-v15-768__v1", "local", model=_ONNX, dim=768),
        _cls("code__o__bge-base-en-v15-768__v1", "cloud", model=_ONNX, dim=768),
    )
    _patch_engine(
        monkeypatch,
        detection=detection,
        outcome=_outcome(ok=True, phase="migrated"),
        closables=closables,
        order=order,
        voyage_key=False,
    )
    with pytest.raises(driver.TargetNameCollisionBlocked) as exc_info:
        driver.run_guided_upgrade(
            sources=_sources,
            vector_client=object(),
            catalog_client=object(),
            t2_db_path=_sources.sqlite_path,
        )
    collisions = exc_info.value.collisions
    assert set(collisions.keys()) == {"code__o__bge-base-en-v15-768__v1"}
    legs = {c.leg for c in collisions["code__o__bge-base-en-v15-768__v1"]}
    assert legs == {"local", "cloud"}
    assert order == ["classify"]


def test_target_name_collision_three_way(monkeypatch, _sources):
    """The guard is provably N-way generic (`len(sources) > 1`), not just
    pairwise — regression firming this up for N=3 (previously untested)."""
    order: list[str] = []
    closables: list[str] = []
    detection = _detection(
        _misnamed_voyage_cls("code__1-3__voyage-code-3__v1", "local"),
        _cross_model_cls("code__1-3__minilm-l6-v2-384__v1", "local"),
        _cls("code__1-3__bge-base-en-v15-768__v1", "local", model=_ONNX, dim=768),
    )
    _patch_engine(
        monkeypatch,
        detection=detection,
        outcome=_outcome(ok=True, phase="migrated"),
        closables=closables,
        order=order,
        voyage_key=False,
    )
    with pytest.raises(driver.TargetNameCollisionBlocked) as exc_info:
        driver.run_guided_upgrade(
            sources=_sources,
            vector_client=object(),
            catalog_client=object(),
            t2_db_path=_sources.sqlite_path,
        )
    collisions = exc_info.value.collisions
    sources = collisions["code__1-3__bge-base-en-v15-768__v1"]
    assert {c.collection for c in sources} == {
        "code__1-3__voyage-code-3__v1",
        "code__1-3__minilm-l6-v2-384__v1",
        "code__1-3__bge-base-en-v15-768__v1",
    }
    assert order == ["classify"]


def test_target_name_collision_message_carries_classification_metadata(
    monkeypatch, _sources
):
    """nexus-5b9v0 Fix 3: the exception's structured `.collisions` and its
    rendered message must surface per-source model/measured_dim/reason, so an
    operator can tell WHICH colliding source is the stale pre-RDR-109 mislabel
    versus the honest sibling, without re-deriving the classification by
    hand."""
    order: list[str] = []
    closables: list[str] = []
    detection = _detection(
        _misnamed_voyage_cls("code__1-3__voyage-code-3__v1", "local"),
        _cls("code__1-3__bge-base-en-v15-768__v1", "local", model=_ONNX, dim=768),
    )
    _patch_engine(
        monkeypatch,
        detection=detection,
        outcome=_outcome(ok=True, phase="migrated"),
        closables=closables,
        order=order,
        voyage_key=False,
    )
    with pytest.raises(driver.TargetNameCollisionBlocked) as exc_info:
        driver.run_guided_upgrade(
            sources=_sources,
            vector_client=object(),
            catalog_client=object(),
            t2_db_path=_sources.sqlite_path,
        )
    sources = exc_info.value.collisions["code__1-3__bge-base-en-v15-768__v1"]
    by_name = {c.collection: c for c in sources}
    mislabeled = by_name["code__1-3__voyage-code-3__v1"]
    assert mislabeled.measured_dim == 768
    assert mislabeled.model == "voyage-code-3"
    assert "NX_VOYAGE_API_KEY" in mislabeled.reason
    honest = by_name["code__1-3__bge-base-en-v15-768__v1"]
    assert honest.model == "bge-base-en-v15-768"

    message = str(exc_info.value)
    assert "768-dim" in message
    assert "voyage-code-3" in message
    assert "NX_VOYAGE_API_KEY" in message


def _derived_cls(leg: str) -> CollectionClassification:
    """A non-remappable, two-segment DERIVED collection (RDR-178 Gap 6) —
    present on this leg WITH data, but land-then-transform disposes of it as
    never-written (regenerable from T2 taxonomy), never landing it anywhere.
    `cross_model_remappable` is False for it (model is None), so
    `target_names` never maps it — it resolves to its own literal name."""
    return CollectionClassification(
        collection="taxonomy__centroids",
        leg=leg,  # type: ignore[arg-type]
        model=None,
        dim=None,
        support="unsupported",
        source_count=447,
        has_data=True,
        reason=(
            "collection name is not four-segment conformant "
            "(<content_type>__<owner>__<model>__v<n>) — cannot resolve an "
            "embedding model; re-index under a conformant name"
        ),
    )


def test_target_name_collision_false_positive_derived_collection_excluded(
    monkeypatch, _sources
):
    """nexus-5b9v0 Fix 2: a non-remappable, two-segment DERIVED collection
    (`taxonomy__centroids`) present with data on BOTH legs maps to its own
    literal name on each leg — a naive grouping would see 2 distinct sources
    claiming one target and BLOCK. But land-then-transform's `_land`
    (`vector_etl.is_never_written`) never actually lands either copy on
    either leg — this must NOT collide."""
    order: list[str] = []
    closables: list[str] = []
    detection = _detection(
        _derived_cls("local"),
        _derived_cls("cloud"),
    )
    _patch_engine(
        monkeypatch,
        detection=detection,
        outcome=_outcome(
            ok=True, phase="migrated", collections=("taxonomy__centroids",)
        ),
        closables=closables,
        order=order,
    )
    result = driver.run_guided_upgrade(
        sources=_sources,
        vector_client=object(),
        catalog_client=object(),
        t2_db_path=_sources.sqlite_path,
    )
    assert result.ok is True
    assert order == ["classify", "land_then_transform"]


def _ephemeral_cls(leg: str) -> CollectionClassification:
    """A non-conformant `tuples__*` session-ephemeral collection (excluded
    from DEFAULT enumeration by `vector_etl.EPHEMERAL_EXCLUDE_PREFIXES`) —
    present on this leg WITH data. NOT on the `_DERIVED_COLLECTIONS`
    allowlist, so `is_derived_skip` alone is False for it; land-then-
    transform still never lands it (a completely separate exclusion
    mechanism folded into `vector_etl.is_never_written`)."""
    return CollectionClassification(
        collection="tuples__hook_events_notification",
        leg=leg,  # type: ignore[arg-type]
        model=None,
        dim=None,
        support="unsupported",
        source_count=64,
        has_data=True,
        reason=(
            "collection name is not four-segment conformant "
            "(<content_type>__<owner>__<model>__v<n>) — cannot resolve an "
            "embedding model; re-index under a conformant name"
        ),
    )


def test_target_name_collision_false_positive_ephemeral_collection_excluded(
    monkeypatch, _sources
):
    """nexus-5b9v0 Fix A (round-2): a session-ephemeral `tuples__*` collection
    present with data on BOTH legs maps to its own literal name on each leg —
    same false-positive shape as the derived-collection case, but via the
    STRUCTURALLY DISTINCT `EPHEMERAL_EXCLUDE_PREFIXES` exclusion (not the
    `_DERIVED_COLLECTIONS` allowlist `is_derived_skip` alone covers). The
    guard must exempt this too — it is never actually landed by the DEFAULT
    enumeration `run_guided_upgrade` always drives."""
    order: list[str] = []
    closables: list[str] = []
    detection = _detection(
        _ephemeral_cls("local"),
        _ephemeral_cls("cloud"),
    )
    _patch_engine(
        monkeypatch,
        detection=detection,
        outcome=_outcome(
            ok=True, phase="migrated",
            collections=("tuples__hook_events_notification",),
        ),
        closables=closables,
        order=order,
    )
    result = driver.run_guided_upgrade(
        sources=_sources,
        vector_client=object(),
        catalog_client=object(),
        t2_db_path=_sources.sqlite_path,
    )
    assert result.ok is True
    assert order == ["classify", "land_then_transform"]


def test_target_name_collision_message_flags_likely_stale_source(
    monkeypatch, _sources
):
    """nexus-5b9v0 Fix C (round-2): the rendered message must explicitly flag
    WHICH colliding source is the likely-stale pre-RDR-109 mislabel (a
    measured-dim override — declared unsupported/voyage but measured as
    local bge/ONNX), rather than presenting symmetric bullets the operator
    has to compare by hand."""
    order: list[str] = []
    closables: list[str] = []
    detection = _detection(
        _misnamed_voyage_cls("code__1-3__voyage-code-3__v1", "local"),
        _cls("code__1-3__bge-base-en-v15-768__v1", "local", model=_ONNX, dim=768),
    )
    _patch_engine(
        monkeypatch,
        detection=detection,
        outcome=_outcome(ok=True, phase="migrated"),
        closables=closables,
        order=order,
        voyage_key=False,
    )
    with pytest.raises(driver.TargetNameCollisionBlocked) as exc_info:
        driver.run_guided_upgrade(
            sources=_sources,
            vector_client=object(),
            catalog_client=object(),
            t2_db_path=_sources.sqlite_path,
        )
    message = str(exc_info.value)
    stale_line = next(
        line for line in message.splitlines()
        if "code__1-3__voyage-code-3__v1" in line
    )
    honest_line = next(
        line for line in message.splitlines()
        if "code__1-3__bge-base-en-v15-768__v1" in line
        and "target" not in line  # skip the "target ... would be written" header line
    )
    assert "LIKELY STALE" in stale_line
    assert "LIKELY STALE" not in honest_line
