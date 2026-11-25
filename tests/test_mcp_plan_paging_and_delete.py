# SPDX-License-Identifier: AGPL-3.0-or-later
"""plan_search pages for real, and plan_delete's missing id is an error result (MCP audit I3, I15).

The store is a stub: the engine's plan search takes a limit and no offset, so the tool has to ask for
offset + limit + 1 rows and slice. No service is reached.
"""
from __future__ import annotations

import contextlib
from typing import Any

import pytest

from nexus.mcp import core
from nexus.mcp._tool_failure import is_failure


class _Plans:
    def __init__(self, n: int) -> None:
        self.rows = {i: self._row(i) for i in range(1, n + 1)}
        self.deleted: list[int] = []

    @staticmethod
    def _row(i: int) -> dict[str, Any]:
        return {"id": i, "query": f"plan {i}", "outcome": "success", "tags": "t", "scope_tags": "", "plan_json": "{}"}

    def get_plan(self, plan_id: int) -> dict[str, Any] | None:
        return self.rows.get(plan_id)

    def delete_plan(self, plan_id: int) -> bool:
        self.deleted.append(plan_id)
        return self.rows.pop(plan_id, None) is not None


class _Db:
    def __init__(self, n: int) -> None:
        self.plans = _Plans(n)
        self.asked: list[int] = []

    def search_plans(self, query: str, limit: int = 5, project: str = "", *, any_lexeme: bool = False) -> list[dict[str, Any]]:
        self.asked.append(limit)  # the engine route has a limit and no offset
        return [self.plans._row(i) for i in sorted(self.plans.rows)][:limit]


@pytest.fixture()
def db(monkeypatch: pytest.MonkeyPatch) -> _Db:
    stub = _Db(5)
    monkeypatch.setattr(core, "_t2_ctx", lambda: contextlib.nullcontext(stub))
    return stub


def _ids(out: str) -> list[int]:
    return [int(line[1 : line.index("]")]) for line in out.splitlines() if line.startswith("[")]


def test_page_one_names_the_next_offset(db: _Db) -> None:
    out = core.plan_search(query="plan", limit=2)
    assert _ids(out) == [1, 2]
    assert "offset=2" in out


def test_page_two_returns_a_full_page_and_the_next_offset(db: _Db) -> None:
    out = core.plan_search(query="plan", limit=2, offset=2)
    assert _ids(out) == [3, 4]
    assert "offset=4" in out


def test_the_last_page_has_no_next_offset(db: _Db) -> None:
    out = core.plan_search(query="plan", limit=2, offset=4)
    assert _ids(out) == [5]
    assert "offset=" not in out


def test_an_offset_larger_than_the_limit_still_pages(db: _Db) -> None:
    out = core.plan_search(query="plan", limit=1, offset=3)
    assert _ids(out) == [4]
    assert "offset=4" in out


def test_an_offset_past_the_end_says_no_matches(db: _Db) -> None:
    assert core.plan_search(query="plan", limit=2, offset=9) == "No matching plans."


def test_the_store_is_asked_for_offset_plus_limit_plus_one(db: _Db) -> None:
    core.plan_search(query="plan", limit=2, offset=3)
    assert db.asked == [6]


def test_plan_delete_of_a_missing_id_is_an_error_result(db: _Db) -> None:
    out = core.plan_delete(plan_id=999)
    assert "Not found: plan id 999" in out
    assert is_failure(out)


def test_plan_delete_of_a_lost_race_is_an_error_result(db: _Db, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db.plans, "delete_plan", lambda plan_id: False)
    out = core.plan_delete(plan_id=1)
    assert "already deleted" in out
    assert is_failure(out)


def test_plan_delete_of_a_present_id_succeeds(db: _Db) -> None:
    out = core.plan_delete(plan_id=2)
    assert out.startswith("Deleted plan id=2")
    assert not is_failure(out)
