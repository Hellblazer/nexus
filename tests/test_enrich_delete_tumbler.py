# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-243 Phase 1 Step 5b (nexus-rfvyy.6.17): ``nx enrich delete --tumbler T``, the remedy the
``sensitive_with_aspects`` doctor row prints. It removes the aspect row of one document found by its
tumbler, and nothing else."""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.cli import main
from nexus.db.t2.http_document_aspects_store import HttpDocumentAspectsStore
from tests.test_document_aspects_store import _make_record

pytestmark = pytest.mark.t2_tenant

_COLL = "knowledge__rfvyy617d__bge-base-en-v15-768__v1"


def _seed(n: int = 2) -> list[str]:
    with HttpCatalogClient() as cat:
        owner = cat.register_owner("rfvyy617d-owner", owner_type="curator", tumbler_prefix="73.1")
        docs = [
            str(cat.register(owner, f"doc{i}", content_type="knowledge", physical_collection=_COLL,
                             source_uri=f"file:///rfvyy617d/{i}.md", file_path=f"{i}.md"))
            for i in range(n)
        ]
    store = HttpDocumentAspectsStore()
    for i, doc in enumerate(docs):
        assert store.upsert(_make_record(collection=_COLL, source_path=f"{i}.md", doc_id=doc))
    return docs


def test_tumbler_removes_that_documents_row_and_no_other(t2_service_env) -> None:
    keep, drop = _seed()
    result = CliRunner().invoke(main, ["enrich", "delete", "--tumbler", drop, "--yes"])
    assert result.exit_code == 0, result.output
    store = HttpDocumentAspectsStore()
    assert store.get_by_doc_id(drop) is None
    assert store.get_by_doc_id(keep) is not None, "the other document's row stays"


def test_tumbler_without_a_row_is_a_notice_not_an_error(t2_service_env) -> None:
    [doc] = _seed(1)
    runner = CliRunner()
    assert runner.invoke(main, ["enrich", "delete", "--tumbler", doc, "--yes"]).exit_code == 0
    again = runner.invoke(main, ["enrich", "delete", "--tumbler", doc, "--yes"])
    assert again.exit_code == 0 and "nothing to delete" in again.output


@pytest.mark.parametrize("extra", [[_COLL], [_COLL, "0.md"], ["--all"]])
def test_tumbler_excludes_the_positional_forms(t2_service_env, extra) -> None:
    result = CliRunner().invoke(main, ["enrich", "delete", "--tumbler", "1.1.1", *extra])
    assert result.exit_code == 2, result.output


def test_a_bare_delete_still_needs_a_collection_or_a_tumbler(t2_service_env) -> None:
    result = CliRunner().invoke(main, ["enrich", "delete"])
    assert result.exit_code == 2 and "--tumbler" in result.output


def test_the_positional_form_is_unchanged(t2_service_env) -> None:
    keep, drop = _seed()
    result = CliRunner().invoke(main, ["enrich", "delete", _COLL, "1.md", "--yes"])
    assert result.exit_code == 0, result.output
    store = HttpDocumentAspectsStore()
    assert store.get_by_doc_id(drop) is None and store.get_by_doc_id(keep) is not None
