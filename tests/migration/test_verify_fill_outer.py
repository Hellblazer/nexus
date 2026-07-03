# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-178 wave-2 verify-fill P2 (nexus-s3dd4.2): the outer count-diff loop.

``nexus.migration.verify_fill.verify_store_counts`` diffs a store's per-table
source (SQLite) counts against the target's ``relation_counts`` surface via
the existing :class:`~nexus.migration.orchestrator.CountSource` Protocol —
NO new engine endpoint. A count-parity table is the caller's signal to SKIP
the (later-bead) inner identity-diff + fill loop.
"""
from __future__ import annotations

from nexus.migration import verify_fill as vf


# ── Fakes ────────────────────────────────────────────────────────────────────


class _FakeCountSource:
    """A :class:`CountSource` returning canned target counts (or ``None`` to
    simulate an unreachable count source → indeterminate)."""

    def __init__(self, counts: dict[str, int] | None):
        self._counts = counts
        self.seen: list[str] | None = None
        self.call_count = 0

    def counts(self, relations: list[str]) -> dict[str, int] | None:
        self.call_count += 1
        self.seen = list(relations)
        return self._counts


# ── Basic (non-dedup) parity / divergence / indeterminacy ────────────────────


class TestVerifyStoreCountsBasic:
    def test_parity_table_marked_parity(self) -> None:
        result = vf.verify_store_counts(
            "memory", _FakeCountSource({"nexus.memory": 10}), {"memory": 10},
        )
        assert result == {
            "memory": {"source_count": 10, "target_count": 10, "status": "parity"},
        }

    def test_target_exceeding_source_is_still_parity(self) -> None:
        # e.g. a prior run already landed extra rows — never a hole.
        result = vf.verify_store_counts(
            "memory", _FakeCountSource({"nexus.memory": 15}), {"memory": 10},
        )
        assert result["memory"]["status"] == "parity"

    def test_short_table_marked_divergent(self) -> None:
        result = vf.verify_store_counts(
            "memory", _FakeCountSource({"nexus.memory": 3}), {"memory": 10},
        )
        assert result == {
            "memory": {"source_count": 10, "target_count": 3, "status": "divergent"},
        }

    def test_unreachable_source_marks_indeterminate(self) -> None:
        result = vf.verify_store_counts(
            "memory", _FakeCountSource(None), {"memory": 10},
        )
        assert result == {
            "memory": {"source_count": 10, "target_count": None, "status": "indeterminate"},
        }

    def test_response_missing_relation_marks_indeterminate(self) -> None:
        # count source reachable but omitted this relation from the response
        result = vf.verify_store_counts(
            "memory", _FakeCountSource({}), {"memory": 10},
        )
        assert result == {
            "memory": {"source_count": 10, "target_count": None, "status": "indeterminate"},
        }

    def test_unmapped_table_marks_indeterminate_without_querying(self) -> None:
        src = _FakeCountSource({"nexus.memory": 10})
        result = vf.verify_store_counts(
            "telemetry", src, {"some_unmapped_table": 5},
        )
        assert result == {
            "some_unmapped_table": {
                "source_count": 5, "target_count": None, "status": "indeterminate",
            },
        }
        # unmapped table never generates a relation to query
        assert src.call_count == 0
        assert src.seen is None

    def test_empty_source_counts_returns_empty_dict_no_query(self) -> None:
        src = _FakeCountSource({"nexus.memory": 10})
        result = vf.verify_store_counts("memory", src, {})
        assert result == {}
        assert src.call_count == 0

    def test_zero_source_count_zero_target_is_parity(self) -> None:
        # idempotent re-run against an already-empty relation: 0 >= 0.
        result = vf.verify_store_counts(
            "memory", _FakeCountSource({"nexus.memory": 0}), {"memory": 0},
        )
        assert result["memory"]["status"] == "parity"


# ── Multi-table batching ──────────────────────────────────────────────────────


class TestVerifyStoreCountsBatching:
    def test_multiple_tables_batched_into_one_call(self) -> None:
        src = _FakeCountSource({
            "nexus.catalog_owners": 5,
            "nexus.catalog_documents": 2,
        })
        result = vf.verify_store_counts(
            "catalog", src,
            {"owners": 5, "documents": 8},
        )
        assert src.call_count == 1
        assert set(src.seen) == {"nexus.catalog_owners", "nexus.catalog_documents"}
        assert result["owners"]["status"] == "parity"
        assert result["documents"]["status"] == "divergent"

    def test_duplicate_relations_not_queried_twice(self) -> None:
        # taxonomy has three distinct relations; verify no accidental dupes
        # inflate the batched query even when counts coincide.
        src = _FakeCountSource({
            "nexus.topics": 4,
            "nexus.topic_assignments": 4,
            "nexus.topic_links": 4,
        })
        vf.verify_store_counts(
            "taxonomy", src,
            {"topics": 4, "topic_assignments": 4, "topic_links": 4},
        )
        assert src.call_count == 1
        assert len(src.seen) == len(set(src.seen))


# ── Dedup guard (audit correction 2, plans convergence) ───────────────────────


class TestVerifyStoreCountsDedupGuard:
    def test_plans_convergence_collapse_marked_parity(self) -> None:
        # source dups converge onto UNIQUE(tenant_id, project, query) — by
        # design, not a hole. Caller must SKIP the inner fill here.
        result = vf.verify_store_counts(
            "plans", _FakeCountSource({"nexus.plans": 80}), {"plans": 98},
        )
        assert result == {
            "plans": {"source_count": 98, "target_count": 80, "status": "parity"},
        }

    def test_plans_written_zero_is_trivial_parity(self) -> None:
        result = vf.verify_store_counts(
            "plans", _FakeCountSource({"nexus.plans": 50}), {"plans": 0},
        )
        assert result["plans"]["status"] == "parity"

    def test_plans_exact_match_is_parity(self) -> None:
        result = vf.verify_store_counts(
            "plans", _FakeCountSource({"nexus.plans": 98}), {"plans": 98},
        )
        assert result["plans"]["status"] == "parity"

    def test_plans_zero_landed_from_nonzero_write_is_divergent(self) -> None:
        # nothing landed despite a non-zero write — a real hole, not
        # convergence.
        result = vf.verify_store_counts(
            "plans", _FakeCountSource({"nexus.plans": 0}), {"plans": 98},
        )
        assert result["plans"]["status"] == "divergent"

    def test_plans_target_exceeding_source_is_divergent(self) -> None:
        # impossible under DO UPDATE in steady state, but treated
        # defensively as divergent rather than a silent parity.
        result = vf.verify_store_counts(
            "plans", _FakeCountSource({"nexus.plans": 120}), {"plans": 98},
        )
        assert result["plans"]["status"] == "divergent"

    def test_plans_indeterminate_takes_precedence_over_dedup_logic(self) -> None:
        result = vf.verify_store_counts(
            "plans", _FakeCountSource(None), {"plans": 98},
        )
        assert result["plans"]["status"] == "indeterminate"
