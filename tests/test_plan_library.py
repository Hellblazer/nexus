# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for plan library (save_plan, search_plans, list_plans) in T2Database."""
from pathlib import Path

import pytest

from nexus.db.t2 import T2Database


@pytest.fixture
def plan_db(tmp_path: Path) -> T2Database:
    """Fresh T2Database for plan library tests."""
    database = T2Database(tmp_path / "plans.db")
    yield database
    database.close()


def test_save_plan(plan_db: T2Database) -> None:
    """save_plan() inserts a row and returns an integer row ID."""
    row_id = plan_db.save_plan(
        query="how to index code",
        plan_json='{"steps": ["classify", "chunk", "embed"]}',
    )
    assert isinstance(row_id, int)
    assert row_id > 0

    row = plan_db.plans.conn.execute("SELECT query, outcome, tags FROM plans WHERE id = ?", (row_id,)).fetchone()
    assert row is not None
    assert row[0] == "how to index code"
    assert row[1] == "success"
    assert row[2] == ""


def test_save_plan_json_stored(plan_db: T2Database) -> None:
    """save_plan() stores plan_json verbatim and it is retrievable as-is."""
    json_payload = '{"steps": ["step1", "step2"], "meta": {"version": 2}}'
    row_id = plan_db.save_plan(query="complex query", plan_json=json_payload)

    row = plan_db.plans.conn.execute("SELECT plan_json FROM plans WHERE id = ?", (row_id,)).fetchone()
    assert row is not None
    assert row[0] == json_payload


def test_search_plans_match(plan_db: T2Database) -> None:
    """search_plans() returns plans whose query text matches the FTS5 query."""
    plan_db.save_plan(query="semantic search over code repositories", plan_json='{"steps":[]}')
    plan_db.save_plan(query="memory management in Python", plan_json='{"steps":[]}')

    results = plan_db.search_plans("semantic")
    assert len(results) == 1
    assert "semantic" in results[0]["query"]


def test_search_plans_tags(plan_db: T2Database) -> None:
    """search_plans() matches on the tags field via FTS5."""
    plan_db.save_plan(query="generic query", plan_json='{}', tags="indexing,code")
    plan_db.save_plan(query="another query", plan_json='{}', tags="memory,retrieval")

    results = plan_db.search_plans("indexing")
    assert len(results) == 1
    assert results[0]["tags"] == "indexing,code"


def test_search_plans_no_match(plan_db: T2Database) -> None:
    """search_plans() returns an empty list when no plans match the query."""
    plan_db.save_plan(query="index repository", plan_json='{}')

    results = plan_db.search_plans("xyzzy_nonexistent_term")
    assert results == []


def test_list_plans_ordered(plan_db: T2Database) -> None:
    """list_plans() returns plans ordered by created_at DESC (most recent first)."""
    plan_db.save_plan(query="first plan", plan_json='{}')
    plan_db.save_plan(query="second plan", plan_json='{}')
    plan_db.save_plan(query="third plan", plan_json='{}')

    results = plan_db.list_plans()
    assert len(results) == 3
    # Backdate first two to ensure deterministic ordering
    plan_db.plans.conn.execute(
        "UPDATE plans SET created_at='2020-01-01T00:00:00Z' WHERE query='first plan'"
    )
    plan_db.plans.conn.execute(
        "UPDATE plans SET created_at='2020-01-02T00:00:00Z' WHERE query='second plan'"
    )
    plan_db.plans.conn.commit()

    results = plan_db.list_plans()
    assert results[0]["query"] == "third plan"
    assert results[1]["query"] == "second plan"
    assert results[2]["query"] == "first plan"


def test_list_plans_empty(plan_db: T2Database) -> None:
    """list_plans() returns an empty list when the plans table is empty."""
    results = plan_db.list_plans()
    assert results == []


def test_list_plans_limit(plan_db: T2Database) -> None:
    """list_plans() respects the limit parameter."""
    for i in range(5):
        plan_db.save_plan(query=f"plan number {i}", plan_json='{}')

    results = plan_db.list_plans(limit=3)
    assert len(results) == 3


def test_save_plan_with_project(plan_db: T2Database) -> None:
    """save_plan() stores the project field correctly."""
    row_id = plan_db.save_plan(
        query="find error patterns",
        plan_json='{"steps":[]}',
        project="nexus",
    )
    row = plan_db.plans.conn.execute("SELECT project FROM plans WHERE id = ?", (row_id,)).fetchone()
    assert row[0] == "nexus"


def test_search_plans_project_filter(plan_db: T2Database) -> None:
    """search_plans() with project filter returns only matching project plans."""
    plan_db.save_plan(query="search code patterns", plan_json='{}', project="nexus")
    plan_db.save_plan(query="search code patterns", plan_json='{}', project="other")

    results = plan_db.search_plans("search", project="nexus")
    assert len(results) == 1
    assert results[0]["project"] == "nexus"

    # Without project filter, both returned
    all_results = plan_db.search_plans("search")
    assert len(all_results) == 2


