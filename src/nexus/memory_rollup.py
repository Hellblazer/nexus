# SPDX-License-Identifier: AGPL-3.0-or-later
"""The attended rollup producer behind ``nx memory rollup`` (RDR-207 Phase 3).

Expiry quarantines a T2 row instead of deleting it, and ``nx memory reap``
deletes only a quarantined row that carries a rollup mark. This module makes
the marks: it groups one project's unmarked quarantined rows by month, asks
the summarize operator for one summary per group, checks each summary against
its sources, and sends the ones that pass to ``POST /v1/memory/summaries``,
which stores the summary and marks its sources in one engine transaction.

Every group stands alone. A group whose summarizer call fails, whose summary
fails the title check, or whose summary the engine refuses is recorded in its
outcome and left unmarked, and the remaining groups still run (RDR-207 failure
modes 5 and 6). Nothing here runs unattended: the caller is a command someone
types, never a session-end hook or a schedule.

The title check is assumption A4 of the RDR, a floor and not a fidelity proof:
every source title must appear in the summary text. The backstops are the
separate reap step and ``nx memory restore``.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx
import structlog

_log = structlog.get_logger(__name__)

#: ``produced_by`` stamped on every summary row this module writes.
PRODUCED_BY: str = "nx memory rollup"

#: The ``model`` recorded on a summary when dispatch runs at the CLI default
#: (operator model tiering switched off with ``NX_OPERATOR_MODEL_TIERING=0``);
#: the engine refuses a blank model.
DEFAULT_MODEL_LABEL: str = "claude-cli-default"


class RollupStore(Protocol):
    """The two memory-store calls a rollup needs (``HttpMemoryStore``)."""

    def list_quarantined(self, project: str | None = None) -> list[dict[str, Any]]: ...

    def insert_summary(
        self,
        project: str,
        content: str,
        source_ids: list[int],
        model: str,
        produced_by: str | None = None,
    ) -> int: ...


@dataclass(frozen=True)
class RollupGroup:
    """Unmarked quarantined rows of one project that share a month.

    ``month`` is ``YYYY-MM`` of each row's ``timestamp``, which is the row's
    LAST WRITE: a put or a merge refreshes it, and the schema keeps no
    creation date.
    """

    month: str
    rows: tuple[dict[str, Any], ...]

    @property
    def source_ids(self) -> list[int]:
        return [int(row["id"]) for row in self.rows]

    @property
    def titles(self) -> list[str]:
        return [str(row["title"]) for row in self.rows]


#: A summarizer returns one summary text for a group, or raises.
Summarizer = Callable[[RollupGroup], str]

Status = Literal["marked", "dry_run", "check_failed", "dispatch_failed", "source_changed", "refused"]


@dataclass(frozen=True)
class GroupOutcome:
    """What happened to one group. Only ``marked`` wrote anything."""

    group: RollupGroup
    status: Status
    summary: str | None = None
    summary_id: int | None = None
    missing_titles: tuple[str, ...] = ()
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("marked", "dry_run")


def plan_groups(rows: Sequence[dict[str, Any]]) -> list[RollupGroup]:
    """Group the unmarked rows of a quarantined listing by month, oldest first.

    The quarantined route has no unmarked filter, so rows that already carry
    ``rolled_up_at`` are dropped here (plan record A3).
    """
    by_month: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("rolled_up_at"):
            continue
        by_month.setdefault(str(row["timestamp"])[:7], []).append(row)
    return [
        RollupGroup(month=month, rows=tuple(sorted(group, key=lambda r: int(r["id"]))))
        for month, group in sorted(by_month.items())
    ]


def missing_titles(summary: str, group: RollupGroup) -> list[str]:
    """The A4 floor: the titles of ``group`` that ``summary`` does not contain."""
    return [title for title in group.titles if title not in summary]


def group_prompt_content(group: RollupGroup) -> str:
    """The content handed to the summarize operator for one group."""
    parts = [
        "Write one summary of the memory entries below. Name every entry by "
        "its exact title: a summary that leaves out a title is refused.",
    ]
    for row in group.rows:
        parts.append(f"## {row['title']}\n{row['content']}")
    return "\n\n".join(parts)


def rollup_model() -> str | None:
    """The model a rollup dispatches at: exactly what ``operator_summarize``
    pins for a call with no model (``nexus.mcp.core._pin_default_model``);
    ``None`` means the CLI default."""
    from nexus.mcp.core import _pin_default_model  # noqa: PLC0415 — deferred: heavy import, keep CLI startup fast; the one pin rule, not a copy (only two modules may import operators.model_tiers)

    return _pin_default_model(None)


def dispatch_summarizer(*, model: str | None = None, timeout: float = 300.0) -> Summarizer:
    """The real summarizer: one ``claude -p`` call per group, through the same
    prompt builder and dispatch ``operator_summarize`` uses (RDR-207
    §Existing Infrastructure Audit: reuse the dispatch, build no new one)."""

    def _summarize(group: RollupGroup) -> str:
        from nexus.mcp.operator_requests import build_summarize_request  # noqa: PLC0415 — deferred: operator deps stay off CLI startup
        from nexus.operators.dispatch import claude_dispatch  # noqa: PLC0415 — deferred: operator deps stay off CLI startup

        prompt, schema = build_summarize_request(group_prompt_content(group))
        result = asyncio.run(claude_dispatch(
            prompt, schema, timeout=timeout, model=model, operator="operator_summarize",
        ))
        return str(result.get("summary", ""))

    return _summarize


def run_groups(
    store: RollupStore,
    project: str,
    groups: Sequence[RollupGroup],
    summarize: Summarizer,
    *,
    model: str,
    dry_run: bool = False,
) -> list[GroupOutcome]:
    """Summarize, check and mark each group in turn; one outcome per group.

    Each group is its own engine transaction (``insert_summary``), so a
    failure in one group never takes another with it or half-commits itself.
    With ``dry_run`` the summarizer and the check still run, and nothing is
    written.
    """
    outcomes: list[GroupOutcome] = []
    for group in groups:
        try:
            summary = summarize(group)
        except Exception as exc:  # noqa: BLE001 — recorded in this group's outcome and logged; the caller reports it and exits nonzero, and the remaining groups still run (RDR-207 failure modes 5 and 6)
            _log.warning(
                "memory_rollup_dispatch_failed", project=project, month=group.month,
                source_ids=group.source_ids, error=str(exc),
            )
            outcomes.append(GroupOutcome(group, "dispatch_failed", error=f"{type(exc).__name__}: {exc}"))
            continue

        missing = missing_titles(summary, group)
        if missing:
            _log.warning(
                "memory_rollup_check_failed", project=project, month=group.month,
                source_ids=group.source_ids, missing_titles=missing,
            )
            outcomes.append(GroupOutcome(
                group, "check_failed", summary=summary, missing_titles=tuple(missing),
            ))
            continue

        if dry_run:
            outcomes.append(GroupOutcome(group, "dry_run", summary=summary))
            continue

        # The summarizer call can take minutes. A source restored or re-put
        # meanwhile is live again, and insertSummary would still mark it (plan
        # residual 1, admitted in the engine), leaving a mark no summary of
        # its current content backs. Re-read the listing right before marking
        # so this command does not widen that window.
        try:
            still = {int(r["id"]) for r in store.list_quarantined(project=project)}
        except httpx.HTTPError as exc:
            _log.warning(
                "memory_rollup_recheck_failed", project=project, month=group.month,
                source_ids=group.source_ids, error=str(exc),
            )
            outcomes.append(GroupOutcome(
                group, "refused", summary=summary, error=f"{type(exc).__name__}: {exc}",
            ))
            continue
        changed = [i for i in group.source_ids if i not in still]
        if changed:
            _log.warning(
                "memory_rollup_source_changed", project=project, month=group.month,
                source_ids=group.source_ids, changed_ids=changed,
            )
            outcomes.append(GroupOutcome(
                group, "source_changed", summary=summary,
                error=f"no longer quarantined: {', '.join(str(i) for i in changed)}",
            ))
            continue

        try:
            summary_id = store.insert_summary(
                project, summary, group.source_ids, model, produced_by=PRODUCED_BY,
            )
        except httpx.HTTPError as exc:
            _log.warning(
                "memory_rollup_insert_refused", project=project, month=group.month,
                source_ids=group.source_ids, error=str(exc),
            )
            outcomes.append(GroupOutcome(
                group, "refused", summary=summary, error=f"{type(exc).__name__}: {exc}",
            ))
            continue
        outcomes.append(GroupOutcome(group, "marked", summary=summary, summary_id=summary_id))
    return outcomes


def rollup(
    store: RollupStore,
    project: str,
    summarize: Summarizer,
    *,
    model: str,
    dry_run: bool = False,
) -> list[GroupOutcome]:
    """:func:`plan_groups` over the project's quarantined listing, then
    :func:`run_groups`."""
    groups = plan_groups(store.list_quarantined(project=project))
    return run_groups(store, project, groups, summarize, model=model, dry_run=dry_run)
