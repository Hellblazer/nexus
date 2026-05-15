# SPDX-License-Identifier: Apache-2.0
"""Tests for the tuples.db retention sweeper (nexus-kk9h, RDR-111).

Covers:
- ``prune_expired_tuples`` deletes rows with past ``expires_at``.
- NULL ``expires_at`` rows are never deleted.
- Future ``expires_at`` rows are retained.
- Paired Chroma rows are removed for deleted tuples.
- ``api.out()`` sets ``expires_at = now + retention_seconds`` when the
  schema declares a positive ``retention_seconds``.
- ``api.out()`` leaves ``expires_at`` NULL when the schema declares
  ``retention_seconds: 0``.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import chromadb
import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

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
  floor: 0.30
  margin: 0.05
  default_lease_seconds: 60
read:
  default_floor: 0.20
  default_n: 5
tiers: [project]
retention_seconds: 86400
"""

_NOTRETAIN_YAML = """
name: ephemeral
tier: project
content_type: text
embed_from: content
dimensions:
  kind: { type: string, required: true }
take:
  enabled: false
  mode: semantic
  floor: 0.30
  margin: 0.05
  default_lease_seconds: 60
read:
  default_floor: 0.20
  default_n: 3
tiers: [project]
retention_seconds: 0
"""


@pytest.fixture
def builtin_dir(tmp_path: Path) -> Path:
    d = tmp_path / "builtin"
    d.mkdir()
    (d / "tasks.yml").write_text(_TASKS_YAML)
    (d / "ephemeral.yml").write_text(_NOTRETAIN_YAML)
    return d


@pytest.fixture
def registry(builtin_dir: Path):
    from nexus.tuplespace.registry import Registry
    return Registry.load(builtin_dir)


@pytest.fixture
def db_conn(tmp_path: Path):
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


# ---------------------------------------------------------------------------
# api.out() — expires_at population from schema.retention_seconds
# ---------------------------------------------------------------------------


def _valid_task_dims():
    return {"status": "open", "priority": "P1", "created_by": "agent-X"}


class TestOutPopulatesExpiresAt:
    def test_out_sets_expires_at_from_schema_retention(self, db_conn, index, registry):
        from nexus.tuplespace.api import out

        before = time.time()
        tid = out(
            conn=db_conn,
            index=index,
            registry=registry,
            subspace="tasks/nexus",
            content="Fix bug",
            dimensions=_valid_task_dims(),
        )
        after = time.time()
        row = db_conn.execute(
            "SELECT expires_at FROM tuples WHERE id = ?", (tid,)
        ).fetchone()
        assert row["expires_at"] is not None
        # tasks schema retention_seconds = 86400
        assert before + 86400 - 1 <= row["expires_at"] <= after + 86400 + 1

    def test_out_leaves_expires_at_null_when_retention_zero(
        self, db_conn, index, registry
    ):
        from nexus.tuplespace.api import out

        tid = out(
            conn=db_conn,
            index=index,
            registry=registry,
            subspace="ephemeral",
            content="x",
            dimensions={"kind": "transient"},
        )
        row = db_conn.execute(
            "SELECT expires_at FROM tuples WHERE id = ?", (tid,)
        ).fetchone()
        assert row["expires_at"] is None

    def test_out_explicit_ttl_overrides_schema(self, db_conn, index, registry):
        from nexus.tuplespace.api import out

        before = time.time()
        tid = out(
            conn=db_conn,
            index=index,
            registry=registry,
            subspace="tasks/nexus",
            content="short-lived",
            dimensions=_valid_task_dims(),
            ttl_seconds=10,
        )
        after = time.time()
        row = db_conn.execute(
            "SELECT expires_at FROM tuples WHERE id = ?", (tid,)
        ).fetchone()
        assert row["expires_at"] is not None
        assert before + 10 - 1 <= row["expires_at"] <= after + 10 + 1


# ---------------------------------------------------------------------------
# prune_expired_tuples — SQL behaviour
# ---------------------------------------------------------------------------


class TestPruneExpired:
    def test_prune_deletes_past_expiry(self, db_conn, index, registry):
        from nexus.tuplespace.api import out
        from nexus.tuplespace.store import prune_expired_tuples

        tid = out(
            conn=db_conn,
            index=index,
            registry=registry,
            subspace="tasks/nexus",
            content="stale",
            dimensions=_valid_task_dims(),
        )
        # Force expiry into the past.
        db_conn.execute(
            "UPDATE tuples SET expires_at = ? WHERE id = ?",
            (time.time() - 100, tid),
        )
        db_conn.commit()

        deleted = prune_expired_tuples(db_conn, index=index, registry=registry)
        assert deleted == 1
        row = db_conn.execute(
            "SELECT id FROM tuples WHERE id = ?", (tid,)
        ).fetchone()
        assert row is None

    def test_prune_keeps_null_expires_at(self, db_conn, index, registry):
        from nexus.tuplespace.api import out
        from nexus.tuplespace.store import prune_expired_tuples

        tid = out(
            conn=db_conn,
            index=index,
            registry=registry,
            subspace="ephemeral",
            content="forever",
            dimensions={"kind": "transient"},
        )
        deleted = prune_expired_tuples(db_conn, index=index, registry=registry)
        assert deleted == 0
        row = db_conn.execute(
            "SELECT id FROM tuples WHERE id = ?", (tid,)
        ).fetchone()
        assert row is not None

    def test_prune_keeps_future_expires_at(self, db_conn, index, registry):
        from nexus.tuplespace.api import out
        from nexus.tuplespace.store import prune_expired_tuples

        tid = out(
            conn=db_conn,
            index=index,
            registry=registry,
            subspace="tasks/nexus",
            content="fresh",
            dimensions=_valid_task_dims(),
        )
        deleted = prune_expired_tuples(db_conn, index=index, registry=registry)
        assert deleted == 0
        row = db_conn.execute(
            "SELECT id FROM tuples WHERE id = ?", (tid,)
        ).fetchone()
        assert row is not None

    def test_prune_removes_paired_chroma_row(
        self, db_conn, index, registry, chroma_client
    ):
        from nexus.tuplespace.api import out
        from nexus.tuplespace.index import collection_name
        from nexus.tuplespace.store import prune_expired_tuples

        tid = out(
            conn=db_conn,
            index=index,
            registry=registry,
            subspace="tasks/nexus",
            content="stale-chroma",
            dimensions=_valid_task_dims(),
        )
        coll = chroma_client.get_collection(collection_name("tasks/<project>"))
        pre = coll.get(ids=[tid])
        assert pre["ids"] == [tid]

        db_conn.execute(
            "UPDATE tuples SET expires_at = ? WHERE id = ?",
            (time.time() - 100, tid),
        )
        db_conn.commit()
        deleted = prune_expired_tuples(db_conn, index=index, registry=registry)
        assert deleted == 1

        post = coll.get(ids=[tid])
        assert post["ids"] == []

    def test_prune_with_now_override(self, db_conn, index, registry):
        """Passing ``now`` lets callers prune relative to a synthetic clock."""
        from nexus.tuplespace.api import out
        from nexus.tuplespace.store import prune_expired_tuples

        tid = out(
            conn=db_conn,
            index=index,
            registry=registry,
            subspace="tasks/nexus",
            content="future-prune",
            dimensions=_valid_task_dims(),
        )
        # Row is fresh (expires_at ~ now + 86400). Pass a far-future "now".
        future = time.time() + 10 * 86400
        deleted = prune_expired_tuples(
            db_conn, index=index, registry=registry, now=int(future)
        )
        assert deleted == 1
