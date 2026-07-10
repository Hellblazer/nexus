# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-159 P4 (nexus-ue6g7.24) — the guided-upgrade engine entry point.

``run_guided_upgrade`` is the ONE function both the nexus CLI and the deferred
conexus veneer call. These tests pin its LIFECYCLE contract with the P0-P3
engine functions injected (monkeypatched on the ``driver`` module), so the
sequencing + open/close discipline is verified without a live service / Chroma:

* fresh-user (no data) → clean no-op, no sequence side effects, no validation;
* a sequence block / partial-leg → ok=False, validation never runs;
* a clean ``migrated`` → validation runs over the data-bearing legs and the
  unlock verdict is the result's ``ok``;
* the detection read legs are CLOSED before the ETL sequence runs (the local
  WAL single-opener invariant);
* the reopened validation legs are closed afterward, even on a raising gate;
* a two-leg migration routes each collection's count check to its source leg.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus.migration import driver
from nexus.migration.detection import CollectionClassification, DetectionReport
from nexus.migration.orchestrator import EtlSources
from nexus.migration.sequencer import SequenceOutcome
from nexus.migration.validation import ValidationChecks, ValidationOutcome

_ONNX = "bge-base-en-v15-768"
_VOYAGE = "voyage-context-3"


@pytest.fixture(autouse=True)
def _isolate_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The validation-setup failure path calls the real ``mark_failed`` (which
    writes the sentinel under the config dir) — isolate it off the real
    ~/.config/nexus."""
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


def _sequence(*, ok: bool, phase: str) -> SequenceOutcome:
    return SequenceOutcome(
        ok=ok,
        phase=phase,
        collections_total=1,
        collections_done=1 if ok else 0,
        t2_total_failed=0,
        legs_attempted=("local",),
        legs_ok=("local",) if ok else (),
        blocked_reason=None if ok else "blocked",
        t2_report={"summary": {"total_failed": 0}},
    )


def _validation(*, unlocked: bool) -> ValidationOutcome:
    return ValidationOutcome(
        unlocked=unlocked,
        verdict="verified" if unlocked else "blocked",
        blocking_reasons=() if unlocked else ("counts: mismatch",),
        taxonomy_orphans=(),
        count_mismatches=() if unlocked else ("c1",),
        count_indeterminate=False,
        manifest_orphan_count=0,
        manifest_vacuous=False,
        stale_aspects=0,
        advisory_notes=(),
        rollback_available=not unlocked,
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


def _patch_engine(
    monkeypatch: pytest.MonkeyPatch,
    *,
    detection: DetectionReport,
    sequence: SequenceOutcome,
    closables: list[str],
    order: list[str],
    validation: ValidationOutcome | None = None,
    compose_capture: dict | None = None,
    seq_capture: dict | None = None,
    voyage_key: bool = False,
):
    """Patch the driver's engine globals; record the detect→sequence ordering."""
    local = _FakeClient("detect-local", closables=closables)
    cloud = _FakeClient("detect-cloud", closables=closables)

    def _open_read_legs(local_path=None):
        return local, cloud

    def _classify(*, local_client, cloud_client, voyage_key_present):
        order.append("classify")
        return detection

    def _run_sequenced(
        det, *, sources, run_leg, voyage_key_present, on_progress=None,
        cross_model_targets=None,
    ):
        order.append("sequence")
        # The detection legs MUST be closed before the ETL sequence runs.
        assert "detect-local" in closables and "detect-cloud" in closables
        if seq_capture is not None:
            seq_capture["cross_model_targets"] = cross_model_targets
        return sequence

    def _compose(*, t2_db_path, read_client, vector_client, catalog_client, collections, dims, target_names=None):
        if compose_capture is not None:
            compose_capture["read_client"] = read_client
            compose_capture["collections"] = collections
            compose_capture["dims"] = dims
        return ValidationChecks(
            taxonomy_check=lambda: [],
            count_check=lambda: {},
            manifest_orphan_check=lambda: 0,
        )

    def _validate(*, taxonomy_check, count_check, manifest_orphan_check, stale_aspects_count=0):
        order.append("validate")
        assert validation is not None
        return validation

    monkeypatch.setattr(driver, "open_read_legs", _open_read_legs)
    monkeypatch.setattr(driver, "classify_collections", _classify)
    monkeypatch.setattr(driver, "run_sequenced_migration", _run_sequenced)
    monkeypatch.setattr(driver, "compose_validation_checks", _compose)
    monkeypatch.setattr(driver, "validate_migration", _validate)
    monkeypatch.setattr(driver, "voyage_key_available", lambda: voyage_key)


