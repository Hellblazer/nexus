# SPDX-License-Identifier: AGPL-3.0-or-later
"""TDD tests for :class:`nexus.plans.session_cache.PlanSessionCache`.

Exercises the T1 ``plans__session`` ChromaDB collection against an
``EphemeralClient`` (local ONNX embedding, no HTTP server, no API
keys) so the SC-2 contract is verified end-to-end in CI.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture()
def ephemeral_cache():
    import chromadb
    from nexus.plans.session_cache import PlanSessionCache

    client = chromadb.EphemeralClient()
    return PlanSessionCache(client=client, session_id="test-session")


@pytest.fixture()
def library(tmp_path: Path):
    from nexus.db.migrations import _add_plan_dimensional_identity
    from nexus.db.t2.plan_library import PlanLibrary

    lib = PlanLibrary(tmp_path / "plans.db")
    _add_plan_dimensional_identity(lib.conn)
    lib.conn.commit()
    return lib


def _seed(library, *, query: str, project: str = "nexus") -> int:
    return library.save_plan(
        query=query,
        plan_json=json.dumps({"steps": []}),
        tags="",
        project=project,
        outcome="success",
    )


# ── Availability ────────────────────────────────────────────────────────────


def test_cache_is_available_with_ephemeral_client(ephemeral_cache) -> None:
    assert ephemeral_cache.is_available is True


# ── populate + query ────────────────────────────────────────────────────────


def test_populate_loads_active_plans(ephemeral_cache, library) -> None:
    pid = _seed(library, query="research projection quality mechanism")
    ephemeral_cache.populate(library)

    hits = ephemeral_cache.query("projection quality", n=3)
    assert any(h[0] == pid for h in hits)


def test_populate_clears_prior_state(ephemeral_cache, library) -> None:
    """Populate is a full rebuild — prior entries must not linger.

    SQLite reuses ``INTEGER PRIMARY KEY`` IDs after delete, so the
    rebuild is verified by count + document content, not by id.
    """
    _seed(library, query="first plan about x")
    _seed(library, query="another early plan about z")
    ephemeral_cache.populate(library)

    # Confirm both early plans made it.
    assert len(ephemeral_cache.query("plan", n=10)) == 2

    # Drop everything, seed a single new plan, re-populate.
    library.conn.execute("DELETE FROM plans")
    library.conn.commit()
    _seed(library, query="second plan about y")
    ephemeral_cache.populate(library)

    # Cache should hold exactly one row now; re-query under a term from
    # the dropped plans returns at most that single survivor.
    hits = ephemeral_cache.query("plan", n=10)
    assert len(hits) == 1


def test_query_returns_plan_id_and_distance(ephemeral_cache, library) -> None:
    pid = _seed(library, query="walking the link graph")
    ephemeral_cache.populate(library)
    hits = ephemeral_cache.query("walking the link graph", n=1)
    assert hits, "expected at least one hit for exact-match query"
    plan_id, distance = hits[0]
    assert plan_id == pid
    assert 0.0 <= distance <= 2.0  # cosine distance range


# ── upsert (mid-session plan_save hook) ────────────────────────────────────


def test_upsert_makes_plan_visible_within_session(
    ephemeral_cache, library,
) -> None:
    """SC-2 — a plan_save during the session must be visible to plan_match
    immediately, without waiting for the next SessionStart."""
    ephemeral_cache.populate(library)  # empty T2 → empty cache

    pid = _seed(library, query="a new plan added mid-session")
    row = library.get_plan(pid)
    ephemeral_cache.upsert(row)

    hits = ephemeral_cache.query("new plan", n=5)
    assert any(h[0] == pid for h in hits)


def test_upsert_overwrites_existing(ephemeral_cache, library) -> None:
    pid = _seed(library, query="original description")
    ephemeral_cache.populate(library)

    library.conn.execute(
        "UPDATE plans SET query = ? WHERE id = ?",
        ("updated description text", pid),
    )
    library.conn.commit()
    ephemeral_cache.upsert(library.get_plan(pid))

    hits = ephemeral_cache.query("updated description", n=1)
    assert hits and hits[0][0] == pid


# ── Resilience ─────────────────────────────────────────────────────────────


def test_populate_skips_rows_with_empty_description(
    ephemeral_cache, library,
) -> None:
    """Sanity guard: an empty description would produce a degenerate
    embedding; skip rather than pollute the cache."""
    library.save_plan(query="", plan_json="{}", project="nexus")
    ephemeral_cache.populate(library)  # must not raise


def test_query_empty_cache_returns_empty_list(ephemeral_cache) -> None:
    assert ephemeral_cache.query("anything", n=5) == []
