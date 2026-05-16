# SPDX-License-Identifier: Apache-2.0
"""Tests for tuple dedup-refresh on identical content fires (nexus-i4kd).

PR #786 deep review: content-identical refires must refresh
``created_at`` / ``expires_at`` so the retention sweeper (nexus-kk9h)
does not expire the row based on the first fire's clock. The refresh
must NOT clobber an in-flight claim.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

import chromadb
import pytest

_TASKS_YAML = """
name: tasks/<project>
tier: project
content_type: text
embed_from: content
dimensions:
  status:     { type: enum, values: [open, in_progress, done, cancelled], required: true }
  priority:   { type: enum, values: [P0, P1, P2, P3, P4], required: true }
  assignee:   { type: string, required: false }
  created_by: { type: string, required: true }
take:
  enabled: true
  mode: semantic
  floor: 0.30
  margin: 0.05
  default_lease_seconds: 60
read:
  default_floor: 0.20
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
def registry(builtin_dir: Path):
    from nexus.tuplespace.registry import Registry
    return Registry.load(builtin_dir)


@pytest.fixture
def db_conn(tmp_path: Path) -> sqlite3.Connection:
    from nexus.tuplespace.store import open_tuples_db
    conn = open_tuples_db(tmp_path / "tuples.db")
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.fixture
def chroma_client():
    client = chromadb.EphemeralClient()
    yield client
    for coll in client.list_collections():
        client.delete_collection(coll.name)


@pytest.fixture
def index(registry, chroma_client):
    from nexus.tuplespace.index import TupleIndex
    return TupleIndex.from_registry(registry, chroma_client)


def _dims() -> dict[str, Any]:
    return {"status": "open", "priority": "P1", "created_by": "agent-X"}


class TestDedupRefresh:
    def test_refire_bumps_created_and_expires(self, db_conn, index, registry):
        """Content-identical refire must move created_at/expires_at forward."""
        from nexus.tuplespace.api import out

        tid1 = out(
            conn=db_conn, index=index, registry=registry,
            subspace="tasks/nexus",
            content="identical body",
            dimensions=_dims(),
        )
        first = db_conn.execute(
            "SELECT created_at, expires_at FROM tuples WHERE id = ?", (tid1,)
        ).fetchone()

        # Force a forward clock movement detectable at REAL precision.
        time.sleep(0.05)

        tid2 = out(
            conn=db_conn, index=index, registry=registry,
            subspace="tasks/nexus",
            content="identical body",
            dimensions=_dims(),
        )
        assert tid1 == tid2
        second = db_conn.execute(
            "SELECT created_at, expires_at FROM tuples WHERE id = ?", (tid2,)
        ).fetchone()

        assert second["created_at"] > first["created_at"], (
            "refire must refresh created_at"
        )
        assert second["expires_at"] > first["expires_at"], (
            "refire must refresh expires_at so retention sweeper respects recency"
        )

    def test_refire_respects_retention(self, db_conn, index, registry):
        """Refreshed expires_at must equal created_at + retention_seconds."""
        from nexus.tuplespace.api import out

        tid = out(
            conn=db_conn, index=index, registry=registry,
            subspace="tasks/nexus",
            content="ttl check",
            dimensions=_dims(),
        )
        time.sleep(0.05)
        out(
            conn=db_conn, index=index, registry=registry,
            subspace="tasks/nexus",
            content="ttl check",
            dimensions=_dims(),
        )
        row = db_conn.execute(
            "SELECT created_at, expires_at FROM tuples WHERE id = ?", (tid,)
        ).fetchone()
        # retention_seconds = 86400 from schema fixture
        delta = row["expires_at"] - row["created_at"]
        assert abs(delta - 86400.0) < 0.01

    def test_refire_does_not_clobber_in_flight_claim(self, db_conn, index, registry):
        """A refire on a content-identical row must leave claim state intact."""
        from nexus.tuplespace.api import out

        tid = out(
            conn=db_conn, index=index, registry=registry,
            subspace="tasks/nexus",
            content="claimed body",
            dimensions=_dims(),
        )
        # Simulate an in-flight claim directly via SQL.
        lease_expiry = time.time() + 60
        db_conn.execute(
            "UPDATE tuples SET claim_state=?, claimant=?, claim_id=?, claim_expires_at=? "
            "WHERE id=?",
            ("claimed", "agent-A", "claim-xyz", lease_expiry, tid),
        )
        db_conn.commit()
        time.sleep(0.05)

        out(
            conn=db_conn, index=index, registry=registry,
            subspace="tasks/nexus",
            content="claimed body",
            dimensions=_dims(),
        )

        row = db_conn.execute(
            "SELECT claim_state, claimant, claim_id, claim_expires_at "
            "FROM tuples WHERE id=?",
            (tid,),
        ).fetchone()
        assert row["claim_state"] == "claimed"
        assert row["claimant"] == "agent-A"
        assert row["claim_id"] == "claim-xyz"
        assert abs(row["claim_expires_at"] - lease_expiry) < 0.001
