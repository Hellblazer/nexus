# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-153 Phase 1 (bead nexus-ouuwb, TDD-red): MigrationIssue +
IssueCollector + JSON report serialization.

The report is the triage / recovery / learning artifact for the
SQLite→Postgres T2 migration and a Phase-4 gate input
(``summary.total_failed == 0``). Contract source:
docs/rdr/rdr-153-migration-data-quality-policy.md § JSON report schema.

Two distinct enums, never mixed: ``class`` (what is wrong) vs ``action``
(what the ETL did). ``summary.by_action`` aggregates by the five ACTION
values — the gate-facing rollup.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime

import pytest

from nexus.migration.migration_report import (
    ACTION_SEVERITY,
    IssueCollector,
    MigrationIssue,
    build_report,
)

# ── MigrationIssue ───────────────────────────────────────────────────────────


class TestMigrationIssue:
    def test_carries_full_contract(self) -> None:
        issue = MigrationIssue(
            issue_class="orphan_parent",
            constraint="topic_assignments.topic_id -> topics.id",
            reason="topic_id references a deleted topic",
            action="skipped",
            count=51286,
            sample_ids=["d1:t9", "d2:t9"],
            sample_truncated=True,
        )
        assert issue.issue_class == "orphan_parent"
        assert issue.action == "skipped"
        assert issue.severity == 3  # derived from action, see ordinals below
        assert issue.constraint.endswith("topics.id")
        assert issue.count == 51286
        assert issue.sample_ids == ["d1:t9", "d2:t9"]
        assert issue.sample_truncated is True

    @pytest.mark.parametrize("bad_class", ["orphan", "", "ORPHAN_PARENT"])
    def test_unknown_class_rejected(self, bad_class: str) -> None:
        with pytest.raises(ValueError, match="class"):
            MigrationIssue(
                issue_class=bad_class, constraint="", reason="r",
                action="skipped", count=1,
            )

    @pytest.mark.parametrize("bad_action", ["dropped", "", "SKIPPED"])
    def test_unknown_action_rejected(self, bad_action: str) -> None:
        with pytest.raises(ValueError, match="action"):
            MigrationIssue(
                issue_class="unexpected", constraint="", reason="r",
                action=bad_action, count=1,
            )

    def test_severity_ordinals_exact(self) -> None:
        """Severity is a function of ACTION, locked to exact ordinals (the
        bead mandates ==N, never >=): failed=4, skipped=3, flagged=2,
        handled=1, schema_corrected=0."""
        assert ACTION_SEVERITY == {
            "failed": 4,
            "skipped": 3,
            "flagged": 2,
            "handled": 1,
            "schema_corrected": 0,
        }
        for action, expected in ACTION_SEVERITY.items():
            issue = MigrationIssue(
                issue_class="unexpected", constraint="", reason="r",
                action=action, count=1,
            )
            assert issue.severity == expected


# ── IssueCollector ───────────────────────────────────────────────────────────


