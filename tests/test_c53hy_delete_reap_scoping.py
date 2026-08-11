# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-c53hy: ``nx store delete --id`` / MCP ``store_delete`` must not
tombstone catalog state before verifying the T3 delete will actually touch
anything.

Round-2 substantive-critic finding on the nexus-o8dil.5 reap-before-delete
reorder (RDR-191 F10c follow-up): both cleanup helpers
(``_reap_catalog_for_doc_ids`` / ``store_delete_catalog_cleanup``) resolve a
chash to its owning catalog document via a GLOBAL chash lookup -- with no
check that the id exists in the TARGET T3 collection, and no check that the
resolved document's ``physical_collection`` matches the collection the
caller named. A ``doc_id`` paired with the WRONG ``--collection``/
``collection`` argument therefore silently tombstones a live, unrelated
catalog document while the caller reports a clean "not found" -- the exact
inverse of the F10c bug class (unconditional destructive action with no
verification).

These tests use a REAL catalog (``ActiveCatalog`` / the live factories) and a
REAL T3 (``T3Database`` over ``InMemoryVectorClient``) so the catalog
mutation is genuinely observable. The pre-existing
``tests/test_store_cmd.py::test_store_delete_by_id`` MagicMock is
structurally blind to it -- it never mocks or observes the catalog-layer
call, so a reap firing on the "not found" path is invisible to it. That gap
is itself part of the nexus-c53hy finding.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest

from click.testing import CliRunner

from nexus.cli import main
from nexus.db.minilm_direct import MiniLMDirectEmbeddingFunction as DefaultEmbeddingFunction
from nexus.db.t3 import T3Database
from nexus.mcp.core import store_delete
from tests.conftest import make_vector_test_client
from tests._catalog_fixture_ops import ActiveCatalog, documents_by_title

# Two DISTINCT, already-conformant physical collection names -- passed
# through t3_collection_name untouched, so there is no ambiguity about
# which physical collection either call targets.
_COL_A = "knowledge__c53hyA__voyage-context-3__v1"
_COL_B = "knowledge__c53hyB__voyage-context-3__v1"


@pytest.fixture
def catalog_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    catalog_dir = tmp_path / "catalog"
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
    return catalog_dir


@pytest.fixture
def local_t3() -> T3Database:
    return T3Database(
        _client=make_vector_test_client(),
        _ef_override=DefaultEmbeddingFunction(),
    )


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _seed_live_doc(local_t3: T3Database, *, collection: str, title: str, content: str) -> str:
    """Write a genuine store_put-shaped document: a T3 chunk in
    *collection* AND its owning catalog row + manifest (physical_collection
    == *collection*), both keyed on the same content-derived chash.
    Returns the chash.
    """
    from nexus.catalog.store_hook import catalog_store_hook_tracked  # noqa: PLC0415 — test-local helper import

    chash = hashlib.sha256(content.encode()).hexdigest()
    local_t3.put(collection=collection, content=content, title=title)
    cat = ActiveCatalog()
    tumbler, _created = catalog_store_hook_tracked(
        title=title, doc_id=chash, collection_name=collection,
    )
    cat.append_manifest_chunks(tumbler, [{"chash": chash, "position": 0}])
    cat.resync_chunk_count_cache(tumbler)
    return chash


class TestMcpStoreDeleteDoesNotReapOnMiss:
    """MCP ``store_delete`` twin of the CLI cases below."""

    def test_wrong_collection_leaves_the_real_owner_untombstoned(
        self, catalog_env: Path, local_t3: T3Database,
    ) -> None:
        """The deterministic case: doc_id is REAL and lives in _COL_A, but
        the caller names _COL_B. The T3 delete legitimately finds nothing
        in _COL_B -- that must NOT destroy the live document that actually
        owns the chash in _COL_A."""
        chash = _seed_live_doc(
            local_t3, collection=_COL_A, title="c53hy-mcp-live",
            content="c53hy mcp wrong-collection live document",
        )
        assert len(documents_by_title("c53hy-mcp-live")) == 1

        with patch("nexus.mcp.core._get_t3", return_value=local_t3):
            result = store_delete(chash, collection=_COL_B)

        assert "not found" in result.lower() or "Not found" in result
        assert len(documents_by_title("c53hy-mcp-live")) == 1, (
            "the catalog row for the document actually owning this chash "
            "(in _COL_A) must survive a delete call that named the WRONG "
            "collection (_COL_B) and found nothing there"
        )

    def test_bogus_doc_id_leaves_every_catalog_document_untombstoned(
        self, catalog_env: Path, local_t3: T3Database,
    ) -> None:
        """A doc_id that matches nothing anywhere must be a pure no-op:
        zero catalog mutation, regardless of what else lives in the
        catalog."""
        _seed_live_doc(
            local_t3, collection=_COL_A, title="c53hy-mcp-bystander",
            content="c53hy mcp innocent bystander document",
        )
        assert len(documents_by_title("c53hy-mcp-bystander")) == 1

        bogus = hashlib.sha256(b"never registered anywhere -- c53hy").hexdigest()
        with patch("nexus.mcp.core._get_t3", return_value=local_t3):
            result = store_delete(bogus, collection=_COL_A)

        assert "not found" in result.lower() or "Not found" in result
        assert len(documents_by_title("c53hy-mcp-bystander")) == 1, (
            "a bogus doc_id must not tombstone an unrelated live document"
        )


class TestCliStoreDeleteDoesNotReapOnMiss:
    """CLI ``nx store delete --id`` twin."""

    def test_wrong_collection_leaves_the_real_owner_untombstoned(
        self, catalog_env: Path, local_t3: T3Database, runner: CliRunner,
    ) -> None:
        chash = _seed_live_doc(
            local_t3, collection=_COL_A, title="c53hy-cli-live",
            content="c53hy cli wrong-collection live document",
        )
        assert len(documents_by_title("c53hy-cli-live")) == 1

        with patch("nexus.commands.store._t3", return_value=local_t3):
            result = runner.invoke(
                main, ["store", "delete", "--collection", _COL_B, "--id", chash],
            )

        assert result.exit_code != 0, result.output
        assert len(documents_by_title("c53hy-cli-live")) == 1, (
            "the catalog row for the document actually owning this chash "
            "(in _COL_A) must survive a delete call that named the WRONG "
            "collection (_COL_B) and found nothing there. CLI output: "
            f"{result.output!r}"
        )

    def test_bogus_doc_id_leaves_every_catalog_document_untombstoned(
        self, catalog_env: Path, local_t3: T3Database, runner: CliRunner,
    ) -> None:
        _seed_live_doc(
            local_t3, collection=_COL_A, title="c53hy-cli-bystander",
            content="c53hy cli innocent bystander document",
        )
        assert len(documents_by_title("c53hy-cli-bystander")) == 1

        bogus = hashlib.sha256(b"never registered anywhere -- c53hy cli").hexdigest()
        with patch("nexus.commands.store._t3", return_value=local_t3):
            result = runner.invoke(
                main, ["store", "delete", "--collection", _COL_A, "--id", bogus],
            )

        assert result.exit_code != 0, result.output
        assert len(documents_by_title("c53hy-cli-bystander")) == 1, (
            "a bogus doc_id must not tombstone an unrelated live document. "
            f"CLI output: {result.output!r}"
        )