def test_list_plans_project_filter(plan_db: T2Database) -> None:
    """list_plans() with project filter returns only matching project plans."""
    plan_db.save_plan(query="plan a", plan_json='{}', project="nexus")
    plan_db.save_plan(query="plan b", plan_json='{}', project="other")
    plan_db.save_plan(query="plan c", plan_json='{}', project="nexus")

    results = plan_db.list_plans(project="nexus")
    assert len(results) == 2
    assert all(r["project"] == "nexus" for r in results)


def test_save_plan_with_ttl(plan_db: T2Database) -> None:
    """save_plan() stores the ttl field correctly."""
    row_id = plan_db.save_plan(
        query="cached author search",
        plan_json='{"steps":[]}',
        ttl=30,
    )
    row = plan_db.plans.conn.execute("SELECT ttl FROM plans WHERE id = ?", (row_id,)).fetchone()
    assert row[0] == 30


def test_save_plan_ttl_none_by_default(plan_db: T2Database) -> None:
    """save_plan() without ttl stores NULL (permanent)."""
    row_id = plan_db.save_plan(query="permanent plan", plan_json='{}')
    row = plan_db.plans.conn.execute("SELECT ttl FROM plans WHERE id = ?", (row_id,)).fetchone()
    assert row[0] is None


def test_search_plans_includes_ttl(plan_db: T2Database) -> None:
    """search_plans() results include the ttl field."""
    plan_db.save_plan(query="search with ttl", plan_json='{}', ttl=7)
    results = plan_db.search_plans("search")
    assert len(results) == 1
    assert results[0]["ttl"] == 7


def test_list_plans_includes_ttl(plan_db: T2Database) -> None:
    """list_plans() results include the ttl field."""
    plan_db.save_plan(query="plan with ttl", plan_json='{}', ttl=14)
    results = plan_db.list_plans()
    assert len(results) == 1
    assert results[0]["ttl"] == 14


# ── plan_exists (RDR-063 Landmine 1 fix) ────────────────────────────────────


def test_plan_exists_returns_false_on_empty_db(plan_db: T2Database) -> None:
    """plan_exists() returns False when the plans table is empty."""
    assert plan_db.plan_exists("any query", "any-tag") is False


def test_plan_exists_matches_query_and_tag(plan_db: T2Database) -> None:
    """plan_exists() returns True when a plan has both the query and tag."""
    plan_db.save_plan(
        query="seed query",
        plan_json='{}',
        tags="builtin-template,catalog,author",
    )
    assert plan_db.plan_exists("seed query", "builtin-template") is True
    assert plan_db.plan_exists("seed query", "catalog") is True
    assert plan_db.plan_exists("seed query", "author") is True


def test_plan_exists_false_on_query_mismatch(plan_db: T2Database) -> None:
    """plan_exists() returns False when the query does not match."""
    plan_db.save_plan(
        query="exists",
        plan_json='{}',
        tags="builtin-template",
    )
    assert plan_db.plan_exists("different query", "builtin-template") is False


def test_plan_exists_false_on_tag_mismatch(plan_db: T2Database) -> None:
    """plan_exists() returns False when the tag is not among the plan's tags."""
    plan_db.save_plan(
        query="q",
        plan_json='{}',
        tags="builtin-template,catalog",
    )
    assert plan_db.plan_exists("q", "nonexistent-tag") is False


def test_plan_exists_uses_comma_boundary_match(plan_db: T2Database) -> None:
    """plan_exists() matches whole tokens, not substrings.

    Regression guard for the review finding: the pre-fix substring LIKE would
    return True for ``builtin-template`` when a plan's tags contained
    ``builtin-template-v2`` or ``not-builtin-template``. The comma-boundary
    pattern ``(',' || tags || ',') LIKE '%,<tag>,%'`` prevents that.
    """
    # Plan tagged with a SUPERSTRING of the search tag — must NOT match.
    plan_db.save_plan(
        query="superstring",
        plan_json='{}',
        tags="builtin-template-v2,other",
    )
    assert plan_db.plan_exists("superstring", "builtin-template") is False

    # Plan tagged with a PREFIXED variant — must NOT match.
    plan_db.save_plan(
        query="prefixed",
        plan_json='{}',
        tags="not-builtin-template,other",
    )
    assert plan_db.plan_exists("prefixed", "builtin-template") is False

    # Plan tagged with the exact token in the MIDDLE of the comma list — must match.
    plan_db.save_plan(
        query="middle",
        plan_json='{}',
        tags="other,builtin-template,more",
    )
    assert plan_db.plan_exists("middle", "builtin-template") is True

    # Plan tagged with the exact token at the END — must match.
    plan_db.save_plan(
        query="end",
        plan_json='{}',
        tags="other,builtin-template",
    )
    assert plan_db.plan_exists("end", "builtin-template") is True


def test_plan_exists_isolated_per_query(plan_db: T2Database) -> None:
    """plan_exists() scopes the match to a single query string."""
    plan_db.save_plan(query="query-a", plan_json='{}', tags="builtin-template")
    plan_db.save_plan(query="query-b", plan_json='{}', tags="other")

    assert plan_db.plan_exists("query-a", "builtin-template") is True
    assert plan_db.plan_exists("query-b", "builtin-template") is False