class TestIssueCollector:
    def test_accumulates_per_store_table(self) -> None:
        c = IssueCollector()
        c.record(
            "taxonomy", "topic_assignments",
            issue_class="orphan_parent",
            constraint="topic_assignments.topic_id -> topics.id",
            reason="deleted topic",
            action="skipped",
            sample_id="d1:t9",
        )
        c.record(
            "taxonomy", "topic_assignments",
            issue_class="orphan_parent",
            constraint="topic_assignments.topic_id -> topics.id",
            reason="deleted topic",
            action="skipped",
            sample_id="d2:t9",
        )
        c.record(
            "telemetry", "hook_failures",
            issue_class="format_anomaly",
            constraint="hook_failures.occurred_at",
            reason="space-form timestamp normalized to ISO-8601",
            action="handled",
            sample_id="hf-1",
        )
        tax = c.issues_for("taxonomy", "topic_assignments")
        assert len(tax) == 1  # same (class, constraint, action) → one issue
        assert tax[0].count == 2
        assert tax[0].sample_ids == ["d1:t9", "d2:t9"]
        tel = c.issues_for("telemetry", "hook_failures")
        assert len(tel) == 1
        assert tel[0].action == "handled"
        assert c.issues_for("telemetry", "nx_answer_runs") == []

    def test_sample_ids_capped_at_200(self) -> None:
        c = IssueCollector()
        for i in range(250):
            c.record(
                "taxonomy", "topic_links",
                issue_class="orphan_parent",
                constraint="topic_links.topic_id -> topics.id",
                reason="deleted topic",
                action="skipped",
                sample_id=f"row-{i}",
            )
        (issue,) = c.issues_for("taxonomy", "topic_links")
        assert issue.count == 250            # full count, exact
        assert len(issue.sample_ids) == 200  # capped
        assert issue.sample_truncated is True
        assert issue.sample_ids[0] == "row-0"

    def test_under_cap_not_truncated(self) -> None:
        c = IssueCollector()
        c.record(
            "catalog", "links",
            issue_class="soft_dangler",
            constraint="links.from_tumbler -> documents.tumbler",
            reason="endpoint missing",
            action="flagged",
            sample_id="L1",
        )
        (issue,) = c.issues_for("catalog", "links")
        assert issue.sample_truncated is False

    def test_table_counts_tracked(self) -> None:
        c = IssueCollector()
        c.count_read("memory", "memory", 2621)
        c.count_written("memory", "memory", 2621)
        counts = c.table_counts("memory", "memory")
        assert counts == {"read": 2621, "written": 2621}


# ── Report serialization ─────────────────────────────────────────────────────


def _populated_collector() -> IssueCollector:
    c = IssueCollector()
    c.count_read("taxonomy", "topic_assignments", 180806)
    c.count_written("taxonomy", "topic_assignments", 129520)
    for i in range(3):
        c.record(
            "taxonomy", "topic_assignments",
            issue_class="orphan_parent",
            constraint="topic_assignments.topic_id -> topics.id",
            reason="topic_id references a deleted topic",
            action="skipped",
            sample_id=f"d{i}:t9",
        )
    c.count_read("telemetry", "hook_failures", 234)
    c.count_written("telemetry", "hook_failures", 234)
    c.record(
        "telemetry", "hook_failures",
        issue_class="format_anomaly",
        constraint="hook_failures.occurred_at",
        reason="space-form timestamp normalized to ISO-8601",
        action="handled",
        sample_id="hf-1",
    )
    c.record(
        "telemetry", "nx_answer_runs",
        issue_class="soft_dangler",
        constraint="nx_answer_runs.plan_id -> plans.id",
        reason="plan deleted; row imports with dangling reference",
        action="flagged",
        sample_id="run-7",
    )
    c.record(
        "taxonomy", "document_aspects",
        issue_class="unexpected",
        constraint="document_aspects",
        reason="unparseable row",
        action="failed",
        sample_id="da-3",
    )
    c.record(
        "taxonomy", "topics",
        issue_class="identity_mismatch",
        constraint="topics.doc_id",
        reason="doc_id is a chash, not a tumbler — schema corrected once",
        action="schema_corrected",
        sample_id="topics",
    )
    return c


