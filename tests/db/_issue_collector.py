# SPDX-License-Identifier: AGPL-3.0-or-later
"""Test-local issue-collector fixture (RDR-155 P4b).

Faithful copy of the retired ``nexus.migration.migration_report``
``MigrationIssue`` / ``IssueCollector`` (deleted with the migration
machinery at P4b). The surviving T2 ETL modules
(``nexus.db.t2.{catalog,taxonomy,telemetry}_etl``) take ``collector`` as
a duck-typed ``Any``; their tests still need a recording implementation
to assert skip/flag/fail policy, so the fixture lives here now.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

__all__ = ["MigrationIssue", "IssueCollector"]

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
        #: Construction time = ETL start. build_report uses this as the
        #: report's started_at so no caller has to remember to capture it
        #: (a forgotten started_at would silently equal completed_at and
        #: erase the run duration from the audit artifact).
        self.started_at: str = datetime.now(UTC).isoformat()

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
        """Record one offending row into its (class, constraint, action) bucket.

        ``reason`` is taken from the FIRST call for each bucket; subsequent
        reasons are not recorded (rows sharing a bucket share a diagnosis —
        51,286 orphans carry one reason, not 51,286).

        Not thread-safe: all ``record()``/``count_*()`` calls must come from
        a single thread (the ETLs run sequentially by design).
        """
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

    def record_event(
        self,
        store: str,
        table: str,
        *,
        issue_class: str,
        constraint: str,
        reason: str,
        action: str,
    ) -> None:
        """Record a ONE-TIME event with no natural row sample (e.g. a
        ``schema_corrected`` decision). ``count`` increments per call;
        ``sample_ids`` stays empty — synthesizing a fake row id for a
        non-row event would be semantically wrong (critic P1.4 finding)."""
        bucket = self._issues.setdefault((store, table), {})
        key = (issue_class, constraint, action)
        issue = bucket.get(key)
        if issue is None:
            issue = MigrationIssue(
                issue_class=issue_class,
                constraint=constraint,
                reason=reason,
                action=action,
            )
            bucket[key] = issue
        issue.count += 1

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
