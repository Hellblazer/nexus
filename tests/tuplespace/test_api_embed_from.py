# SPDX-License-Identifier: Apache-2.0
"""embed_from fail-loud tests — nexus-zm2n (RDR-111).

When a subspace declares ``embed_from: match_text``, callers MUST supply
a non-empty ``match_text``. Silently falling back to ``content`` violates
the project rule "no silent fallbacks for data-correctness problems"
(feedback_no_silent_fallbacks_for_correctness.md): downstream semantic
retrieval would index against the wrong text and silently degrade recall.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import chromadb
import pytest


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
    (d / "plans.yml").write_text(_PLANS_YAML)
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


class TestEmbedFromFailLoud:
    """nexus-zm2n: empty match_text on embed_from=match_text must raise."""

    def test_empty_match_text_raises_on_match_text_schema(
        self, db_conn, index, registry
    ):
        """match_text=None when schema says embed_from=match_text -> raise."""
        from nexus.tuplespace.api import SubspaceSchemaError, out

        with pytest.raises(SubspaceSchemaError) as excinfo:
            out(
                conn=db_conn, index=index, registry=registry,
                subspace="plans",
                content="plan body content",
                dimensions={"query_hash": "abc123"},
                match_text=None,
            )

        msg = str(excinfo.value)
        assert "plans" in msg, "error must name the subspace"
        assert "match_text" in msg, "error must name the misconfigured field"

    def test_blank_match_text_raises_on_match_text_schema(
        self, db_conn, index, registry
    ):
        """Empty-string match_text is just as bad as None."""
        from nexus.tuplespace.api import SubspaceSchemaError, out

        with pytest.raises(SubspaceSchemaError, match="match_text"):
            out(
                conn=db_conn, index=index, registry=registry,
                subspace="plans",
                content="plan body content",
                dimensions={"query_hash": "abc123"},
                match_text="",
            )

    def test_nonempty_match_text_succeeds_on_match_text_schema(
        self, db_conn, index, registry
    ):
        """Happy path: a real match_text embeds normally."""
        from nexus.tuplespace.api import out

        tid = out(
            conn=db_conn, index=index, registry=registry,
            subspace="plans",
            content="plan body",
            dimensions={"query_hash": "abc123"},
            match_text="search query for semantic retrieval",
        )
        assert isinstance(tid, str)

    def test_embed_from_content_does_not_require_match_text(
        self, db_conn, index, registry
    ):
        """embed_from=content schemas never look at match_text — must still work."""
        from nexus.tuplespace.api import out

        tid = out(
            conn=db_conn, index=index, registry=registry,
            subspace="tasks/nexus",
            content="some task body",
            dimensions={
                "status": "open",
                "priority": "P1",
                "created_by": "agent-X",
            },
            match_text=None,
        )
        assert isinstance(tid, str)