class TestBuildReport:
    def _report(self) -> dict:
        return build_report(
            _populated_collector(),
            source={"sqlite": "/cfg/memory.db", "catalog_db": "/cfg/catalog.db"},
            target={"service_url": "http://127.0.0.1:5999",
                    "db_schema_version": "grants-002-changelog-read"},
        )

    def test_envelope_contract(self) -> None:
        report = self._report()
        assert report["schema_version"] == "1"
        uuid.UUID(report["migration_id"])  # parseable uuid
        datetime.fromisoformat(report["started_at"])
        datetime.fromisoformat(report["completed_at"])
        assert report["source"]["sqlite"] == "/cfg/memory.db"
        assert report["target"]["db_schema_version"] == "grants-002-changelog-read"

    def test_stores_and_tables_shape(self) -> None:
        report = self._report()
        stores = {s["store"]: s for s in report["stores"]}
        assert set(stores) == {"taxonomy", "telemetry"}
        tables = {t["table"]: t for t in stores["taxonomy"]["tables"]}
        ta = tables["topic_assignments"]
        assert ta["read"] == 180806
        assert ta["written"] == 129520
        assert ta["skipped"] == 3
        assert ta["flagged"] == 0
        assert ta["failed"] == 0
        (issue,) = ta["issues"]
        assert issue["class"] == "orphan_parent"
        assert issue["action"] == "skipped"
        assert issue["severity"] == 3
        assert issue["count"] == 3
        assert issue["sample_truncated"] is False

    def test_summary_by_action_keyed_on_actions(self) -> None:
        """CRITICAL contract: by_action keys are the 5 ACTION values, not
        classes — this is the gate-facing rollup."""
        report = self._report()
        summary = report["summary"]
        assert set(summary["by_action"]) == {
            "skipped", "handled", "flagged", "schema_corrected", "failed",
        }
        assert summary["by_action"]["skipped"] == 3
        assert summary["by_action"]["handled"] == 1
        assert summary["by_action"]["flagged"] == 1
        assert summary["by_action"]["failed"] == 1
        assert summary["by_action"]["schema_corrected"] == 1
        assert summary["total_read"] == 180806 + 234
        assert summary["total_written"] == 129520 + 234
        assert summary["total_skipped"] == 3
        assert summary["total_flagged"] == 1
        assert summary["total_failed"] == 1
        assert summary["max_severity"] == 4  # the failed issue

    def test_phase4_gate_predicate(self) -> None:
        # The Phase-4 gate predicate is summary.total_failed == 0.
        clean = IssueCollector()
        clean.count_read("memory", "memory", 10)
        clean.count_written("memory", "memory", 10)
        report = build_report(clean, source={}, target={})
        assert report["summary"]["total_failed"] == 0
        assert report["summary"]["max_severity"] == 0

    def test_round_trip_stable(self) -> None:
        report = self._report()
        encoded = json.dumps(report, sort_keys=True)
        decoded = json.loads(encoded)
        assert decoded == report
        assert json.dumps(decoded, sort_keys=True) == encoded


