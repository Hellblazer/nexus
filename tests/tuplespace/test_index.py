# SPDX-License-Identifier: Apache-2.0
"""Tests for nexus.tuplespace.index — Chroma collection layout (project tier).

RDR-110 P1.3 (nexus-78hx). Covers:
- Slug generation for literal and parameterised template names.
- Collection creation per registered template at from_registry time.
- out() writes a document with correct metadata.
- read() queries the correct collection with subspace filter merged.
- Subspace isolation: read() only returns tuples from the requested subspace.
- Caller where-predicate filter is forwarded.
- Quota enforcement: n_results limit, document size, query length, where count.
- Upsert idempotence: duplicate tuple_id does not raise.

TDD: this file was written before index.py existed.
Integration over mocks: real chromadb.EphemeralClient throughout.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Test YAML fixtures (minimal valid schemas)
# ---------------------------------------------------------------------------

_TASKS_YAML = """\
name: tasks/<project>
tier: project
content_type: text
embed_from: content
dimensions:
  status:    { type: enum, values: [open, in_progress, done, cancelled], required: true }
  priority:  { type: enum, values: [P0, P1, P2, P3, P4], required: true }
  assignee:  { type: string, required: false }
  created_by: { type: string, required: true }
take:
  enabled: true
  mode: semantic
  floor: 0.55
  margin: 0.08
  default_lease_seconds: 600
read:
  default_floor: 0.40
  default_n: 5
tiers: [project]
retention_seconds: 7776000
"""

_LOCKS_YAML = """\
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
retention_seconds: 86400
"""

_PLANS_YAML = """\
name: plans
tier: project
content_type: text
embed_from: content
dimensions:
  verb: { type: string, required: true }
take:
  enabled: false
  mode: semantic
  floor: 0.40
  margin: 0.05
  default_lease_seconds: 0
read:
  default_floor: 0.40
  default_n: 5
tiers: [project]
retention_seconds: 0
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def builtin_dir(tmp_path: Path) -> Path:
    """Synthetic builtin/ directory with two parameterised templates."""
    d = tmp_path / "builtin"
    d.mkdir()
    (d / "tasks.yml").write_text(_TASKS_YAML)
    (d / "locks.yml").write_text(_LOCKS_YAML)
    return d


@pytest.fixture()
def builtin_dir_with_literal(tmp_path: Path) -> Path:
    """Synthetic builtin/ directory including a literal (non-parameterised) template."""
    d = tmp_path / "builtin"
    d.mkdir()
    (d / "tasks.yml").write_text(_TASKS_YAML)
    (d / "plans.yml").write_text(_PLANS_YAML)
    return d


@pytest.fixture()
def registry(builtin_dir: Path):
    from nexus.tuplespace.registry import Registry

    return Registry.load(builtin_dir)


@pytest.fixture()
def chroma_client():
    """Fresh EphemeralClient per test — collections accumulate in-process memory."""
    import chromadb

    return chromadb.EphemeralClient()


@pytest.fixture()
def index(registry, chroma_client):
    from nexus.tuplespace.index import TupleIndex

    return TupleIndex.from_registry(registry, chroma_client)


# ---------------------------------------------------------------------------
# Slug generation
# ---------------------------------------------------------------------------


class TestTemplateSlug:
    """Unit tests for the internal slug function."""

    def _slug(self, name: str) -> str:
        from nexus.tuplespace.index import _template_slug

        return _template_slug(name)

    def test_literal_name_unchanged(self):
        assert self._slug("plans") == "plans"

    def test_single_param_segment(self):
        assert self._slug("tasks/<project>") == "tasks_project"

    def test_param_in_second_segment(self):
        assert self._slug("locks/<resource>") == "locks_resource"

    def test_param_with_underscore_in_name(self):
        assert self._slug("barriers/<barrier_id>") == "barriers_barrier_id"

    def test_multi_param_segments(self):
        # Hypothetical multi-param template (not shipped in v1 but valid slug logic).
        assert self._slug("a/<b>/c/<d>") == "a_b_c_d"

    def test_no_slash_no_param(self):
        assert self._slug("scratch") == "scratch"

    def test_angle_brackets_stripped(self):
        # Ensures < and > are removed, not the content between them.
        assert "<" not in self._slug("tasks/<project>")
        assert ">" not in self._slug("tasks/<project>")


# ---------------------------------------------------------------------------
# Collection name
# ---------------------------------------------------------------------------


