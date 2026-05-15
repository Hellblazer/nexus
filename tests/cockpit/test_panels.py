# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for cockpit Phase 3 panels -- active claims, recent events, bindings.

TDD-first: written before the panel implementations under
``src/nexus/cockpit/panels/``. Each panel is a pure data-fetch function
returning a dataclass; the renderer (if any) lives separately. Tests
exercise the fetchers against a real ``tuples.db`` + ``EphemeralClient``
chroma index so the SQL paths and claim semantics are validated end-to-end
(integration over mocks, per project conventions).
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import chromadb
import pytest

from nexus.tuplespace.api import out, take
from nexus.tuplespace.index import TupleIndex
from nexus.tuplespace.registry import Registry
from nexus.tuplespace.store import open_tuples_db


_TASKS_YAML = """
name: tasks/<project>
tier: project
content_type: text
embed_from: content
dimensions:
  status:     { type: enum, values: [open, in_progress, done, cancelled], required: true }
  priority:   { type: enum, values: [P0, P1, P2, P3, P4], required: true }
  created_by: { type: string, required: true }
take:
  enabled: true
  mode: semantic
  floor: 0.0
  margin: 0.0
  default_lease_seconds: 300
read:
  default_floor: 0.0
  default_n: 5
tiers: [project]
retention_seconds: 86400
"""


@pytest.fixture
def builtin_dir(tmp_path: Path) -> Path:
    d = tmp_path / "builtin"
    d.mkdir()
    (d / "tasks.yml").write_text(_TASKS_YAML)
    return d


@pytest.fixture
def registry(builtin_dir: Path) -> Registry:
    return Registry.load(builtin_dir)


@pytest.fixture
def conn(tmp_path: Path):
    db = tmp_path / "tuples.db"
    c = open_tuples_db(db)
    c.row_factory = sqlite3.Row
    yield c
    c.close()


@pytest.fixture
def chroma_client():
    client = chromadb.EphemeralClient()
    yield client
    for coll in client.list_collections():
        client.delete_collection(coll.name)


@pytest.fixture
def index(registry, chroma_client) -> TupleIndex:
    return TupleIndex.from_registry(registry, chroma_client)


# ---------------------------------------------------------------------------
# active_claims panel
# ---------------------------------------------------------------------------


class TestActiveClaimsPanel:
    def test_empty_when_no_claims(self, conn):
        from nexus.cockpit.panels.active_claims import fetch_active_claims

        result = fetch_active_claims(conn=conn)
        assert result.rows == []
        assert result.total == 0

    def test_groups_claims_by_subspace(self, conn, index, registry):
        # Drop two tuples into the same subspace, claim one.
        out(
            conn=conn, index=index, registry=registry,
            subspace="tasks/demo",
            content="alpha", dimensions={"status": "open", "priority": "P1", "created_by": "a"},
        )
        out(
            conn=conn, index=index, registry=registry,
            subspace="tasks/demo",
            content="beta", dimensions={"status": "open", "priority": "P2", "created_by": "b"},
        )
        claimed = take(
            conn=conn, index=index, registry=registry,
            subspace="tasks/demo", query="alpha", claimant="worker-1",
        )
        assert claimed is not None

        from nexus.cockpit.panels.active_claims import fetch_active_claims

        result = fetch_active_claims(conn=conn)
        assert result.total == 1
        assert len(result.rows) == 1
        row = result.rows[0]
        assert row.subspace == "tasks/demo"
        assert row.claimant == "worker-1"
        assert row.claim_id != ""
        # ttl-remaining is a positive number of seconds (lease 300s, just claimed)
        assert row.ttl_remaining_seconds is not None
        assert row.ttl_remaining_seconds > 0

    def test_groups_field_returns_per_subspace_summary(self, conn, index, registry):
        out(
            conn=conn, index=index, registry=registry,
            subspace="tasks/alpha",
            content="x", dimensions={"status": "open", "priority": "P1", "created_by": "a"},
        )
        out(
            conn=conn, index=index, registry=registry,
            subspace="tasks/beta",
            content="y", dimensions={"status": "open", "priority": "P1", "created_by": "a"},
        )
        take(conn=conn, index=index, registry=registry,
             subspace="tasks/alpha", query="x", claimant="w1")
        take(conn=conn, index=index, registry=registry,
             subspace="tasks/beta", query="y", claimant="w2")

        from nexus.cockpit.panels.active_claims import fetch_active_claims

        result = fetch_active_claims(conn=conn)
        assert result.total == 2
        groups = result.groups_by_subspace()
        assert set(groups) == {"tasks/alpha", "tasks/beta"}
        assert len(groups["tasks/alpha"]) == 1
        assert len(groups["tasks/beta"]) == 1


