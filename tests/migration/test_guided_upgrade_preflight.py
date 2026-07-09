# SPDX-License-Identifier: AGPL-3.0-or-later
"""ez5.2 — pre-flight migration detection for ``nx guided-upgrade``.

The command must decide whether there is anything to migrate BEFORE it
provisions and serves the engine-service (ez5.6): a fresh user with no
legacy Chroma footprint must short-circuit to a no-op, never stand up a
service for nothing. This is a thin CONSUMER of the existing
``detection.classify_collections`` (RDR-159/162) — no reclassification logic
lives here.
"""

from __future__ import annotations

import pytest

from nexus.migration.guided_upgrade import (
    PreflightDetection,
    detect_pending_migration,
)

# Conformant collection names by support class (mirror test_detection.py).
ONNX_768 = "code__nexus-1-1__bge-base-en-v15-768__v1"
MINILM_384 = "knowledge__nexus-1-1__minilm-l6-v2-384__v1"  # legacy, unsupported
VOYAGE_1024 = "knowledge__nexus-1-1__voyage-context-3__v1"


class _FakeCollection:
    def __init__(self, name: str, count: int) -> None:
        self.name = name
        self._count = count

    def count(self) -> int:
        return self._count

    def get(self, limit: int = 1, include: list | None = None) -> dict:
        # nexus-nb7hr: the classifier's ground-truth dim probe. No stored
        # embeddings modeled here -> measured_dim None -> name-based
        # classification unchanged (these tests pin the legacy behavior).
        return {"ids": [], "embeddings": None}


class _FakeChromaClient:
    def __init__(self, counts: dict[str, int]) -> None:
        self._counts = dict(counts)
        self.closed = False

    def list_collections(self) -> list[_FakeCollection]:
        return [_FakeCollection(n, c) for n, c in self._counts.items()]

    def get_collection(self, name: str) -> _FakeCollection:
        return _FakeCollection(name, self._counts[name])


def _legs(local: _FakeChromaClient | None, cloud: _FakeChromaClient | None):
    """Build an ``open_legs`` injection returning the given doubles."""

    def _open(_local_path):  # noqa: ANN001, ANN202
        return local, cloud

    return _open


class TestDetectPendingMigration:
    def test_fresh_user_no_legs_is_no_op(self) -> None:
        result = detect_pending_migration(
            voyage_key_present=False, open_legs=_legs(None, None)
        )
        assert isinstance(result, PreflightDetection)
        assert result.needs_migration is False
        assert result.report.classifications == ()
        assert result.data_bearing_count == 0

    def test_empty_collections_only_is_no_op(self) -> None:
        # Legs exist but every collection is empty -> not data-bearing.
        local = _FakeChromaClient({ONNX_768: 0, MINILM_384: 0})
        result = detect_pending_migration(
            voyage_key_present=False, open_legs=_legs(local, None)
        )
        assert result.needs_migration is False
        assert result.data_bearing_count == 0

    def test_legacy_minilm_with_data_needs_migration(self) -> None:
        local = _FakeChromaClient({MINILM_384: 42})
        result = detect_pending_migration(
            voyage_key_present=False, open_legs=_legs(local, None)
        )
        assert result.needs_migration is True
        assert result.data_bearing_count == 1
        # minilm-384 is classified unsupported; this is the RAW classification
        # count (RDR-162 auto-remaps it — it is NOT the genuinely-blocked count).
        assert result.classified_unsupported_count == 1

    def test_supported_data_still_needs_migration(self) -> None:
        # Even a fully-supported collection must move Chroma -> pgvector.
        local = _FakeChromaClient({ONNX_768: 7})
        result = detect_pending_migration(
            voyage_key_present=False, open_legs=_legs(local, None)
        )
        assert result.needs_migration is True
        assert result.data_bearing_count == 1
        assert result.classified_unsupported_count == 0

    def test_both_legs_aggregated(self) -> None:
        local = _FakeChromaClient({ONNX_768: 3})
        cloud = _FakeChromaClient({VOYAGE_1024: 5})
        result = detect_pending_migration(
            voyage_key_present=True, open_legs=_legs(local, cloud)
        )
        assert result.needs_migration is True
        assert result.data_bearing_count == 2
        assert set(result.report.legs_with_data) == {"local", "cloud"}
        # Pin count propagation: a degenerate impl that flags any non-None leg
        # data-bearing without reading counts would not sum to 8.
        assert sum(c.source_count for c in result.report.classifications) == 8

    def test_single_leg_close_does_not_dispatch_absent_leg(self) -> None:
        # WAL single-opener invariant: the local leg MUST be closed before the
        # ETL/migration reopens it. An ABSENT leg is never dispatched to the
        # close hook (so injected hooks need not tolerate None).
        local = _FakeChromaClient({ONNX_768: 1})
        closed: list[object] = []
        detect_pending_migration(
            voyage_key_present=False,
            open_legs=_legs(local, None),
            close_leg=closed.append,
        )
        assert closed == [local]

    def test_both_legs_are_closed_after_detection(self) -> None:
        # The two-leg close contract: BOTH opened legs are closed, not just
        # the local one (a regression closing only local would strand cloud).
        local = _FakeChromaClient({ONNX_768: 1})
        cloud = _FakeChromaClient({VOYAGE_1024: 1})
        closed: list[object] = []
        detect_pending_migration(
            voyage_key_present=True,
            open_legs=_legs(local, cloud),
            close_leg=closed.append,
        )
        assert set(map(id, closed)) == {id(local), id(cloud)}

    def test_enumeration_failure_is_loud_and_still_closes(self) -> None:
        class _Boom:
            def list_collections(self):  # noqa: ANN202
                raise RuntimeError("corrupt sqlite header: not a database")

        boom = _Boom()
        closed: list[object] = []
        with pytest.raises(RuntimeError, match="corrupt sqlite header"):
            detect_pending_migration(
                voyage_key_present=False,
                open_legs=_legs(boom, None),
                close_leg=closed.append,
            )
        # The close-in-finally fires even when classification raises.
        assert closed == [boom]