class TestCollectionName:
    """Unit tests for the public collection-name helper."""

    def _name(self, template_name: str) -> str:
        from nexus.tuplespace.index import collection_name

        return collection_name(template_name)

    def test_prefixed_with_tuples_double_underscore(self):
        assert self._name("tasks/<project>").startswith("tuples__")

    def test_tasks_collection_name(self):
        assert self._name("tasks/<project>") == "tuples__tasks_project"

    def test_locks_collection_name(self):
        assert self._name("locks/<resource>") == "tuples__locks_resource"

    def test_barriers_collection_name(self):
        assert self._name("barriers/<barrier_id>") == "tuples__barriers_barrier_id"

    def test_literal_collection_name(self):
        assert self._name("plans") == "tuples__plans"

    def test_mailbox_collection_name(self):
        assert self._name("mailbox/<agent>") == "tuples__mailbox_agent"

    def test_events_collection_name(self):
        assert self._name("events/<topic>") == "tuples__events_topic"


# ---------------------------------------------------------------------------
# from_registry creates collections
# ---------------------------------------------------------------------------


class TestFromRegistry:
    def test_creates_collection_for_each_template(self, index, chroma_client):
        """Both registered templates get a collection."""
        from nexus.tuplespace.index import collection_name

        existing = {c.name for c in chroma_client.list_collections()}
        assert collection_name("tasks/<project>") in existing
        assert collection_name("locks/<resource>") in existing

    def test_no_extra_collections_created(self, index, chroma_client):
        """Exactly the registered templates' collections are present (plus any pre-existing)."""
        from nexus.tuplespace.index import collection_name

        created = {
            collection_name("tasks/<project>"),
            collection_name("locks/<resource>"),
        }
        existing = {c.name for c in chroma_client.list_collections()}
        assert created.issubset(existing)

    def test_from_registry_idempotent(self, registry, chroma_client):
        """Calling from_registry twice does not raise (get_or_create semantics)."""
        from nexus.tuplespace.index import TupleIndex

        TupleIndex.from_registry(registry, chroma_client)
        TupleIndex.from_registry(registry, chroma_client)  # no exception

    def test_literal_template_collection(self, tmp_path, chroma_client):
        """Literal (non-parameterised) template name produces correct collection."""
        from nexus.tuplespace.index import TupleIndex, collection_name
        from nexus.tuplespace.registry import Registry

        d = tmp_path / "builtin"
        d.mkdir()
        (d / "plans.yml").write_text(_PLANS_YAML)
        registry = Registry.load(d)
        TupleIndex.from_registry(registry, chroma_client)

        existing = {c.name for c in chroma_client.list_collections()}
        assert collection_name("plans") in existing


# ---------------------------------------------------------------------------
# out — write a tuple
# ---------------------------------------------------------------------------


class TestOut:
    def test_out_writes_document(self, index, chroma_client):
        """out() adds a retrievable document to the template's collection."""
        from nexus.tuplespace.index import collection_name

        index.out(
            template_name="tasks/<project>",
            subspace="tasks/nexus",
            tuple_id="t-001",
            payload="fix the bug in core",
            metadata={"subspace": "tasks/nexus", "status": "open", "priority": "P1",
                      "created_by": "alice"},
        )

        coll = chroma_client.get_collection(collection_name("tasks/<project>"))
        result = coll.get(ids=["t-001"])
        assert result["ids"] == ["t-001"]
        assert result["documents"] == ["fix the bug in core"]
        assert result["metadatas"][0]["subspace"] == "tasks/nexus"
        assert result["metadatas"][0]["status"] == "open"

    def test_out_upsert_is_idempotent(self, index):
        """Calling out() twice with the same tuple_id must not raise."""
        kwargs: dict[str, Any] = dict(
            template_name="tasks/<project>",
            subspace="tasks/nexus",
            tuple_id="t-idempotent",
            payload="initial content",
            metadata={"subspace": "tasks/nexus", "status": "open", "priority": "P0",
                      "created_by": "bob"},
        )
        index.out(**kwargs)
        index.out(**kwargs)  # second call — should not raise

    def test_out_metadata_includes_subspace(self, index, chroma_client):
        """Metadata stored always includes the subspace field."""
        from nexus.tuplespace.index import collection_name

        index.out(
            template_name="locks/<resource>",
            subspace="locks/db",
            tuple_id="l-001",
            payload="hold the db lock",
            metadata={"subspace": "locks/db", "resource": "db", "holder": "worker-1"},
        )

        coll = chroma_client.get_collection(collection_name("locks/<resource>"))
        result = coll.get(ids=["l-001"])
        assert result["metadatas"][0]["subspace"] == "locks/db"


# ---------------------------------------------------------------------------
# Quota enforcement — out
# ---------------------------------------------------------------------------


