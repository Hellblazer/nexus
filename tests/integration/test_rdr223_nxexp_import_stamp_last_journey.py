# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-223 decision D2 on the ``.nxexp`` import (nexus-z0o2p.34): each document's stamp is LAST.

The import used to stamp a document complete with the request that wrote its last page, and fire the
post-store chains for that page afterwards. A process killed in the chains left a document that read
complete whose chains (taxonomy assignment, aspect enqueue) nothing would fire again, and a rerun of
the same file kept it as current truth. The writer now defers the stamp
(``MultiDocumentImportWriter(defer_completion=True)``): the page's rows and chunks land, the importer
fires the page's chains, and only then does it send the stamp of every document whose last page that
was (one stamp-only ``append_many`` for the page, ``complete`` per document).

The journey kills the process inside the chains of the page that carries a document's last rows, on
the real engine substrate:

* the documents whose last page came earlier are ``complete``, each stamped after ITS page's chains;
* the document whose last rows just landed is ``indexing`` with its whole manifest owned;
* no write request of the import carried a completion stamp;
* the rerun resumes it (this same file left it ``indexing``), fires its chains again, and completes it.
"""
from __future__ import annotations

import dataclasses

import pytest

import nexus.catalog.http_catalog_client as hcc
import nexus.exporter as exporter_mod
from nexus.catalog.factory import make_catalog_reader
from nexus.catalog.multi_document_write import MultiDocumentImportWriter
from nexus.db.http_vector_client import HttpVectorClient
from nexus.db.limits import QUOTAS
from nexus.exporter import import_collection
from nexus.hook_registry import HookRegistry
from tests.test_z0o2p19_nxexp_import_combined_write import (
    _PAGE,
    _coll,
    _manifest,
    _page_of_each_doc,
    _shaped_file,
    _write_nxexp,
)

pytestmark = [pytest.mark.integration]


class HookKilled(BaseException):
    """The simulated death of the client process inside a post-store chain."""


@pytest.fixture
def small_pages(monkeypatch):
    monkeypatch.setattr(
        "nexus.exporter.QUOTAS", dataclasses.replace(QUOTAS, MAX_RECORDS_PER_WRITE=_PAGE))


def test_a_kill_in_a_page_chain_leaves_the_documents_last_page_indexing_and_the_rerun_completes_it(
    t2_service_env, tmp_path, small_pages, monkeypatch,
):
    client = HttpVectorClient(tenant=t2_service_env)
    reader = make_catalog_reader()
    dst = _coll("stamplast")
    records, expected = _shaped_file(dst)
    f = tmp_path / "stamplast.nxexp"
    _write_nxexp(f, dst, records)
    spans = _page_of_each_doc(records)
    last_page = max(hi for _, hi in spans.values())
    # The kill page: a page that is the LAST page of at least one document and is not the file's last.
    kill_page = min(hi for _, hi in spans.values() if 0 < hi < last_page)
    finishing = {u for u, (_, hi) in spans.items() if hi == kill_page}
    earlier = {u for u, (_, hi) in spans.items() if hi < kill_page}
    assert finishing and earlier, "non-vacuity: the kill page ends a document and earlier ones ended before it"

    # Record the write requests, to prove none carries a stamp and where the stamps are.
    bodies: list[tuple[str, dict]] = []
    real_post = hcc.HttpCatalogClient._post

    def _post(self, path, body=None, **kw):
        if path in ("/manifest/write_many", "/manifest/append_many"):
            bodies.append((path, body or {}))
        return real_post(self, path, body, **kw)

    monkeypatch.setattr(hcc.HttpCatalogClient, "_post", _post)

    # A SIGKILL runs no cleanup: the importer's own failure handling (which marks its open documents
    # ``failed`` on an exception) must not run, so the fences stay as the kill left them.
    monkeypatch.setattr(MultiDocumentImportWriter, "abort", lambda self, error: None)
    # Nor does the removal of the documents the import registered and never wrote.
    monkeypatch.setattr(exporter_mod._OwnerImport, "compensate_minted", lambda self: 0)
    state = {"calls": 0, "armed": True}

    def _chain(*a, **kw):
        state["calls"] += 1
        if state["armed"] and state["calls"] == kill_page + 1:
            raise HookKilled("killed in a post-store chain")

    hooks = HookRegistry()
    hooks.register_batch(_chain)

    with pytest.raises(HookKilled):
        import_collection(db=client, input_path=f, target_collection=dst, hooks=hooks)

    assert state["calls"] == kill_page + 1, "non-vacuity: the kill landed in the kill page's chain"
    for uri in earlier:
        assert reader.by_source_uri(uri).index_state == "complete", f"{uri} ended before the kill page"
    for uri in finishing:
        entry = reader.by_source_uri(uri)
        assert entry.index_state == "indexing", f"{uri}: its stamp was sent ahead of its chains"
        assert [c for _, c in _manifest(reader, str(entry.tumbler))] == expected[uri], (
            f"{uri}: every row of the document had landed and was owned")
    for path, body in bodies:
        assert not body.get("complete"), (path, "a request-level stamp")
        for entry in body.get("docs", []):
            if entry.get("rows"):
                assert "complete" not in entry, (path, entry["doc_id"], "a stamp riding a data request")

    state["armed"] = False
    done = import_collection(db=client, input_path=f, target_collection=dst, hooks=hooks)

    assert done["imported_count"] > 0, "the rerun resumed the documents this file left open"
    for uri, chashes in expected.items():
        entry = reader.by_source_uri(uri)
        assert entry.index_state == "complete", (uri, entry.index_state)
        assert [c for _, c in _manifest(reader, str(entry.tumbler))] == chashes
