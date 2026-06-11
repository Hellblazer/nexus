# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-153: structured issue reporting for the SQLite→Postgres T2 migration.

The migration runs with strict foreign keys DELIBERATELY — the constraints
are the diagnostic. This module is the shared issue-record primitive every
``migrate_*`` ETL writes to (replacing ad-hoc per-row error logging) and
the serializer for the one structured ``migration-report.json`` each run
emits — the triage / recovery / learning artifact and the Phase-4 gate
input (``summary.total_failed == 0``).

Lives in ``src/nexus/migration/`` and NOT under ``src/nexus/db/t2/``:
RDR-152 Phase 4 deletes that subtree, but this primitive and the
``migration-report show`` reader must survive the SQLite decommission
(the Phase-4 gate itself reads a report).

Contract (docs/rdr/rdr-153-migration-data-quality-policy.md): two distinct
enums, never mixed — each issue has a ``class`` (what is wrong) and an
``action`` (what the ETL did). ``summary.by_action`` aggregates by the
five ACTION values; that is the gate-facing rollup.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog

_log = structlog.get_logger(__name__)

__all__ = [
    "ISSUE_CLASSES",
    "ISSUE_ACTIONS",
    "ACTION_SEVERITY",
    "SAMPLE_IDS_CAP",
    "MigrationIssue",
    "IssueCollector",
    "build_report",
]

#: What is WRONG with the row (diagnosis).
ISSUE_CLASSES: frozenset[str] = frozenset({
    "orphan_parent",
    "identity_mismatch",
    "format_anomaly",
    "soft_dangler",
    "unexpected",
})

#: What the ETL DID about it (policy outcome).
ISSUE_ACTIONS: frozenset[str] = frozenset({
    "skipped",
    "handled",
    "flagged",
    "schema_corrected",
    "failed",
})

#: Severity is a function of ACTION (the policy outcome ranks the run, not
#: the diagnosis). Exact ordinals locked by the P1.1 suite:
#: failed=4 (gate-blocking), skipped=3 (data not migrated),
#: flagged=2 (imported with advisory), handled=1 (normalized),
#: schema_corrected=0 (schema fixed; data correct).
ACTION_SEVERITY: dict[str, int] = {
    "failed": 4,
    "skipped": 3,
    "flagged": 2,
    "handled": 1,
    "schema_corrected": 0,
}

#: sample_ids cap per issue; the full set is reproducible by re-running.
SAMPLE_IDS_CAP: int = 200


@dataclass
class MigrationIssue:
    """One issue line: a (class, constraint, action) bucket with counts.

    ``issue_class`` (not ``class`` — Python keyword) ∈ :data:`ISSUE_CLASSES`;
    ``action`` ∈ :data:`ISSUE_ACTIONS`; ``severity`` derives from the action
    via :data:`ACTION_SEVERITY` and is not caller-settable.

    Composite-key ``sample_ids`` are the key tuple joined with ``:`` (e.g.
    ``topic_assignments`` → ``"<doc_id>:<topic_id>"``); record the
    convention per table in ``reason``.
    """

    issue_class: str
    constraint: str
    reason: str
    action: str
    count: int = 0
    sample_ids: list[str] = field(default_factory=list)
    sample_truncated: bool = False

    def __post_init__(self) -> None:
        if self.issue_class not in ISSUE_CLASSES:
            raise ValueError(
                f"unknown issue class {self.issue_class!r} — "
                f"must be one of {sorted(ISSUE_CLASSES)}"
            )
        if self.action not in ISSUE_ACTIONS:
            raise ValueError(
                f"unknown issue action {self.action!r} — "
                f"must be one of {sorted(ISSUE_ACTIONS)}"
            )

    @property
    def severity(self) -> int:
        return ACTION_SEVERITY[self.action]

    def add_sample(self, sample_id: str) -> None:
        """Count one more occurrence; keep at most :data:`SAMPLE_IDS_CAP` ids."""
        self.count += 1
        if len(self.sample_ids) < SAMPLE_IDS_CAP:
            self.sample_ids.append(sample_id)
        else:
            self.sample_truncated = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "class": self.issue_class,
            "constraint": self.constraint,
            "reason": self.reason,
            "action": self.action,
            "severity": self.severity,
            "count": self.count,
            "sample_ids": list(self.sample_ids),
            "sample_truncated": self.sample_truncated,
        }


