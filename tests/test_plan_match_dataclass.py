# SPDX-License-Identifier: AGPL-3.0-or-later
"""TDD tests for the ``Match`` dataclass — RDR-078 P1 (nexus-05i.2).

The dataclass is the unit of currency between :func:`plan_match` and
:func:`plan_run`. Two construction paths exist:

  * From a T1 cosine query result — ``confidence`` is the cosine score.
  * From a T2 ``plans`` row when T1 is unavailable —
    ``Match.from_plan_row(row)`` sets ``confidence=None`` as the FTS5
    fallback sentinel.

Schema reference: RDR-078 §Phase 1 Vocabulary.
"""
from __future__ import annotations

import json

import pytest


def test_match_constructs_with_required_fields() -> None:
    from nexus.plans.match import Match

    m = Match(
        plan_id=42,
        name="default",
        description="Walk the link graph from a seed RDR.",
        confidence=0.91,
        dimensions={"verb": "research", "scope": "global"},
        tags="builtin-template,research",
        plan_json='{"steps": []}',
        required_bindings=["intent"],
        optional_bindings=[],
        default_bindings={},
        parent_dims=None,
    )
    assert m.plan_id == 42
    assert m.confidence == 0.91
    assert m.dimensions == {"verb": "research", "scope": "global"}


def test_match_confidence_none_marks_fts5_fallback() -> None:
    """``confidence=None`` is the FTS5-fallback sentinel.

    Skills that gate on ``confidence >= threshold`` must explicitly
    check ``confidence is not None`` first.
    """
    from nexus.plans.match import Match

    m = Match(
        plan_id=1, name="x", description="d", confidence=None,
        dimensions={}, tags="", plan_json="{}", required_bindings=[],
        optional_bindings=[], default_bindings={}, parent_dims=None,
    )
    assert m.confidence is None


def test_from_plan_row_parses_dimensions_json() -> None:
    """``dimensions`` arrives as a canonical JSON string from SQLite;
    ``Match.from_plan_row`` parses it back into a dict."""
    from nexus.plans.match import Match

    row = {
        "id": 7, "project": "nexus", "query": "Walk decision history.",
        "plan_json": '{"steps": []}', "outcome": "success",
        "tags": "builtin-template",
        "created_at": "2026-04-15T00:00:00Z", "ttl": None,
        "dimensions": '{"scope":"global","verb":"research"}',
        "default_bindings": None, "parent_dims": None, "name": "default",
        "verb": "research", "scope": "global",
    }
    m = Match.from_plan_row(row)
    assert m.confidence is None
    assert m.plan_id == 7
    assert m.name == "default"
    assert m.description == "Walk decision history."
    assert m.dimensions == {"scope": "global", "verb": "research"}
    assert m.default_bindings == {}
    assert m.parent_dims is None
    assert m.tags == "builtin-template"
    assert m.plan_json == '{"steps": []}'


def test_from_plan_row_parses_default_bindings_json() -> None:
    from nexus.plans.match import Match

    row = {
        "id": 1, "project": "nexus", "query": "q", "plan_json": "{}",
        "outcome": "success", "tags": "", "created_at": "2026-04-15Z",
        "ttl": None, "dimensions": '{"verb":"r"}',
        "default_bindings": '{"intent":"default-intent","limit":5}',
        "parent_dims": None, "name": "default", "verb": "r", "scope": None,
    }
    m = Match.from_plan_row(row)
    assert m.default_bindings == {"intent": "default-intent", "limit": 5}


def test_from_plan_row_parses_parent_dims_json() -> None:
    from nexus.plans.match import Match

    row = {
        "id": 1, "project": "nexus", "query": "q", "plan_json": "{}",
        "outcome": "success", "tags": "", "created_at": "Z", "ttl": None,
        "dimensions": '{"verb":"r","strategy":"security"}',
        "default_bindings": None,
        "parent_dims": '{"verb":"r","strategy":"default"}',
        "name": "security", "verb": "r", "scope": None,
    }
    m = Match.from_plan_row(row)
    assert m.parent_dims == {"verb": "r", "strategy": "default"}


def test_from_plan_row_handles_legacy_rdr042_rows() -> None:
    """RDR-042 rows have NULL dimensional columns; the fallback must
    still produce a runnable Match."""
    from nexus.plans.match import Match

    row = {
        "id": 1, "project": "nexus", "query": "legacy plan", "plan_json": "{}",
        "outcome": "success", "tags": "", "created_at": "Z", "ttl": None,
        "dimensions": None, "default_bindings": None, "parent_dims": None,
        "name": None, "verb": None, "scope": None,
    }
    m = Match.from_plan_row(row)
    assert m.dimensions == {}
    assert m.default_bindings == {}
    assert m.parent_dims is None
    assert m.name == ""


def test_from_plan_row_extracts_required_and_optional_bindings() -> None:
    """``plan_json`` declares ``required_bindings`` / ``optional_bindings``;
    the constructor surfaces them as top-level Match fields so the runner
    can validate without re-parsing."""
    from nexus.plans.match import Match

    plan = {
        "steps": [],
        "required_bindings": ["intent", "subtree"],
        "optional_bindings": ["limit"],
    }
    row = {
        "id": 1, "project": "nexus", "query": "q",
        "plan_json": json.dumps(plan),
        "outcome": "success", "tags": "", "created_at": "Z", "ttl": None,
        "dimensions": None, "default_bindings": None, "parent_dims": None,
        "name": "default", "verb": "research", "scope": "global",
    }
    m = Match.from_plan_row(row)
    assert m.required_bindings == ["intent", "subtree"]
    assert m.optional_bindings == ["limit"]


def test_from_plan_row_ignores_malformed_json() -> None:
    """A corrupt ``dimensions`` value must not crash the fallback —
    fall back to an empty dict and let the runner surface the issue."""
    from nexus.plans.match import Match

    row = {
        "id": 1, "project": "nexus", "query": "q", "plan_json": "{}",
        "outcome": "success", "tags": "", "created_at": "Z", "ttl": None,
        "dimensions": "not-json", "default_bindings": "also-not-json",
        "parent_dims": "garbage", "name": None, "verb": None, "scope": None,
    }
    m = Match.from_plan_row(row)
    assert m.dimensions == {}
    assert m.default_bindings == {}
    assert m.parent_dims is None