def test_fresh_user_noop_clean_success(monkeypatch, _sources):
    order: list[str] = []
    closables: list[str] = []
    _patch_engine(
        monkeypatch,
        detection=_detection(),  # no data-bearing collections
        sequence=_sequence(ok=True, phase="not-migrating"),
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
    # Detection clients closed; validation never reached.
    assert "validate" not in order
    assert set(closables) == {"detect-local", "detect-cloud"}


def test_run_t2_passthrough_only_when_provided(monkeypatch, _sources):
    """RDR-178 Gap 7 (nexus-1sx01): ``run_t2`` is forwarded to the sequencer
    ONLY when the caller explicitly supplies it — omitting it preserves the
    sequencer's own default (``migrate_all``), never an explicit ``None``
    overriding it (which would break every caller unaware of this seam)."""
    captured: dict[str, object] = {}

    def _open_read_legs(local_path=None):
        return _FakeClient("l", closables=[]), _FakeClient("c", closables=[])

    def _classify(*, local_client, cloud_client, voyage_key_present):
        return _detection()

    def _run_sequenced(
        det, *, sources, run_leg, voyage_key_present,
        on_progress=None, cross_model_targets=None, run_t2="UNSET",
    ):
        captured["run_t2"] = run_t2
        return _sequence(ok=True, phase="not-migrating")

    monkeypatch.setattr(driver, "open_read_legs", _open_read_legs)
    monkeypatch.setattr(driver, "classify_collections", _classify)
    monkeypatch.setattr(driver, "run_sequenced_migration", _run_sequenced)

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


def test_sequence_block_no_validation(monkeypatch, _sources):
    order: list[str] = []
    closables: list[str] = []
    _patch_engine(
        monkeypatch,
        detection=_detection(_cls("code__o__m__v1", "local")),
        sequence=_sequence(ok=False, phase="migrated-failed"),
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
    assert "validate" not in order


def test_clean_migrated_validates_and_unlocks(monkeypatch, _sources):
    order: list[str] = []
    closables: list[str] = []
    reopened: list[str] = []
    _patch_engine(
        monkeypatch,
        detection=_detection(_cls("code__o__m__v1", "local")),
        sequence=_sequence(ok=True, phase="migrated"),
        closables=closables,
        order=order,
        validation=_validation(unlocked=True),
    )

    def _reopen(leg: str):
        reopened.append(leg)
        return _FakeClient(f"reopen-{leg}", closables=closables)

    result = driver.run_guided_upgrade(
        sources=_sources,
        vector_client=object(),
        catalog_client=object(),
        t2_db_path=_sources.sqlite_path,
        reopen_leg=_reopen,
    )
    assert result.ok is True
    assert result.validation is not None and result.validation.unlocked
    assert order == ["classify", "sequence", "validate"]
    assert reopened == ["local"]
    # Reopened leg closed after validation.
    assert "reopen-local" in closables


def test_clean_migrated_validation_blocks_offers_rollback(monkeypatch, _sources):
    order: list[str] = []
    closables: list[str] = []
    _patch_engine(
        monkeypatch,
        detection=_detection(_cls("code__o__m__v1", "local")),
        sequence=_sequence(ok=True, phase="migrated"),
        closables=closables,
        order=order,
        validation=_validation(unlocked=False),
    )
    result = driver.run_guided_upgrade(
        sources=_sources,
        vector_client=object(),
        catalog_client=object(),
        t2_db_path=_sources.sqlite_path,
        reopen_leg=lambda leg: _FakeClient(f"reopen-{leg}", closables=closables),
    )
    assert result.ok is False
    assert result.validation is not None and not result.validation.unlocked
    assert result.rollback_available is True


def test_two_leg_composes_collections_and_dims(monkeypatch, _sources):
    """A local+cloud migration validates over the union, with distinct dims, and
    routes each collection's count check to its source leg."""
    order: list[str] = []
    closables: list[str] = []
    capture: dict = {}
    detection = _detection(
        _cls("code__o__bge-base-en-v15-768__v1", "local", model=_ONNX, dim=768),
        _cls("docs__o__voyage-context-3__v1", "cloud", model=_VOYAGE, dim=1024),
    )
    _patch_engine(
        monkeypatch,
        detection=detection,
        sequence=_sequence(ok=True, phase="migrated"),
        closables=closables,
        order=order,
        validation=_validation(unlocked=True),
        compose_capture=capture,
    )
    legs_clients = {}

    def _reopen(leg: str):
        client = _FakeClient(f"reopen-{leg}", closables=closables)
        legs_clients[leg] = client
        return client

    result = driver.run_guided_upgrade(
        sources=_sources,
        vector_client=object(),
        catalog_client=object(),
        t2_db_path=_sources.sqlite_path,
        reopen_leg=_reopen,
    )
    assert result.ok is True
    assert set(capture["collections"]) == {
        "code__o__bge-base-en-v15-768__v1",
        "docs__o__voyage-context-3__v1",
    }
    assert capture["dims"] == (768, 1024)
    # The composite read client routes each collection to its source leg.
    composite = capture["read_client"]
    assert composite._by_collection["code__o__bge-base-en-v15-768__v1"] is legs_clients["local"]
    assert composite._by_collection["docs__o__voyage-context-3__v1"] is legs_clients["cloud"]
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
        sequence=_sequence(ok=False, phase="migrated-failed"),
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
        sequence=_sequence(ok=False, phase="migrated-failed"),
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


def test_reopened_legs_closed_when_validation_raises(monkeypatch, _sources):
    order: list[str] = []
    closables: list[str] = []
    _patch_engine(
        monkeypatch,
        detection=_detection(_cls("code__o__m__v1", "local")),
        sequence=_sequence(ok=True, phase="migrated"),
        closables=closables,
        order=order,
        validation=_validation(unlocked=True),
    )

    def _boom(**_kwargs):
        raise RuntimeError("gate exploded")

    marked: list[str] = []
    monkeypatch.setattr(driver, "validate_migration", _boom)
    monkeypatch.setattr(driver, "mark_failed", lambda reason: marked.append(reason))
    with pytest.raises(RuntimeError, match="gate exploded"):
        driver.run_guided_upgrade(
            sources=_sources,
            vector_client=object(),
            catalog_client=object(),
            t2_db_path=_sources.sqlite_path,
            reopen_leg=lambda leg: _FakeClient(f"reopen-{leg}", closables=closables),
        )
    assert "reopen-local" in closables
    # The T3 copy was done; an exploding gate must NOT strand the sentinel at
    # an unvalidated `migrated` — it transitions to migrated-failed (HIGH-1).
    assert marked and "gate exploded" in marked[0]


def test_reopen_leg_raises_marks_failed_not_stranded(monkeypatch, _sources):
    """A reopened read leg vanishing between sequence and validation must
    transition the sentinel to migrated-failed, never leave it `migrated`."""
    order: list[str] = []
    closables: list[str] = []
    marked: list[str] = []
    _patch_engine(
        monkeypatch,
        detection=_detection(_cls("code__o__m__v1", "local")),
        sequence=_sequence(ok=True, phase="migrated"),
        closables=closables,
        order=order,
        validation=_validation(unlocked=True),
    )
    monkeypatch.setattr(driver, "mark_failed", lambda reason: marked.append(reason))

    def _reopen_boom(leg: str):
        raise RuntimeError("chroma store gone")

    with pytest.raises(RuntimeError, match="chroma store gone"):
        driver.run_guided_upgrade(
            sources=_sources,
            vector_client=object(),
            catalog_client=object(),
            t2_db_path=_sources.sqlite_path,
            reopen_leg=_reopen_boom,
        )
    assert "validate" not in order
    assert marked and "validation could not be performed" in marked[0]


def test_reopen_leg_filenotfound_wrapped_as_runtimeerror(monkeypatch, _sources):
    """nexus-5b9v0 round-3 Fix D (bead nexus-rndvq, CRITICAL): the validation-
    setup except-block used to bare `raise`, re-propagating whatever exception
    type the reopen actually raised. In production this is a FileNotFoundError
    — exactly what ``chroma_read.open_local_read_client`` raises (line 64) when
    the local Chroma store vanished between the ETL write and the validation
    reopen, the scenario this except-block's own comment names as motivating.
    FileNotFoundError is NOT a RuntimeError subclass, so
    ``migrate_cmd.py``'s ``except RuntimeError`` guard could never catch it —
    the operator still got a raw traceback for this exact failure mode. The
    fix wraps at the origin: `raise RuntimeError(reason) from exc` so ANY
    RuntimeError-catching caller (present or future) gets this covered,
    while `__cause__` still carries the original FileNotFoundError for a
    caller that wants to recover it."""
    order: list[str] = []
    closables: list[str] = []
    marked: list[str] = []
    _patch_engine(
        monkeypatch,
        detection=_detection(_cls("code__o__m__v1", "local")),
        sequence=_sequence(ok=True, phase="migrated"),
        closables=closables,
        order=order,
        validation=_validation(unlocked=True),
    )
    monkeypatch.setattr(driver, "mark_failed", lambda reason: marked.append(reason))

    original = FileNotFoundError(
        "local Chroma store not found at /tmp/gone — nothing to migrate"
    )

    def _reopen_vanished(leg: str):
        raise original

    with pytest.raises(RuntimeError) as exc_info:
        driver.run_guided_upgrade(
            sources=_sources,
            vector_client=object(),
            catalog_client=object(),
            t2_db_path=_sources.sqlite_path,
            reopen_leg=_reopen_vanished,
        )
    # The re-raised exception must be a RuntimeError (catchable by
    # migrate_cmd.py's `except RuntimeError`), NOT the original
    # FileNotFoundError propagating raw.
    assert type(exc_info.value) is RuntimeError
    assert not isinstance(exc_info.value, FileNotFoundError)
    # The original exception must still be recoverable via __cause__.
    assert exc_info.value.__cause__ is original
    assert "local Chroma store not found" in str(exc_info.value)
    assert "validate" not in order
    assert marked and "local Chroma store not found" in marked[0]


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


def test_target_name_collision_blocked_before_sequence(monkeypatch, _sources):
    """nexus-5b9v0 (Steve's real failure): a pre-RDR-109 misnamed-voyage
    collection measures as bge-768 and cross-model-remaps onto the EXACT same
    target name as its honest, non-remapped bge sibling. Both would write into
    one pgvector target under one name — the guard must block this BEFORE any
    ETL, naming both colliding source collections and the shared target."""
    order: list[str] = []
    closables: list[str] = []
    detection = _detection(
        _misnamed_voyage_cls("code__1-3__voyage-code-3__v1", "local"),
        _cls("code__1-3__bge-base-en-v15-768__v1", "local", model=_ONNX, dim=768),
    )
    _patch_engine(
        monkeypatch,
        detection=detection,
        sequence=_sequence(ok=True, phase="migrated"),
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
    # The guard runs BEFORE the sequencer is ever invoked — classification ran
    # (it needs the classifications), but no sequence/ETL work began.
    assert order == ["classify"]
    assert "sequence" not in order


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
        sequence=_sequence(ok=True, phase="migrated"),
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
    must NOT trip the guard — migration proceeds through sequence+validate."""
    order: list[str] = []
    closables: list[str] = []
    detection = _detection(
        _cls("code__o__bge-base-en-v15-768__v1", "local", model=_ONNX, dim=768),
        _cls("docs__o__voyage-context-3__v1", "cloud", model=_VOYAGE, dim=1024),
    )
    _patch_engine(
        monkeypatch,
        detection=detection,
        sequence=_sequence(ok=True, phase="migrated"),
        closables=closables,
        order=order,
        validation=_validation(unlocked=True),
    )
    result = driver.run_guided_upgrade(
        sources=_sources,
        vector_client=object(),
        catalog_client=object(),
        t2_db_path=_sources.sqlite_path,
        reopen_leg=lambda leg: _FakeClient(f"reopen-{leg}", closables=closables),
    )
    assert result.ok is True
    assert order == ["classify", "sequence", "validate"]


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
        sequence=_sequence(ok=True, phase="migrated"),
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
        sequence=_sequence(ok=True, phase="migrated"),
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
        sequence=_sequence(ok=True, phase="migrated"),
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
    present on this leg WITH data, but the ETL disposes of it as
    'skipped-derived' (regenerable from T2 taxonomy), never writing it
    anywhere. `cross_model_remappable` is False for it (model is None), so
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
    claiming one target and BLOCK. But the ETL disposes of it as
    'skipped-derived' on every leg (`vector_etl.is_derived_skip`) and never
    actually writes either copy — this must NOT collide."""
    order: list[str] = []
    closables: list[str] = []
    detection = _detection(
        _derived_cls("local"),
        _derived_cls("cloud"),
    )
    _patch_engine(
        monkeypatch,
        detection=detection,
        sequence=_sequence(ok=True, phase="migrated"),
        closables=closables,
        order=order,
        validation=_validation(unlocked=True),
    )
    result = driver.run_guided_upgrade(
        sources=_sources,
        vector_client=object(),
        catalog_client=object(),
        t2_db_path=_sources.sqlite_path,
        reopen_leg=lambda leg: _FakeClient(f"reopen-{leg}", closables=closables),
    )
    assert result.ok is True
    assert order == ["classify", "sequence", "validate"]


def _ephemeral_cls(leg: str) -> CollectionClassification:
    """A non-conformant `tuples__*` session-ephemeral collection (excluded
    from DEFAULT enumeration by `vector_etl.EPHEMERAL_EXCLUDE_PREFIXES`) —
    present on this leg WITH data. NOT on the `_DERIVED_COLLECTIONS`
    allowlist, so `is_derived_skip` alone is False for it; the ETL still
    never writes it (a completely separate exclusion mechanism, checked
    inline in `migrate_collections`/`migrate_cloud`'s enumeration loop)."""
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
    guard must exempt this too — it is never actually written by the
    DEFAULT enumeration `run_guided_upgrade` always drives."""
    order: list[str] = []
    closables: list[str] = []
    detection = _detection(
        _ephemeral_cls("local"),
        _ephemeral_cls("cloud"),
    )
    _patch_engine(
        monkeypatch,
        detection=detection,
        sequence=_sequence(ok=True, phase="migrated"),
        closables=closables,
        order=order,
        validation=_validation(unlocked=True),
    )
    result = driver.run_guided_upgrade(
        sources=_sources,
        vector_client=object(),
        catalog_client=object(),
        t2_db_path=_sources.sqlite_path,
        reopen_leg=lambda leg: _FakeClient(f"reopen-{leg}", closables=closables),
    )
    assert result.ok is True
    assert order == ["classify", "sequence", "validate"]


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
        sequence=_sequence(ok=True, phase="migrated"),
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
