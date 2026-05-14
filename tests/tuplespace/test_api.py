# SPDX-License-Identifier: Apache-2.0
"""Tests for nexus.tuplespace.api — Core API (RDR-110 P1.4, nexus-8q4v).

Covers: out, read, take (semantic + exact + CAS race), ack, nack,
list_subspaces, subspace_schema, subspace_stats.

block=True is feature-flagged OFF in Phase 1 (RDR-112 §A2); tests
confirm the parameter is accepted but raises BlockingNotSupported in
direct mode.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

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

_LOCKS_YAML = """
name: locks/<resource>
tier: project
content_type: text
embed_from: content
dimensions:
  resource: { type: string, required: true }
  holder:   { type: string, required: true }
take:
  enabled: true
  mode: exact
  match_keys: [resource]
  default_lease_seconds: 30
read:
  default_floor: 0.0
  default_n: 1
tiers: [project]
retention_seconds: 3600
"""

_PLANS_YAML = """
name: plans
tier: project
content_type: text
embed_from: match_text
dimensions:
  query_hash: { type: string, required: true }
take:
  enabled: true
  mode: semantic
  floor: 0.30
  margin: 0.05
  default_lease_seconds: 60
read:
  default_floor: 0.20
  default_n: 3
tiers: [project]
retention_seconds: 86400
"""

_DIM_EMBED_YAML = """
name: signals/<channel>
tier: project
content_type: text
embed_from: "dimensions:priority"
dimensions:
  priority: { type: string, required: true }
  note:     { type: string, required: false }
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
retention_seconds: 86400
"""


@pytest.fixture
def builtin_dir(tmp_path: Path) -> Path:
    d = tmp_path / "builtin"
    d.mkdir()
    (d / "tasks.yml").write_text(_TASKS_YAML)
    (d / "locks.yml").write_text(_LOCKS_YAML)
    (d / "plans.yml").write_text(_PLANS_YAML)
    (d / "signals.yml").write_text(_DIM_EMBED_YAML)
    return d


@pytest.fixture
def registry(builtin_dir: Path):
    from nexus.tuplespace.registry import Registry
    return Registry.load(builtin_dir)


@pytest.fixture
def db_conn(tmp_path: Path) -> sqlite3.Connection:
    from nexus.tuplespace.store import open_tuples_db
    db_path = tmp_path / "tuples.db"
    conn = open_tuples_db(db_path)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.fixture
def chroma_client():
    client = chromadb.EphemeralClient()
    yield client
    # Clear collections so EphemeralClient shared-process state doesn't bleed
    for coll in client.list_collections():
        client.delete_collection(coll.name)


@pytest.fixture
def index(registry, chroma_client):
    from nexus.tuplespace.index import TupleIndex
    return TupleIndex.from_registry(registry, chroma_client)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _valid_task_dims() -> dict[str, Any]:
    return {"status": "open", "priority": "P1", "created_by": "agent-X"}


def _valid_lock_dims() -> dict[str, Any]:
    return {"resource": "db/write", "holder": "agent-A"}


# ---------------------------------------------------------------------------
# out — valid dimensions
# ---------------------------------------------------------------------------

class TestOut:
    def test_out_returns_tuple_id(self, db_conn, index, registry):
        from nexus.tuplespace.api import out

        tid = out(
            conn=db_conn,
            index=index,
            registry=registry,
            subspace="tasks/nexus",
            content="Fix the segfault in the parser",
            dimensions=_valid_task_dims(),
        )
        assert isinstance(tid, str)
        assert len(tid) == 32

    def test_out_inserts_into_sqlite(self, db_conn, index, registry):
        from nexus.tuplespace.api import out

        tid = out(
            conn=db_conn,
            index=index,
            registry=registry,
            subspace="tasks/nexus",
            content="Fix the segfault in the parser",
            dimensions=_valid_task_dims(),
        )
        row = db_conn.execute("SELECT * FROM tuples WHERE id = ?", (tid,)).fetchone()
        assert row is not None
        assert row["subspace"] == "tasks/nexus"
        assert row["template_name"] == "tasks/<project>"
        assert row["content"] == "Fix the segfault in the parser"
        assert row["consumed_at"] is None
        assert row["claim_state"] is None

    def test_out_idempotent_same_content(self, db_conn, index, registry):
        from nexus.tuplespace.api import out

        tid1 = out(
            conn=db_conn, index=index, registry=registry,
            subspace="tasks/nexus",
            content="same content",
            dimensions=_valid_task_dims(),
        )
        tid2 = out(
            conn=db_conn, index=index, registry=registry,
            subspace="tasks/nexus",
            content="same content",
            dimensions=_valid_task_dims(),
        )
        assert tid1 == tid2
        rows = db_conn.execute("SELECT count(*) FROM tuples WHERE id = ?", (tid1,)).fetchone()
        assert rows[0] == 1

    def test_out_different_content_yields_different_id(self, db_conn, index, registry):
        from nexus.tuplespace.api import out

        tid1 = out(
            conn=db_conn, index=index, registry=registry,
            subspace="tasks/nexus", content="task A",
            dimensions=_valid_task_dims(),
        )
        tid2 = out(
            conn=db_conn, index=index, registry=registry,
            subspace="tasks/nexus", content="task B",
            dimensions=_valid_task_dims(),
        )
        assert tid1 != tid2

    def test_out_invalid_dimensions_raises(self, db_conn, index, registry):
        from nexus.tuplespace.api import SubspaceSchemaError, out

        with pytest.raises(SubspaceSchemaError):
            out(
                conn=db_conn, index=index, registry=registry,
                subspace="tasks/nexus",
                content="something",
                dimensions={"status": "invalid_status", "priority": "P1", "created_by": "x"},
            )

    def test_out_missing_required_dimension_raises(self, db_conn, index, registry):
        from nexus.tuplespace.api import SubspaceSchemaError, out

        with pytest.raises(SubspaceSchemaError):
            out(
                conn=db_conn, index=index, registry=registry,
                subspace="tasks/nexus",
                content="something",
                # missing 'priority' and 'created_by'
                dimensions={"status": "open"},
            )

    def test_out_unknown_subspace_raises(self, db_conn, index, registry):
        from nexus.tuplespace.registry import UnknownSubspaceError
        from nexus.tuplespace.api import out

        with pytest.raises(UnknownSubspaceError):
            out(
                conn=db_conn, index=index, registry=registry,
                subspace="unknown/xyz",
                content="something",
                dimensions={},
            )


# ---------------------------------------------------------------------------
# out — embed_from validation
# ---------------------------------------------------------------------------

class TestOutEmbedFrom:
    def test_embed_from_content_is_valid(self, db_conn, index, registry):
        """embed_from='content' is the default and always valid."""
        from nexus.tuplespace.api import out

        tid = out(
            conn=db_conn, index=index, registry=registry,
            subspace="tasks/nexus",
            content="some work item",
            dimensions=_valid_task_dims(),
        )
        assert isinstance(tid, str)

    def test_embed_from_match_text_is_valid(self, db_conn, index, registry):
        """embed_from='match_text' valid when match_text is provided."""
        from nexus.tuplespace.api import out

        # 'plans' subspace uses embed_from: match_text
        tid = out(
            conn=db_conn, index=index, registry=registry,
            subspace="plans",
            content="plan body",
            dimensions={"query_hash": "abc123"},
            match_text="search query for semantic retrieval",
        )
        assert isinstance(tid, str)

    def test_embed_from_dimensions_key_is_valid(self, db_conn, index, registry):
        """embed_from='dimensions:priority' valid when 'priority' exists in schema."""
        from nexus.tuplespace.api import out

        # 'signals/<channel>' uses embed_from: dimensions:priority
        tid = out(
            conn=db_conn, index=index, registry=registry,
            subspace="signals/alerts",
            content="signal body",
            dimensions={"priority": "HIGH", "note": "watch this"},
        )
        assert isinstance(tid, str)

    def test_embed_from_invalid_bare_string_raises(self, db_conn, index, registry):
        """embed_from values that are not 'content', 'match_text', or 'dimensions:<key>'."""
        from nexus.tuplespace.api import SubspaceSchemaError, out

        # Temporarily patch a schema with an invalid embed_from.
        # We test this via the validation helper directly.
        from nexus.tuplespace.api import _validate_embed_from

        with pytest.raises(SubspaceSchemaError, match="embed_from"):
            _validate_embed_from("body", dimensions={"status": "open"})

    def test_embed_from_dimensions_missing_key_raises(self, db_conn, index, registry):
        """dimensions:<key> where key does not exist in schema dimensions."""
        from nexus.tuplespace.api import SubspaceSchemaError, _validate_embed_from

        with pytest.raises(SubspaceSchemaError, match="embed_from"):
            _validate_embed_from("dimensions:nonexistent", dimensions={"status": "open"})

    def test_embed_from_dimensions_prefix_without_key_raises(self, db_conn, index, registry):
        """'dimensions:' with empty key is invalid."""
        from nexus.tuplespace.api import SubspaceSchemaError, _validate_embed_from

        with pytest.raises(SubspaceSchemaError, match="embed_from"):
            _validate_embed_from("dimensions:", dimensions={"status": "open"})


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------

class TestRead:
    def test_read_returns_matching_tuples(self, db_conn, index, registry):
        from nexus.tuplespace.api import out, read

        out(conn=db_conn, index=index, registry=registry,
            subspace="tasks/nexus", content="fix the memory leak",
            dimensions=_valid_task_dims())

        results = read(
            conn=db_conn, index=index, registry=registry,
            subspace="tasks/nexus",
            query="memory leak bug",
        )
        assert len(results) >= 1
        assert results[0]["content"] == "fix the memory leak"

    def test_read_filters_by_where(self, db_conn, index, registry):
        from nexus.tuplespace.api import out, read

        dims_open = {"status": "open", "priority": "P1", "created_by": "x"}
        dims_done = {"status": "done", "priority": "P1", "created_by": "x"}
        out(conn=db_conn, index=index, registry=registry,
            subspace="tasks/nexus", content="open task fix the bug",
            dimensions=dims_open)
        out(conn=db_conn, index=index, registry=registry,
            subspace="tasks/nexus", content="done task fixed the bug",
            dimensions=dims_done)

        results = read(
            conn=db_conn, index=index, registry=registry,
            subspace="tasks/nexus",
            query="fix the bug",
            where={"status": {"$eq": "open"}},
        )
        statuses = {r["dimensions"].get("status") for r in results}
        assert statuses == {"open"}

    def test_read_excludes_consumed_tuples(self, db_conn, index, registry):
        from nexus.tuplespace.api import out, read

        tid = out(conn=db_conn, index=index, registry=registry,
                  subspace="tasks/nexus", content="consumed task remove me",
                  dimensions=_valid_task_dims())

        # Manually mark consumed
        db_conn.execute(
            "UPDATE tuples SET consumed_at = ?, consumed_by = ? WHERE id = ?",
            (time.time(), "agent-Z", tid),
        )
        db_conn.commit()

        results = read(
            conn=db_conn, index=index, registry=registry,
            subspace="tasks/nexus", query="consumed task remove me",
        )
        ids = [r["id"] for r in results]
        assert tid not in ids

    def test_read_excludes_claimed_tuples(self, db_conn, index, registry):
        from nexus.tuplespace.api import out, read

        tid = out(conn=db_conn, index=index, registry=registry,
                  subspace="tasks/nexus", content="claimed task do not read",
                  dimensions=_valid_task_dims())

        # Manually mark claimed
        db_conn.execute(
            "UPDATE tuples SET claim_state='claimed', claimant='agent-A', "
            "claim_id='cid-123', claim_expires_at=? WHERE id = ?",
            (time.time() + 60, tid),
        )
        db_conn.commit()

        results = read(
            conn=db_conn, index=index, registry=registry,
            subspace="tasks/nexus", query="claimed task do not read",
        )
        ids = [r["id"] for r in results]
        assert tid not in ids

    def test_read_returns_empty_when_no_match(self, db_conn, index, registry):
        from nexus.tuplespace.api import read

        results = read(
            conn=db_conn, index=index, registry=registry,
            subspace="tasks/nexus", query="no tuples here",
        )
        assert results == []


# ---------------------------------------------------------------------------
# take — semantic mode
# ---------------------------------------------------------------------------

class TestTakeSemantic:
    def test_take_returns_tuple_and_claim_id(self, db_conn, index, registry):
        from nexus.tuplespace.api import out, take

        out(conn=db_conn, index=index, registry=registry,
            subspace="tasks/nexus", content="fix the memory leak in allocator",
            dimensions=_valid_task_dims())

        result = take(
            conn=db_conn, index=index, registry=registry,
            subspace="tasks/nexus",
            query="memory leak allocator",
            claimant="agent-A",
        )
        assert result is not None
        t, claim_id = result
        assert isinstance(claim_id, str)
        assert t["subspace"] == "tasks/nexus"

    def test_take_marks_tuple_claimed(self, db_conn, index, registry):
        from nexus.tuplespace.api import out, take

        tid = out(conn=db_conn, index=index, registry=registry,
                  subspace="tasks/nexus", content="fix the memory leak in allocator",
                  dimensions=_valid_task_dims())

        take(conn=db_conn, index=index, registry=registry,
             subspace="tasks/nexus", query="memory leak allocator",
             claimant="agent-A")

        row = db_conn.execute("SELECT claim_state, claimant FROM tuples WHERE id = ?", (tid,)).fetchone()
        assert row["claim_state"] == "claimed"
        assert row["claimant"] == "agent-A"

    def test_take_writes_claim_log_entry(self, db_conn, index, registry):
        from nexus.tuplespace.api import out, take

        tid = out(conn=db_conn, index=index, registry=registry,
                  subspace="tasks/nexus", content="task requiring claim log",
                  dimensions=_valid_task_dims())

        result = take(conn=db_conn, index=index, registry=registry,
                      subspace="tasks/nexus", query="task requiring claim log",
                      claimant="agent-A")
        assert result is not None
        _, claim_id = result

        log = db_conn.execute(
            "SELECT * FROM tuple_claim_log WHERE tuple_id = ? AND transition = 'claim'",
            (tid,),
        ).fetchone()
        assert log is not None
        assert log["claimant"] == "agent-A"
        assert log["claim_id"] == claim_id

    def test_take_returns_none_when_no_candidate(self, db_conn, index, registry):
        from nexus.tuplespace.api import take

        result = take(
            conn=db_conn, index=index, registry=registry,
            subspace="tasks/nexus", query="something",
            claimant="agent-A",
        )
        assert result is None

    def test_take_respects_floor_margin_top1(self, db_conn, index, registry):
        """Only one candidate: margin not needed, floor must be met."""
        from nexus.tuplespace.api import out, take

        out(conn=db_conn, index=index, registry=registry,
            subspace="tasks/nexus", content="unique task for floor test",
            dimensions=_valid_task_dims())

        # With a high floor, the match may fail
        result = take(
            conn=db_conn, index=index, registry=registry,
            subspace="tasks/nexus", query="unique task for floor test",
            claimant="agent-A",
            floor=0.01,  # very permissive floor
        )
        # Should succeed with very permissive floor
        assert result is not None

    def test_take_does_not_return_consumed_tuple(self, db_conn, index, registry):
        from nexus.tuplespace.api import out, take

        tid = out(conn=db_conn, index=index, registry=registry,
                  subspace="tasks/nexus", content="already consumed task",
                  dimensions=_valid_task_dims())
        db_conn.execute(
            "UPDATE tuples SET consumed_at = ?, consumed_by = ? WHERE id = ?",
            (time.time(), "agent-Z", tid),
        )
        db_conn.commit()

        result = take(conn=db_conn, index=index, registry=registry,
                      subspace="tasks/nexus", query="already consumed task",
                      claimant="agent-B")
        assert result is None

    def test_take_block_true_raises_in_direct_mode(self, db_conn, index, registry):
        from nexus.tuplespace.api import BlockingNotSupported, take

        with pytest.raises(BlockingNotSupported):
            take(conn=db_conn, index=index, registry=registry,
                 subspace="tasks/nexus", query="something",
                 claimant="agent-A",
                 block=True)


# ---------------------------------------------------------------------------
# take — CAS race (exactly-one-winner)
# ---------------------------------------------------------------------------

class TestTakeCASRace:
    def test_exactly_one_winner_in_concurrent_take(self, tmp_path, index, registry):
        """Two claimants race on the same tuple — exactly one wins.

        The test uses a threading.Barrier(2) injected via the module-level
        _take_pre_update_hook to synchronise both threads after candidate
        selection and before the UPDATE. The single-statement CAS guarantees
        exactly one RETURNING row.
        """
        import nexus.tuplespace.api as api_mod
        from nexus.tuplespace.api import out, take
        from nexus.tuplespace.store import open_tuples_db

        # Two separate connections to the same file (WAL handles concurrent access).
        # Both use check_same_thread=False because they are handed off to worker threads.
        db_path = tmp_path / "race.db"
        # Bootstrap schema via a temporary connection, then close it.
        _bootstrap = open_tuples_db(db_path)
        _bootstrap.close()
        conn_a = sqlite3.connect(str(db_path), check_same_thread=False)
        conn_a.row_factory = sqlite3.Row
        conn_a.execute("PRAGMA journal_mode=WAL")
        conn_a.commit()
        conn_b = sqlite3.connect(str(db_path), check_same_thread=False)
        conn_b.row_factory = sqlite3.Row
        conn_b.execute("PRAGMA journal_mode=WAL")
        conn_b.commit()

        # Insert one tuple using conn_a
        tid = out(
            conn=conn_a, index=index, registry=registry,
            subspace="tasks/nexus", content="race condition fix task",
            dimensions=_valid_task_dims(),
        )
        assert isinstance(tid, str)

        barrier = threading.Barrier(2, timeout=5.0)

        def hook():
            barrier.wait()

        api_mod._take_pre_update_hook = hook

        results: list[Any] = []
        errors: list[BaseException] = []

        def worker(conn, claimant):
            try:
                r = take(
                    conn=conn, index=index, registry=registry,
                    subspace="tasks/nexus",
                    query="race condition fix task",
                    claimant=claimant,
                )
                results.append(r)
            except BaseException as exc:
                errors.append(exc)

        t1 = threading.Thread(target=worker, args=(conn_a, "agent-A"), daemon=True)
        t2 = threading.Thread(target=worker, args=(conn_b, "agent-B"), daemon=True)
        t1.start()
        t2.start()
        t1.join(timeout=10.0)
        t2.join(timeout=10.0)

        api_mod._take_pre_update_hook = None
        conn_a.close()
        conn_b.close()

        assert not errors, f"Unexpected errors: {errors}"
        wins = [r for r in results if r is not None]
        losses = [r for r in results if r is None]
        assert len(wins) == 1, f"Expected exactly 1 winner, got {len(wins)}"
        assert len(losses) == 1, f"Expected exactly 1 loser, got {len(losses)}"


# ---------------------------------------------------------------------------
# take — exact mode (locks/<resource>)
# ---------------------------------------------------------------------------

class TestTakeExact:
    def test_take_exact_claims_by_match_key(self, db_conn, index, registry):
        from nexus.tuplespace.api import out, take

        out(conn=db_conn, index=index, registry=registry,
            subspace="locks/db-write",
            content="lock for db write",
            dimensions={"resource": "db/write", "holder": "agent-A"})

        result = take(
            conn=db_conn, index=index, registry=registry,
            subspace="locks/db-write",
            query="",  # unused in exact mode
            claimant="agent-A",
            where={"resource": "db/write"},
        )
        assert result is not None

    def test_take_exact_missing_match_key_raises(self, db_conn, index, registry):
        from nexus.tuplespace.api import SubspaceSchemaError, take

        with pytest.raises(SubspaceSchemaError, match="match_key"):
            take(
                conn=db_conn, index=index, registry=registry,
                subspace="locks/db-write",
                query="",
                claimant="agent-A",
                where={"holder": "agent-A"},  # 'resource' match_key missing
            )

    def test_take_exact_returns_none_when_already_claimed(self, db_conn, index, registry):
        from nexus.tuplespace.api import out, take

        tid = out(conn=db_conn, index=index, registry=registry,
                  subspace="locks/db-write",
                  content="lock for db write",
                  dimensions={"resource": "db/write", "holder": "agent-A"})

        # First take: wins
        r1 = take(conn=db_conn, index=index, registry=registry,
                  subspace="locks/db-write", query="",
                  claimant="agent-A", where={"resource": "db/write"})
        assert r1 is not None

        # Second take different claimant: loses
        r2 = take(conn=db_conn, index=index, registry=registry,
                  subspace="locks/db-write", query="",
                  claimant="agent-B", where={"resource": "db/write"})
        assert r2 is None


# ---------------------------------------------------------------------------
# ack
# ---------------------------------------------------------------------------

class TestAck:
    def test_ack_marks_consumed_and_writes_log(self, db_conn, index, registry):
        from nexus.tuplespace.api import ack, out, take

        tid = out(conn=db_conn, index=index, registry=registry,
                  subspace="tasks/nexus", content="task to ack",
                  dimensions=_valid_task_dims())

        result = take(conn=db_conn, index=index, registry=registry,
                      subspace="tasks/nexus", query="task to ack",
                      claimant="agent-A")
        assert result is not None
        _, claim_id = result

        ack(conn=db_conn, claim_id=claim_id, claimant="agent-A")

        row = db_conn.execute("SELECT consumed_at, consumed_by, claim_state FROM tuples WHERE id = ?", (tid,)).fetchone()
        assert row["consumed_at"] is not None
        assert row["consumed_by"] == "agent-A"
        assert row["claim_state"] == "acked"

        log = db_conn.execute(
            "SELECT * FROM tuple_claim_log WHERE claim_id = ? AND transition = 'ack'",
            (claim_id,),
        ).fetchone()
        assert log is not None
        assert log["claimant"] == "agent-A"
        assert log["transition"] == "ack"

    def test_ack_wrong_claimant_raises(self, db_conn, index, registry):
        from nexus.tuplespace.api import ClaimOwnershipError, ack, out, take

        out(conn=db_conn, index=index, registry=registry,
            subspace="tasks/nexus", content="task wrong ack",
            dimensions=_valid_task_dims())

        result = take(conn=db_conn, index=index, registry=registry,
                      subspace="tasks/nexus", query="task wrong ack",
                      claimant="agent-A")
        assert result is not None
        _, claim_id = result

        with pytest.raises(ClaimOwnershipError):
            ack(conn=db_conn, claim_id=claim_id, claimant="agent-B")


# ---------------------------------------------------------------------------
# nack
# ---------------------------------------------------------------------------

class TestNack:
    def test_nack_releases_claim_and_writes_log(self, db_conn, index, registry):
        from nexus.tuplespace.api import nack, out, take

        tid = out(conn=db_conn, index=index, registry=registry,
                  subspace="tasks/nexus", content="task to nack",
                  dimensions=_valid_task_dims())

        result = take(conn=db_conn, index=index, registry=registry,
                      subspace="tasks/nexus", query="task to nack",
                      claimant="agent-A")
        assert result is not None
        _, claim_id = result

        nack(conn=db_conn, claim_id=claim_id, claimant="agent-A")

        row = db_conn.execute(
            "SELECT claim_state, claim_id FROM tuples WHERE id = ?", (tid,)
        ).fetchone()
        assert row["claim_state"] is None
        assert row["claim_id"] is None

        log = db_conn.execute(
            "SELECT * FROM tuple_claim_log WHERE claim_id = ? AND transition = 'nack'",
            (claim_id,),
        ).fetchone()
        assert log is not None
        assert log["claimant"] == "agent-A"
        assert log["transition"] == "nack"

    def test_nack_wrong_claimant_raises(self, db_conn, index, registry):
        from nexus.tuplespace.api import ClaimOwnershipError, nack, out, take

        out(conn=db_conn, index=index, registry=registry,
            subspace="tasks/nexus", content="task wrong nack",
            dimensions=_valid_task_dims())

        result = take(conn=db_conn, index=index, registry=registry,
                      subspace="tasks/nexus", query="task wrong nack",
                      claimant="agent-A")
        assert result is not None
        _, claim_id = result

        with pytest.raises(ClaimOwnershipError):
            nack(conn=db_conn, claim_id=claim_id, claimant="agent-B")


# ---------------------------------------------------------------------------
# list_subspaces, subspace_schema, subspace_stats
# ---------------------------------------------------------------------------

class TestMetadataOps:
    def test_list_subspaces_returns_all_template_names(self, registry):
        from nexus.tuplespace.api import list_subspaces

        names = list_subspaces(registry=registry)
        assert isinstance(names, list)
        assert "tasks/<project>" in names
        assert "locks/<resource>" in names

    def test_subspace_schema_returns_dict(self, registry):
        from nexus.tuplespace.api import subspace_schema

        result = subspace_schema(registry=registry, subspace="tasks/nexus")
        assert isinstance(result, dict)
        assert result["name"] == "tasks/<project>"
        assert "dimensions" in result
        assert "take" in result
        assert "read" in result

    def test_subspace_schema_unknown_raises(self, registry):
        from nexus.tuplespace.api import subspace_schema
        from nexus.tuplespace.registry import UnknownSubspaceError

        with pytest.raises(UnknownSubspaceError):
            subspace_schema(registry=registry, subspace="unknown/xyz")

    def test_subspace_stats_returns_counts(self, db_conn, index, registry):
        from nexus.tuplespace.api import out, subspace_stats

        out(conn=db_conn, index=index, registry=registry,
            subspace="tasks/nexus", content="stat task one",
            dimensions=_valid_task_dims())
        out(conn=db_conn, index=index, registry=registry,
            subspace="tasks/nexus", content="stat task two",
            dimensions={**_valid_task_dims(), "status": "done"})

        stats = subspace_stats(conn=db_conn, subspace="tasks/nexus")
        assert isinstance(stats, dict)
        assert stats["total"] == 2
        assert stats["available"] >= 1
        assert stats["consumed"] == 0
        assert stats["claimed"] == 0

    def test_subspace_stats_empty_returns_zeros(self, db_conn, registry):
        from nexus.tuplespace.api import subspace_stats

        stats = subspace_stats(conn=db_conn, subspace="tasks/nexus")
        assert stats["total"] == 0
        assert stats["available"] == 0
