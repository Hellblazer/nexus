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

import pytest

from nexus.migration import driver
from nexus.migration.detection import CollectionClassification, DetectionReport
from nexus.migration.orchestrator import EtlSources
from nexus.migration.sequencer import SequenceOutcome
from nexus.migration.validation import ValidationChecks, ValidationOutcome

_ONNX = "minilm-l6-v2-384"
_VOYAGE = "voyage-context-3"


def _cls(
    collection: str, leg: str, *, model: str = _ONNX, dim: int = 384, has_data: bool = True
) -> CollectionClassification:
    return CollectionClassification(
        collection=collection,
        leg=leg,  # type: ignore[arg-type]
        model=model,
        dim=dim,
        support="supported-onnx-384" if model == _ONNX else "supported-voyage-1024",
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
):
    """Patch the driver's engine globals; record the detect→sequence ordering."""
    local = _FakeClient("detect-local", closables=closables)
    cloud = _FakeClient("detect-cloud", closables=closables)

    def _open_read_legs(local_path=None):
        return local, cloud

    def _classify(*, local_client, cloud_client, voyage_key_present):
        order.append("classify")
        return detection

    def _run_sequenced(det, *, sources, run_leg, voyage_key_present, on_progress=None):
        order.append("sequence")
        # The detection legs MUST be closed before the ETL sequence runs.
        assert "detect-local" in closables and "detect-cloud" in closables
        return sequence

    def _compose(*, t2_db_path, read_client, vector_client, catalog_client, collections, dims):
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
    monkeypatch.setattr(driver, "voyage_key_available", lambda: False)


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
        _cls("code__o__minilm-l6-v2-384__v1", "local", model=_ONNX, dim=384),
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
        "code__o__minilm-l6-v2-384__v1",
        "docs__o__voyage-context-3__v1",
    }
    assert capture["dims"] == (384, 1024)
    # The composite read client routes each collection to its source leg.
    composite = capture["read_client"]
    assert composite._by_collection["code__o__minilm-l6-v2-384__v1"] is legs_clients["local"]
    assert composite._by_collection["docs__o__voyage-context-3__v1"] is legs_clients["cloud"]
    # Both reopened legs closed.
    assert {"reopen-local", "reopen-cloud"} <= set(closables)


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

    monkeypatch.setattr(driver, "validate_migration", _boom)
    with pytest.raises(RuntimeError, match="gate exploded"):
        driver.run_guided_upgrade(
            sources=_sources,
            vector_client=object(),
            catalog_client=object(),
            t2_db_path=_sources.sqlite_path,
            reopen_leg=lambda leg: _FakeClient(f"reopen-{leg}", closables=closables),
        )
    assert "reopen-local" in closables


def test_composite_read_client_unknown_collection_raises():
    composite = driver._CompositeReadClient({})
    with pytest.raises(RuntimeError, match="no source read leg"):
        composite.get_collection("missing__o__m__v1")