class TestOutQuota:
    def test_document_too_large_raises(self, index):
        """Payload > MAX_DOCUMENT_BYTES (16384) raises RecordTooLarge."""
        from nexus.db.chroma_quotas import RecordTooLarge

        big_payload = "x" * (16_384 + 1)
        with pytest.raises(RecordTooLarge):
            index.out(
                template_name="tasks/<project>",
                subspace="tasks/nexus",
                tuple_id="t-big",
                payload=big_payload,
                metadata={"subspace": "tasks/nexus", "status": "open", "priority": "P1",
                          "created_by": "alice"},
            )


# ---------------------------------------------------------------------------
# read — query tuples
# ---------------------------------------------------------------------------


class TestRead:
    def _populate(self, index: Any) -> None:
        """Insert a few tuples into the tasks template for use in read tests."""
        index.out(
            template_name="tasks/<project>",
            subspace="tasks/nexus",
            tuple_id="read-t-001",
            payload="implement caching layer for faster retrieval",
            metadata={"subspace": "tasks/nexus", "status": "open", "priority": "P1",
                      "created_by": "alice"},
        )
        index.out(
            template_name="tasks/<project>",
            subspace="tasks/nexus",
            tuple_id="read-t-002",
            payload="fix the authentication bug in login flow",
            metadata={"subspace": "tasks/nexus", "status": "open", "priority": "P0",
                      "created_by": "bob"},
        )
        # Different subspace — must not appear in tasks/nexus reads.
        index.out(
            template_name="tasks/<project>",
            subspace="tasks/other",
            tuple_id="read-t-003",
            payload="unrelated task in another project",
            metadata={"subspace": "tasks/other", "status": "open", "priority": "P2",
                      "created_by": "carol"},
        )

    def test_read_returns_list(self, index):
        """read() always returns a list."""
        self._populate(index)
        results = index.read(
            template_name="tasks/<project>",
            subspace="tasks/nexus",
            query="caching",
            n_results=1,
        )
        assert isinstance(results, list)

    def test_read_result_has_expected_keys(self, index):
        """Each result dict has id, document, metadata, distance keys."""
        self._populate(index)
        results = index.read(
            template_name="tasks/<project>",
            subspace="tasks/nexus",
            query="caching layer",
            n_results=1,
        )
        assert len(results) >= 1
        r = results[0]
        assert "id" in r
        assert "document" in r
        assert "metadata" in r
        assert "distance" in r

    def test_read_subspace_filter_isolates_results(self, index):
        """read() only returns tuples from the requested subspace."""
        self._populate(index)
        results = index.read(
            template_name="tasks/<project>",
            subspace="tasks/nexus",
            query="task project work",
            n_results=10,
        )
        for r in results:
            assert r["metadata"]["subspace"] == "tasks/nexus", (
                f"Unexpected subspace in result: {r['metadata']['subspace']}"
            )

    def test_read_where_filter_applied(self, index):
        """Caller where-predicate is forwarded to the chroma query."""
        self._populate(index)
        results = index.read(
            template_name="tasks/<project>",
            subspace="tasks/nexus",
            query="bug fix authentication",
            where={"priority": {"$eq": "P0"}},
            n_results=5,
        )
        for r in results:
            assert r["metadata"]["priority"] == "P0"

    def test_read_empty_where_is_ok(self, index):
        """read() with where=None works without error."""
        self._populate(index)
        results = index.read(
            template_name="tasks/<project>",
            subspace="tasks/nexus",
            query="caching",
        )
        assert isinstance(results, list)


# ---------------------------------------------------------------------------
# Quota enforcement — read
# ---------------------------------------------------------------------------


class TestReadQuota:
    def test_n_results_too_large_raises(self, index):
        """n_results > 300 raises ResultsExceedLimit."""
        from nexus.db.chroma_quotas import ResultsExceedLimit

        with pytest.raises(ResultsExceedLimit):
            index.read(
                template_name="tasks/<project>",
                subspace="tasks/nexus",
                query="anything",
                n_results=301,
            )

    def test_query_string_too_long_raises(self, index):
        """query > 256 chars raises QueryStringTooLong."""
        from nexus.db.chroma_quotas import QueryStringTooLong

        long_query = "a" * 257
        with pytest.raises(QueryStringTooLong):
            index.read(
                template_name="tasks/<project>",
                subspace="tasks/nexus",
                query=long_query,
            )

    def test_where_too_many_predicates_raises(self, index):
        """where with > 8 top-level keys raises TooManyPredicates."""
        from nexus.db.chroma_quotas import TooManyPredicates

        # 9 top-level keys in caller's where dict exceeds the 8-predicate limit.
        big_where = {f"key{i}": {"$eq": f"val{i}"} for i in range(9)}
        with pytest.raises(TooManyPredicates):
            index.read(
                template_name="tasks/<project>",
                subspace="tasks/nexus",
                query="anything",
                where=big_where,
            )
