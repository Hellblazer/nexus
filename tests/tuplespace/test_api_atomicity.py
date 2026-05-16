# SPDX-License-Identifier: Apache-2.0
"""Atomicity tests for api.out — nexus-qmrr (RDR-111).

Invariant: SQLite presence implies Chroma presence. If the Chroma upsert
fails, no SQLite row exists for that tuple_id. Achieved by ordering the
Chroma write before the SQLite commit (mirrors the retention sweeper's
asymmetric ordering, which deletes from Chroma first so a crash leaves
recoverable SQLite orphans).
"""

from __future__ import annotations

import sqlite3
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


def _valid_task_dims() -> dict[str, Any]:
    return {"status": "open", "priority": "P1", "created_by": "agent-X"}


class TestOutAtomicity:
    """nexus-qmrr: api.out must not commit SQLite if Chroma write fails."""

    def test_happy_path_writes_both_stores(self, db_conn, index, registry):
        """Sanity: successful out() leaves both SQLite row and Chroma record."""
        from nexus.tuplespace.api import out

        tid = out(
            conn=db_conn, index=index, registry=registry,
            subspace="tasks/nexus",
            content="hello",
            dimensions=_valid_task_dims(),
        )

        row = db_conn.execute(
            "SELECT id FROM tuples WHERE id = ?", (tid,)
        ).fetchone()
        assert row is not None, "SQLite row must exist on success"

        chroma_results = index.read(
            template_name="tasks/<project>",
            subspace="tasks/nexus",
            query="hello",
            n_results=5,
        )
        assert any(r["id"] == tid for r in chroma_results), \
            "Chroma record must exist on success"

    def test_chroma_failure_leaves_no_sqlite_row(
        self, db_conn, index, registry, monkeypatch
    ):
        """If Chroma upsert raises, SQLite must have no row for that tuple_id."""
        from nexus.tuplespace.api import out

        def boom(**_kwargs):
            raise RuntimeError("simulated chroma outage")

        monkeypatch.setattr(index, "out", boom)

        with pytest.raises(RuntimeError, match="simulated chroma outage"):
            out(
                conn=db_conn, index=index, registry=registry,
                subspace="tasks/nexus",
                content="failed write",
                dimensions=_valid_task_dims(),
            )

        # The whole point of nexus-qmrr: SQLite must not have the row.
        rows = db_conn.execute(
            "SELECT id FROM tuples WHERE content = ?", ("failed write",)
        ).fetchall()
        assert rows == [], (
            "Two-store invariant violated: SQLite row exists but Chroma "
            "write failed. nexus-qmrr regression."
        )

    def test_sqlite_failure_after_chroma_leaves_orphan_chroma(
        self, db_conn, index, registry
    ):
        """nexus-11m2: ordering asymmetry — SQLite fails AFTER Chroma succeeded.

        The current contract documented at ``src/nexus/tuplespace/api.py:415-424``
        explicitly accepts an orphan Chroma record on this failure mode:
        Chroma upserts are idempotent on tuple_id, so the next refire
        reclaims the orphan, and the retention sweeper picks up
        stragglers. This test locks both halves of that contract.
        """
        from nexus.tuplespace.api import out

        captured: dict[str, str] = {}

        flaky = _FlakyConn(
            db_conn,
            fail_on_prefix="INSERT INTO tuples",
            tid_sink=captured,
        )

        with pytest.raises(sqlite3.OperationalError, match="simulated sqlite outage"):
            out(
                conn=flaky, index=index, registry=registry,
                subspace="tasks/nexus",
                content="sqlite-fails-after-chroma",
                dimensions=_valid_task_dims(),
            )

        # 1. SQLite has no row for the failed tuple_id.
        rows = db_conn.execute(
            "SELECT id FROM tuples WHERE content = ?",
            ("sqlite-fails-after-chroma",),
        ).fetchall()
        assert rows == [], (
            "Failed SQLite write must not leave a partial row in tuples."
        )

        # 2. Chroma DOES contain the tuple — the documented orphan state.
        # Recover the tuple_id from index.out parameters captured by FlakyConn.
        assert "tid" in captured, (
            "index.out must have run before SQLite tripped; otherwise the "
            "ordering being tested is reversed."
        )
        collection = index._collections["tasks/<project>"]
        chroma_get = collection.get(ids=[captured["tid"]])
        assert chroma_get["ids"] == [captured["tid"]], (
            "Chroma orphan-accept contract: the prior-write upsert must "
            "survive the SQLite failure (nexus-qmrr documented behaviour)."
        )

    def test_refire_after_sqlite_failure_recovers(
        self, db_conn, index, registry
    ):
        """nexus-11m2 follow-on: a successful refire reclaims the orphan."""
        from nexus.tuplespace.api import out

        captured: dict[str, str] = {}
        flaky = _FlakyConn(
            db_conn,
            fail_on_prefix="INSERT INTO tuples",
            tid_sink=captured,
        )

        with pytest.raises(sqlite3.OperationalError):
            out(
                conn=flaky, index=index, registry=registry,
                subspace="tasks/nexus",
                content="orphan-then-recover",
                dimensions=_valid_task_dims(),
            )

        # Second fire with identical args via the real (healthy) connection.
        # Chroma upsert is idempotent on the deterministic tuple_id; SQLite
        # gets its row this time.
        tid_second = out(
            conn=db_conn, index=index, registry=registry,
            subspace="tasks/nexus",
            content="orphan-then-recover",
            dimensions=_valid_task_dims(),
        )

        rows = db_conn.execute(
            "SELECT id FROM tuples WHERE id = ?", (tid_second,)
        ).fetchall()
        assert len(rows) == 1, "refire must produce exactly one SQLite row"

        collection = index._collections["tasks/<project>"]
        chroma_get = collection.get(ids=[tid_second])
        assert chroma_get["ids"] == [tid_second]


class _FlakyConn:
    """Wrapper that proxies a real ``sqlite3.Connection`` and trips one path.

    sqlite3.Connection's execute is a read-only built-in attribute, so
    monkeypatching it raises. Wrapping in a proxy gives us a Python-level
    method we can override while keeping every other call (commit,
    rollback, row_factory, etc.) delegating to the real connection.
    """

    def __init__(
        self,
        real: sqlite3.Connection,
        *,
        fail_on_prefix: str,
        tid_sink: dict[str, str] | None = None,
    ) -> None:
        self._real = real
        self._fail_on_prefix = fail_on_prefix
        self._tid_sink = tid_sink

    def execute(self, sql, *args, **kwargs):
        # Capture the tuple_id parameter for the asserting side. The INSERT
        # statement's first positional bind value is the tuple_id.
        if (
            self._tid_sink is not None
            and isinstance(sql, str)
            and sql.strip().startswith("INSERT INTO tuples")
        ):
            if args and isinstance(args[0], (tuple, list)) and args[0]:
                self._tid_sink["tid"] = args[0][0]
        if isinstance(sql, str) and sql.strip().startswith(self._fail_on_prefix):
            raise sqlite3.OperationalError("simulated sqlite outage")
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)
