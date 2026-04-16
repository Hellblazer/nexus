# SPDX-License-Identifier: AGPL-3.0-or-later
"""Operator cost-budget tracking — RDR-080 P5.

Reads cumulative daily spend from the ``nx_answer_runs`` T2 table and
enforces the ``operators.daily_budget_usd`` limit from ``.nexus.yml``.

Thresholds:
  * **80%** — WARNING logged, dispatch proceeds.
  * **100%** — dispatch REFUSED with a clear error.
  * **No budget configured** — pass-through, no tracking.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class BudgetCheckResult:
    """Result of a budget check."""

    allowed: bool
    warning: bool
    spend: float = 0.0
    budget: float | None = None
    message: str = ""


def today_spend_usd(conn: sqlite3.Connection) -> float:
    """Sum ``cost_usd`` from ``nx_answer_runs`` for today (UTC)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0.0) FROM nx_answer_runs "
        "WHERE date(created_at) = ?",
        (today,),
    ).fetchone()
    return float(row[0]) if row else 0.0


def check_budget(
    *,
    daily_budget_usd: float | None,
    today_spend: float,
) -> BudgetCheckResult:
    """Check whether a dispatch should proceed given the daily budget."""
    if daily_budget_usd is None:
        return BudgetCheckResult(allowed=True, warning=False)

    if daily_budget_usd <= 0:
        return BudgetCheckResult(allowed=True, warning=False)

    ratio = today_spend / daily_budget_usd

    if ratio >= 1.0:
        return BudgetCheckResult(
            allowed=False,
            warning=True,
            spend=today_spend,
            budget=daily_budget_usd,
            message=(
                f"Daily operator budget exhausted: "
                f"${today_spend:.2f} / ${daily_budget_usd:.2f} "
                f"({ratio:.0%}). Reset at midnight UTC."
            ),
        )

    if ratio >= 0.8:
        return BudgetCheckResult(
            allowed=True,
            warning=True,
            spend=today_spend,
            budget=daily_budget_usd,
            message=(
                f"Daily operator budget at {ratio:.0%}: "
                f"${today_spend:.2f} / ${daily_budget_usd:.2f}"
            ),
        )

    return BudgetCheckResult(
        allowed=True,
        warning=False,
        spend=today_spend,
        budget=daily_budget_usd,
    )