# ---------------------------------------------------------------------------
# recent_events panel
# ---------------------------------------------------------------------------


class TestRecentEventsPanel:
    def test_empty_when_no_events(self, conn):
        from nexus.cockpit.panels.recent_events import fetch_recent_events

        result = fetch_recent_events(conn=conn, limit=10)
        assert result.rows == []

    def test_newest_first(self, conn, index, registry):
        for i in range(3):
            out(
                conn=conn, index=index, registry=registry,
                subspace=f"tasks/{i}",
                content=f"c{i}",
                dimensions={"status": "open", "priority": "P1", "created_by": "a"},
            )
            time.sleep(0.001)

        from nexus.cockpit.panels.recent_events import fetch_recent_events

        result = fetch_recent_events(conn=conn, limit=10)
        assert len(result.rows) == 3
        # newest-first: subspace tasks/2 comes first
        assert result.rows[0].subspace == "tasks/2"
        assert result.rows[-1].subspace == "tasks/0"
        # every row carries op, tuple_id, ts
        for row in result.rows:
            assert row.op == "out"
            assert row.tuple_id
            assert row.ts > 0

    def test_respects_limit(self, conn, index, registry):
        for i in range(5):
            out(
                conn=conn, index=index, registry=registry,
                subspace="tasks/x",
                content=f"c{i}",
                dimensions={"status": "open", "priority": "P1", "created_by": "a"},
            )

        from nexus.cockpit.panels.recent_events import fetch_recent_events

        result = fetch_recent_events(conn=conn, limit=2)
        assert len(result.rows) == 2


# ---------------------------------------------------------------------------
# active_bindings panel
# ---------------------------------------------------------------------------


_PROFILE_YAML = """\
profile: testprofile
bindings:
  - name: rule_a
    match:
      subspace: hook_events/notification
      op: out
    action:
      kind: log
      marker: hit_a
  - name: rule_b
    match:
      subspace: hook_events/tool_call_completed
      op: out
    action:
      kind: python
      callable: nexus.cockpit.bindings:action_log_marker
"""


class TestActiveBindingsPanel:
    def test_empty_when_no_profiles_dir(self, tmp_path: Path):
        from nexus.cockpit.panels.active_bindings import fetch_active_bindings

        result = fetch_active_bindings(profiles_dir=tmp_path / "nope")
        assert result.rows == []

    def test_lists_bindings_per_profile(self, tmp_path: Path):
        prof_dir = tmp_path / "profiles"
        prof_dir.mkdir()
        (prof_dir / "testprofile.yml").write_text(_PROFILE_YAML)

        from nexus.cockpit.panels.active_bindings import fetch_active_bindings

        result = fetch_active_bindings(profiles_dir=prof_dir)
        assert len(result.rows) == 2
        names = {r.binding_name for r in result.rows}
        assert names == {"rule_a", "rule_b"}
        a = next(r for r in result.rows if r.binding_name == "rule_a")
        assert a.profile == "testprofile"
        assert a.action_ref == "log:hit_a"
        assert "subspace=hook_events/notification" in a.match_summary

        b = next(r for r in result.rows if r.binding_name == "rule_b")
        assert b.action_ref == "python:nexus.cockpit.bindings:action_log_marker"
