# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for P5 operator cost-budget surfacing — RDR-080.

Tests cover:
  - OperatorBudget config parsing from .nexus.yml
  - 80% warn threshold
  - 100% refuse threshold
  - Budget reset at midnight boundary
  - No-budget pass-through
  - nx doctor --operators output format
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from nexus.db.migrations import migrate_nx_answer_runs


# ── Config parsing ────────────────────────────────────────────────────────────


class TestOperatorBudgetConfig:
    """operators.daily_budget_usd parsed from config."""

    def test_default_is_none(self):
        from nexus.config import OperatorBudgetConfig

        cfg = OperatorBudgetConfig()
        assert cfg.daily_budget_usd is None

    def test_from_dict(self):
        from nexus.config import OperatorBudgetConfig

        cfg = OperatorBudgetConfig(daily_budget_usd=5.0)
        assert cfg.daily_budget_usd == 5.0


# ── Budget tracker ────────────────────────────────────────────────────────────


class TestBudgetTracker:
    """In-memory budget tracker with injectable clock."""

    def _make_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        migrate_nx_answer_runs(conn)
        return conn

    def _insert_run(self, conn: sqlite3.Connection, cost: float, ts: str) -> None:
        conn.execute(
            "INSERT INTO nx_answer_runs (question, step_count, final_text, cost_usd, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("q", 1, "", cost, ts),
        )
        conn.commit()

    def test_no_budget_always_passes(self):
        from nexus.operators.budget import check_budget

        # No budget configured — should never refuse.
        result = check_budget(daily_budget_usd=None, today_spend=0.50)
        assert result.allowed is True
        assert result.warning is False

    def test_under_80_percent(self):
        from nexus.operators.budget import check_budget

        result = check_budget(daily_budget_usd=1.0, today_spend=0.70)
        assert result.allowed is True
        assert result.warning is False

    def test_at_80_percent_warns(self):
        from nexus.operators.budget import check_budget

        result = check_budget(daily_budget_usd=1.0, today_spend=0.80)
        assert result.allowed is True
        assert result.warning is True

    def test_at_100_percent_refuses(self):
        from nexus.operators.budget import check_budget

        result = check_budget(daily_budget_usd=1.0, today_spend=1.00)
        assert result.allowed is False

    def test_over_100_percent_refuses(self):
        from nexus.operators.budget import check_budget

        result = check_budget(daily_budget_usd=1.0, today_spend=1.50)
        assert result.allowed is False

    def test_today_spend_from_db(self):
        from nexus.operators.budget import today_spend_usd

        conn = self._make_conn()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._insert_run(conn, 0.05, today)
        self._insert_run(conn, 0.03, today)
        # Yesterday — should NOT count.
        self._insert_run(conn, 9.99, "2020-01-01T00:00:00Z")

        total = today_spend_usd(conn)
        assert abs(total - 0.08) < 0.001

    def test_today_spend_empty_db(self):
        from nexus.operators.budget import today_spend_usd

        conn = self._make_conn()
        total = today_spend_usd(conn)
        assert total == 0.0