class TestReviewPassRegressions:
    """P1.3/P1.4 review findings (2026-06-11)."""

    def test_phase4_gate_clears_with_nonzero_skipped_and_handled(self) -> None:
        """Critic S1: the REAL passing-gate shape — the production audit
        expects ~51k skipped + 273 flagged + 234 handled + 1 schema
        correction and ZERO failed. The gate must clear on exactly that."""
        c = IssueCollector()
        c.count_read("taxonomy", "topic_assignments", 180806)
        c.count_written("taxonomy", "topic_assignments", 129520)
        for i in range(3):
            c.record(
                "taxonomy", "topic_assignments",
                issue_class="orphan_parent",
                constraint="topic_assignments.topic_id -> topics.id",
                reason="deleted topic",
                action="skipped",
                sample_id=f"d{i}:t9",
            )
        c.record(
            "catalog", "links",
            issue_class="soft_dangler",
            constraint="links.from_tumbler -> documents.tumbler",
            reason="endpoint missing",
            action="flagged",
            sample_id="L1",
        )
        c.record(
            "telemetry", "hook_failures",
            issue_class="format_anomaly",
            constraint="hook_failures.occurred_at",
            reason="space-form timestamp normalized",
            action="handled",
            sample_id="hf-1",
        )
        c.record_event(
            "taxonomy", "topics",
            issue_class="identity_mismatch",
            constraint="topics.doc_id",
            reason="doc_id is a chash — schema corrected",
            action="schema_corrected",
        )
        report = build_report(c, source={}, target={})
        summary = report["summary"]
        assert summary["total_failed"] == 0      # the gate clears
        assert summary["total_skipped"] == 3
        assert summary["total_flagged"] == 1
        assert summary["by_action"]["handled"] == 1
        assert summary["by_action"]["schema_corrected"] == 1
        assert summary["max_severity"] == 3      # skipped, nothing failed

    def test_started_at_defaults_to_collector_construction(self) -> None:
        """Critic S2: a forgotten started_at must not silently equal
        completed_at — the collector captures it at construction (= ETL
        start), so the audit artifact keeps the run duration."""
        c = IssueCollector()
        c.count_read("memory", "memory", 1)
        report = build_report(c, source={}, target={})
        assert report["started_at"] == c.started_at
        assert report["completed_at"] >= report["started_at"]
        # Explicit override still wins.
        explicit = build_report(
            c, source={}, target={}, started_at="2026-06-10T00:00:00+00:00",
        )
        assert explicit["started_at"] == "2026-06-10T00:00:00+00:00"

    def test_multiple_issues_same_table(self) -> None:
        """CRE M2: two distinct (class, constraint, action) buckets in ONE
        (store, table) — the most likely production shape."""
        c = IssueCollector()
        c.count_read("taxonomy", "topic_links", 17638)
        c.count_written("taxonomy", "topic_links", 6535)
        c.record(
            "taxonomy", "topic_links",
            issue_class="orphan_parent",
            constraint="topic_links.topic_id -> topics.id",
            reason="deleted from-topic",
            action="skipped",
            sample_id="tl-1",
        )
        c.record(
            "taxonomy", "topic_links",
            issue_class="orphan_parent",
            constraint="topic_links.to_topic_id -> topics.id",
            reason="deleted to-topic",
            action="skipped",
            sample_id="tl-2",
        )
        c.record(
            "taxonomy", "topic_links",
            issue_class="unexpected",
            constraint="topic_links",
            reason="unparseable row",
            action="failed",
            sample_id="tl-3",
        )
        report = build_report(c, source={}, target={})
        (store,) = report["stores"]
        (table,) = store["tables"]
        assert len(table["issues"]) == 3
        assert table["skipped"] == 2   # summed across the two skip buckets
        assert table["failed"] == 1
        assert report["summary"]["by_action"]["skipped"] == 2
        assert report["summary"]["total_failed"] == 1

    def test_table_key_set_locked(self) -> None:
        """CRE L2: the table dict carries EXACTLY the RDR schema columns —
        handled/schema_corrected are by_action-only by design."""
        c = IssueCollector()
        c.count_read("memory", "memory", 1)
        report = build_report(c, source={}, target={})
        (table,) = report["stores"][0]["tables"]
        assert set(table.keys()) == {
            "table", "read", "written", "skipped", "flagged", "failed", "issues",
        }

    def test_by_action_severity_descending_order(self) -> None:
        """CRE L1: key order matches the RDR example (human readability;
        JSON object order is non-semantic)."""
        c = IssueCollector()
        c.count_read("memory", "memory", 1)
        report = build_report(c, source={}, target={})
        assert list(report["summary"]["by_action"]) == [
            "failed", "skipped", "flagged", "handled", "schema_corrected",
        ]

    def test_record_event_has_no_samples(self) -> None:
        c = IssueCollector()
        c.record_event(
            "taxonomy", "topics",
            issue_class="identity_mismatch",
            constraint="topics.doc_id",
            reason="schema corrected once",
            action="schema_corrected",
        )
        (issue,) = c.issues_for("taxonomy", "topics")
        assert issue.count == 1
        assert issue.sample_ids == []
        assert issue.sample_truncated is False


class TestEtlRegistry:
    """RDR-153 P2 critique S5: the Phase-3 seam — ladder order + the
    uniform runner contract live in ONE reviewable place."""

    def test_ladder_order_exact(self) -> None:
        from nexus.migration.etl_registry import LADDER_ORDER

        assert LADDER_ORDER == (
            "memory", "plans", "telemetry", "taxonomy",
            "aspects", "chash", "catalog",
        )

    def test_unknown_store_rejected(self) -> None:
        from nexus.migration.etl_registry import StoreEtl

        with pytest.raises(ValueError, match="unknown store"):
            StoreEtl(store="vectors", run=lambda sources, collector: {})

    def test_ordered_sorts_and_rejects_duplicates(self) -> None:
        from nexus.migration.etl_registry import StoreEtl, ordered

        runner = lambda sources, collector: {}  # noqa: E731
        etls = [
            StoreEtl("catalog", runner),
            StoreEtl("memory", runner),
            StoreEtl("taxonomy", runner),
        ]
        assert [e.store for e in ordered(etls)] == ["memory", "taxonomy", "catalog"]
        with pytest.raises(ValueError, match="duplicate"):
            ordered([StoreEtl("memory", runner), StoreEtl("memory", runner)])
