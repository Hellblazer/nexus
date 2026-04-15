# SPDX-License-Identifier: AGPL-3.0-or-later
"""SC-9 regression suite — RDR-078 P1 (nexus-05i.2).

The epic close gate for RDR-078 is "zero regressions in plan_save /
plan_search / search / query from the P1 additions". This suite
verifies the round-trip behaviour that existed before this bead still
holds, focusing on areas where the matcher + T1 cache could plausibly
have regressed the legacy path.

These are smoke tests intentionally: each legacy tool has its own
exhaustive test suite elsewhere. The point here is to pin the
interaction between the legacy tools and the P1 additions.
"""
from __future__ import annotations

import json

import pytest


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_singletons(monkeypatch, tmp_path):
    """Route MCP tools to a per-test T2 database and reset the plan cache."""
    from pathlib import Path

    from nexus.mcp_infra import reset_plan_cache_for_tests, reset_singletons

    reset_singletons()
    reset_plan_cache_for_tests()
    db_path = Path(tmp_path) / "t2.sqlite"
    monkeypatch.setattr(
        "nexus.mcp_infra.default_db_path", lambda: db_path,
    )
    yield
    reset_singletons()
    reset_plan_cache_for_tests()


# ── plan_save round-trip ────────────────────────────────────────────────────


def test_plan_save_roundtrip_unchanged() -> None:
    """plan_save → plan_search returns the saved plan with original content."""
    from nexus.mcp.core import plan_save, plan_search

    result = plan_save(
        query="walk the link graph from an RDR",
        plan_json=json.dumps({"steps": [{"tool": "traverse", "args": {}}]}),
        project="nexus",
        tags="rdr-078-regression",
    )
    assert result.startswith("Saved plan:"), result

    found = plan_search("link graph", project="nexus")
    assert "walk the link graph from an RDR" in found


def test_plan_save_success_outcome_preserved() -> None:
    """The ``outcome`` field round-trips through save → search."""
    from nexus.mcp.core import plan_save, plan_search

    plan_save(
        query="something", plan_json='{"steps": []}', project="nexus",
        outcome="success", tags="sc9",
    )
    out = plan_search("something", project="nexus")
    assert "outcome=success" in out


def test_plan_save_ttl_honoured() -> None:
    """``ttl`` is accepted and stored on the row."""
    from nexus.mcp.core import plan_save, plan_search

    plan_save(
        query="expiring plan", plan_json='{"steps": []}', project="nexus",
        ttl=30, tags="ttl-test",
    )
    out = plan_search("expiring plan", project="nexus")
    assert "expiring plan" in out


# ── FTS5 baseline ──────────────────────────────────────────────────────────


def test_plan_search_fts5_baseline() -> None:
    """Keyword-only search still returns the expected plan."""
    from nexus.mcp.core import plan_save, plan_search

    plan_save(
        query="review changes in auth middleware",
        plan_json='{"steps": []}', project="nexus", tags="auth,review",
    )
    plan_save(
        query="document the plugin system",
        plan_json='{"steps": []}', project="nexus", tags="docs",
    )

    # Exact-token match on the first plan.
    result = plan_search("auth middleware", project="nexus")
    assert "review changes in auth middleware" in result
    assert "document the plugin system" not in result


# ── Integration: plan_save writes through to matcher ───────────────────────


def test_plan_save_then_plan_match_sees_new_plan() -> None:
    """A mid-session plan_save lands in T1 cache; plan_match finds it
    (may be via FTS5 when the session cache can't bind a client)."""
    from nexus.mcp.core import plan_match, plan_save

    plan_save(
        query="how projection quality ICF hub detection works",
        plan_json='{"steps": []}', project="nexus",
        tags="rdr-077,research",
    )
    out = plan_match("projection quality mechanism", project="nexus",
                     min_confidence=0.0)
    assert "how projection quality ICF hub detection works" in out


# ── Legacy search / query MCP tools unaffected ─────────────────────────────


def test_search_tool_still_importable_and_callable() -> None:
    """``search`` MCP tool still returns a string, no import-time surprises."""
    from nexus.mcp.core import search

    # Empty-store smoke: call returns a "No results" or similar string —
    # the point is that adding plan_match/plan_run didn't break the import
    # path or tool wiring.
    out = search(query="nothing-indexed-sentinel", corpus="knowledge", limit=1)
    assert isinstance(out, str)


def test_plan_save_accepts_dimensional_fields() -> None:
    """RDR-078 P1: plan_save MCP tool must populate the UNIQUE
    (project, dimensions) dedup index when callers supply dimensional
    identity. Two saves with identical canonical dims → single row."""
    from nexus.mcp.core import plan_save

    r1 = plan_save(
        query="first author plan",
        plan_json='{"steps": []}',
        project="nexus-dim-test",
        verb="plan-author",
        scope="global",
        dimensions='{"verb":"plan-author","scope":"global"}',
    )
    assert r1.startswith("Saved plan:"), r1

    # Second save with the same canonical dims should NO-OP, not DUPLICATE.
    # The return string must be distinguishable from a new insert so agent
    # callers can branch on idempotency.
    r2 = plan_save(
        query="second author plan (same dims)",
        plan_json='{"steps": []}',
        project="nexus-dim-test",
        verb="plan-author",
        scope="global",
        dimensions='{"verb":"plan-author","scope":"global"}',
    )
    assert r2.startswith("Plan exists (no-op):"), r2
    assert "was NOT saved" in r2, r2


def test_plan_save_rejects_malformed_dimensions_json() -> None:
    """Bad JSON in dimensions → clean error, not stack trace fragment."""
    from nexus.mcp.core import plan_save

    out = plan_save(
        query="q", plan_json='{"steps": []}', project="nexus",
        dimensions="not json at all",
    )
    assert out.startswith("Error:"), out


def test_plan_match_accepts_json_dimensions_with_commas_in_values() -> None:
    """JSON object form handles values containing commas (legacy CSV form
    fragments on them). Regression for RDR-078 critique finding."""
    from nexus.mcp.core import plan_match

    # Values with commas would fragment the legacy CSV parser; JSON handles it.
    out = plan_match(
        intent="test",
        dimensions='{"topic":"how X, Y, Z works"}',
        project="nexus",
        min_confidence=0.0,
        n=1,
    )
    # Should not error on the dims parse — either no match (string), or hit.
    assert not out.startswith("Error:"), out


def test_memory_tool_still_importable_and_callable() -> None:
    """``memory_put`` / ``memory_get`` still work end-to-end."""
    from nexus.mcp.core import memory_get, memory_put

    put_result = memory_put(
        content="regression probe content",
        project="sc9-regression",
        title="probe",
    )
    assert "Stored" in put_result

    got = memory_get(project="sc9-regression", title="probe")
    assert "regression probe content" in got