class IssueCollector:
    """In-run accumulator every ETL writes to.

    Issues bucket per ``(store, table)`` on the ``(class, constraint,
    action)`` identity — recording the same anomaly for many rows grows
    one issue's count instead of emitting thousands of lines. Read/written
    row counts are tracked per table alongside.
    """

    def __init__(self) -> None:
        # (store, table) -> (issue_class, constraint, action) -> MigrationIssue
        self._issues: dict[tuple[str, str], dict[tuple[str, str, str], MigrationIssue]] = {}
        # (store, table) -> {"read": int, "written": int}
        self._counts: dict[tuple[str, str], dict[str, int]] = {}

    # ── Recording ────────────────────────────────────────────────────────────

    def record(
        self,
        store: str,
        table: str,
        *,
        issue_class: str,
        constraint: str,
        reason: str,
        action: str,
        sample_id: str,
    ) -> None:
        """Record one offending row into its (class, constraint, action) bucket."""
        bucket = self._issues.setdefault((store, table), {})
        key = (issue_class, constraint, action)
        issue = bucket.get(key)
        if issue is None:
            # Validation happens in MigrationIssue.__post_init__ — fail loud
            # at the first record, not at serialization time.
            issue = MigrationIssue(
                issue_class=issue_class,
                constraint=constraint,
                reason=reason,
                action=action,
            )
            bucket[key] = issue
        issue.add_sample(sample_id)

    def count_read(self, store: str, table: str, n: int) -> None:
        self._table(store, table)["read"] += n

    def count_written(self, store: str, table: str, n: int) -> None:
        self._table(store, table)["written"] += n

    # ── Reading ──────────────────────────────────────────────────────────────

    def issues_for(self, store: str, table: str) -> list[MigrationIssue]:
        return list(self._issues.get((store, table), {}).values())

    def table_counts(self, store: str, table: str) -> dict[str, int]:
        return dict(self._table(store, table))

    def store_names(self) -> list[str]:
        names = {s for s, _ in self._issues} | {s for s, _ in self._counts}
        return sorted(names)

    def tables_for(self, store: str) -> list[str]:
        tables = {t for s, t in self._issues if s == store}
        tables |= {t for s, t in self._counts if s == store}
        return sorted(tables)

    # ── Internals ────────────────────────────────────────────────────────────

    def _table(self, store: str, table: str) -> dict[str, int]:
        return self._counts.setdefault((store, table), {"read": 0, "written": 0})


def build_report(
    collector: IssueCollector,
    *,
    source: dict[str, str],
    target: dict[str, str],
    migration_id: str | None = None,
    started_at: str | None = None,
) -> dict[str, Any]:
    """Serialize one migration run into the schema_version=1 report dict.

    Self-describing and stable (``schema_version``) so downstream triage
    tooling evolves independently; JSON-round-trip safe (plain dict/list/
    str/int/bool values only).
    """
    now = datetime.now(UTC).isoformat()
    stores_out: list[dict[str, Any]] = []
    by_action: dict[str, int] = {a: 0 for a in sorted(ISSUE_ACTIONS)}
    totals = {"read": 0, "written": 0, "skipped": 0, "flagged": 0, "failed": 0}
    max_severity = 0

    for store in collector.store_names():
        tables_out: list[dict[str, Any]] = []
        for table in collector.tables_for(store):
            counts = collector.table_counts(store, table)
            issues = collector.issues_for(store, table)
            per_action = {a: 0 for a in ISSUE_ACTIONS}
            for issue in issues:
                per_action[issue.action] += issue.count
                by_action[issue.action] += issue.count
                max_severity = max(max_severity, issue.severity)
            tables_out.append({
                "table": table,
                "read": counts["read"],
                "written": counts["written"],
                "skipped": per_action["skipped"],
                "flagged": per_action["flagged"],
                "failed": per_action["failed"],
                "issues": [i.to_dict() for i in issues],
            })
            totals["read"] += counts["read"]
            totals["written"] += counts["written"]
            totals["skipped"] += per_action["skipped"]
            totals["flagged"] += per_action["flagged"]
            totals["failed"] += per_action["failed"]
        stores_out.append({"store": store, "tables": tables_out})

    report = {
        "schema_version": "1",
        "migration_id": migration_id or str(uuid.uuid4()),
        "started_at": started_at or now,
        "completed_at": now,
        "source": dict(source),
        "target": dict(target),
        "stores": stores_out,
        "summary": {
            "total_read": totals["read"],
            "total_written": totals["written"],
            "total_skipped": totals["skipped"],
            "total_flagged": totals["flagged"],
            "total_failed": totals["failed"],
            "max_severity": max_severity,
            "by_action": by_action,
        },
    }
    _log.info(
        "migration_report_built",
        migration_id=report["migration_id"],
        total_failed=totals["failed"],
        max_severity=max_severity,
    )
    return report
