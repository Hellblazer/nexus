"""Tests for benchmark teardown scoping (nexus-duoak.3)."""

from __future__ import annotations

from teardown_scope import bench_tumblers, plan_teardown

REAL = [
    "code__1-1__voyage-code-3__v1",
    "code__1-15__voyage-code-3__v1",
    "knowledge__knowledge__voyage-context-3__v1",
]

OWNERS = [
    {"tumbler": "1.1", "name": "nexus", "type": "repo"},
    {"tumbler": "1.15", "name": "a2ui", "type": "repo"},
    {"tumbler": "1.47", "name": "benchidx-0704a-w1", "type": "repo"},
    {"tumbler": "1.48", "name": "benchidx-0704a-w2", "type": "repo"},
]


class TestBenchTumblers:
    def test_selects_only_marker_owners_in_dashed_form(self) -> None:
        assert bench_tumblers(OWNERS) == {"1-47", "1-48"}

    def test_no_bench_owners_is_empty(self) -> None:
        assert bench_tumblers(OWNERS[:2]) == set()


class TestPlanTeardown:
    def test_deletes_only_new_bench_owned_collections(self) -> None:
        after = REAL + [
            "code__1-47__voyage-code-3__v1",
            "docs__1-47__voyage-context-3__v1",
            "code__1-48__voyage-code-3__v1",
        ]
        to_delete, unexpected = plan_teardown(REAL, after, {"1-47", "1-48"})
        assert to_delete == [
            "code__1-47__voyage-code-3__v1",
            "docs__1-47__voyage-context-3__v1",
            "code__1-48__voyage-code-3__v1",
        ]
        assert unexpected == []

    def test_never_deletes_preexisting_even_if_bench_owned(self) -> None:
        # Survivor of a previous crashed run: in BOTH snapshots -> not ours.
        stale = "code__1-40__voyage-code-3__v1"
        before = REAL + [stale]
        after = before + ["code__1-47__voyage-code-3__v1"]
        to_delete, unexpected = plan_teardown(before, after, {"1-40", "1-47"})
        assert to_delete == ["code__1-47__voyage-code-3__v1"]
        assert stale not in to_delete

    def test_new_nonbench_collection_reported_never_deleted(self) -> None:
        after = REAL + ["code__1-16__voyage-code-3__v1"]
        to_delete, unexpected = plan_teardown(REAL, after, {"1-47"})
        assert to_delete == []
        assert unexpected == ["code__1-16__voyage-code-3__v1"]

    def test_no_changes_is_clean_noop(self) -> None:
        assert plan_teardown(REAL, list(REAL), {"1-47"}) == ([], [])

    def test_exact_counts_from_full_sweep(self) -> None:
        # 4 worker runs x (code + docs) each = exactly 8 deletions.
        after = list(REAL)
        tumblers = set()
        for i, _w in enumerate((1, 2, 4, 8)):
            t = f"1-5{i}"
            tumblers.add(t)
            after.append(f"code__{t}__voyage-code-3__v1")
            after.append(f"docs__{t}__voyage-context-3__v1")
        to_delete, unexpected = plan_teardown(REAL, after, tumblers)
        assert len(to_delete) == 8
        assert unexpected == []

    def test_malformed_collection_name_is_unexpected_not_deleted(self) -> None:
        after = REAL + ["weird_name_no_double_underscores"]
        to_delete, unexpected = plan_teardown(REAL, after, {"1-47"})
        assert to_delete == []
        assert unexpected == ["weird_name_no_double_underscores"]
