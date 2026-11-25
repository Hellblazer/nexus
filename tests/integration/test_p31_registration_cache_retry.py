# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P3.1 (nexus-p2m1x.17): a write that gets the engine's typed unregistered-collection 422
invalidates the name in the per-process registration cache, registers it again and retries ONCE.

After P3.1 the engine refuses a write under a collection with no registry row with a typed 422
(``UnregisteredCollectionException``, reason ``unregistered_collection``). A long-lived process
caches a name as registered (``nexus.corpus._REGISTERED_COLLECTIONS``); another process can delete
the registry row afterwards. These tests prime the cache, delete the row from a separate catalog
client, then index a PDF through ``doc_indexer.index_pdf`` against the real engine substrate:

* the write succeeds with exactly one re-registration;
* a second consecutive refusal surfaces, with no further retry (never a loop);
* a 422 for any other reason does not trigger the retry;
* a name this process never cached keeps its 422 (nothing here registers a collection nobody asked for).
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import httpx
import pytest

import nexus.corpus as corpus
from nexus.catalog.factory import make_catalog_reader, make_catalog_writer
from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.db.http_vector_client import HttpVectorClient
from nexus.db.t2._refreshable_client import RefreshableHttpStoreMixin
from nexus.doc_indexer import _register_or_lookup_doc_id, index_pdf
from nexus.pdf_chunker import TextChunk
from nexus.pdf_extractor import ExtractionResult

pytestmark = [pytest.mark.integration]

_COLLECTION = "docs__p31retry-pdf__bge-base-en-v15-768__v1"


class _FakeExtractor:
    lines: list[str] = []

    def extract(self, pdf_path, *, extractor="mineru", on_page=None,
                allow_degraded=False, on_heartbeat=None):
        pos, bounds = 0, []
        for i, line in enumerate(self.lines):
            if on_page:
                on_page(i, line, {"page_number": i + 1, "text_length": len(line)})
            bounds.append({"page_number": i + 1, "start_char": pos, "page_text_length": len(line) + 1})
            pos += len(line) + 1
        return ExtractionResult(
            text="\n".join(self.lines),
            metadata={"extraction_method": "mineru", "page_count": len(self.lines),
                      "page_boundaries": bounds, "table_regions": [], "format": "markdown"})


class _FakeChunker:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def chunk(self, text, metadata):
        return [
            TextChunk(text=line, chunk_index=i, metadata={"page_number": i + 1})
            for i, line in enumerate(x for x in text.split("\n") if x.strip())
        ]


@pytest.fixture()
def fake_pdf(monkeypatch):
    def install(lines: list[str]) -> None:
        ex = type("Extractor", (_FakeExtractor,), {"lines": lines})
        for mod in ("nexus.doc_indexer", "nexus.pipeline_stages"):
            monkeypatch.setattr(f"{mod}.PDFExtractor", ex)
            monkeypatch.setattr(f"{mod}.PDFChunker", _FakeChunker)

    return install


def _pdf(tmp_path: Path, marker: str) -> Path:
    p = tmp_path / f"doc-{marker}.pdf"
    p.write_bytes(f"%PDF-1.4 p31retry {marker}\n".encode())
    return p.resolve()


def _index(path: Path, marker: str, *, streaming: str) -> int:
    return index_pdf(path, f"p31retry-{marker}", t3=HttpVectorClient(),
                     collection_name=_COLLECTION, streaming=streaming)


def _delete_row_from_another_client() -> None:
    """Another process deleting the registry row: a catalog client of its own, and this process's
    cache left exactly as it was (still calling the name registered)."""

    writer = make_catalog_writer()
    try:
        writer.delete_collection(_COLLECTION)
    finally:
        writer.close()
    assert _COLLECTION in corpus._REGISTERED_COLLECTIONS, "the cache must still trust the deleted name"


def _row_exists() -> bool:
    return make_catalog_reader().get_collection(_COLLECTION) is not None


def _prime_then_delete(tmp_path: Path, fake_pdf, streaming: str) -> None:
    """Index one PDF (registering the collection and caching it), then delete the row from another client."""
    fake_pdf([f"prime {streaming} page {i} p31retry unique text" for i in range(6)])
    assert _index(_pdf(tmp_path, f"prime-{streaming}"), f"prime-{streaming}", streaming=streaming) == 6
    _delete_row_from_another_client()
    assert not _row_exists()


@contextmanager
def _watch(*, rewrite_reason: str | None = None) -> Iterator[dict[str, list]]:
    """What the engine saw, below the retry: every registration (``/collections/upsert``) and every 422
    answer, by path, over the one transport the catalog, pipeline and T2 stores share.

    *rewrite_reason* turns the engine's own 422 into one with that ``reason`` (same collection), to put a
    refusal of another kind in front of the client."""

    seen: dict[str, list] = {"registrations": [], "refusals": []}
    orig_send = RefreshableHttpStoreMixin._send

    def send(self, method, path, **kw):
        if path.endswith("/collections/upsert"):
            seen["registrations"].append(path)
        try:
            return orig_send(self, method, path, **kw)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 422:
                seen["refusals"].append(path)
                if rewrite_reason is not None:
                    body = {**exc.response.json(), "reason": rewrite_reason}
                    raise httpx.HTTPStatusError(
                        str(exc), request=exc.request,
                        response=httpx.Response(422, json=body, request=exc.request)) from exc
            raise

    RefreshableHttpStoreMixin._send = send  # type: ignore[method-assign]
    try:
        yield seen
    finally:
        RefreshableHttpStoreMixin._send = orig_send  # type: ignore[method-assign]


@pytest.mark.parametrize("streaming", ["always", "never"])
def test_a_pdf_indexed_after_another_client_deleted_the_cached_row_succeeds_with_one_retry(
    tmp_path, fake_pdf, streaming,
) -> None:
    _prime_then_delete(tmp_path, fake_pdf, streaming)

    fake_pdf([f"second {streaming} page {i} p31retry unique text" for i in range(6)])
    with _watch() as seen:
        n = _index(_pdf(tmp_path, f"second-{streaming}"), f"second-{streaming}", streaming=streaming)

    assert n == 6
    assert _row_exists(), "the retry registered the name again"
    assert len(seen["refusals"]) == 1, f"one refusal, then one retry: {seen['refusals']}"
    assert len(seen["registrations"]) == 1, "exactly one re-registration"


def _register_document(tmp_path: Path, marker: str) -> tuple[str, bool]:
    """ONE document registration under the collection (what every PDF path does first), as the engine
    answers it; ``("", False)`` is a registration that failed."""

    return _register_or_lookup_doc_id(
        _pdf(tmp_path, marker), f"p31retry-{marker}", content_type="paper",
        physical_collection=_COLLECTION, with_created=True)


def test_a_second_consecutive_refusal_surfaces_and_never_loops(tmp_path, fake_pdf, monkeypatch) -> None:

    _prime_then_delete(tmp_path, fake_pdf, "always")
    registrations: list[str] = []
    # The registration "succeeds" and writes nothing, so the engine refuses the resend too.
    monkeypatch.setattr(
        HttpCatalogClient, "register_collection", lambda self, name, **kw: registrations.append(name))

    with _watch() as seen:
        outcome = _register_document(tmp_path, "third")

    assert outcome == ("", False), "the second refusal surfaced"
    assert registrations == [_COLLECTION], "registered once, not once per refusal"
    assert len(seen["refusals"]) == 2, f"the refusal and its one resend, then it surfaces: {seen['refusals']}"
    assert not _row_exists()


def test_a_422_for_another_reason_is_not_retried(tmp_path, fake_pdf) -> None:
    _prime_then_delete(tmp_path, fake_pdf, "always")

    with _watch(rewrite_reason="unregistered_embedding_model") as seen:
        outcome = _register_document(tmp_path, "other")

    assert outcome == ("", False)
    assert len(seen["refusals"]) == 1, "no resend"
    assert seen["registrations"] == [], "and no registration"
    assert not _row_exists()


def test_a_name_this_process_never_cached_keeps_its_422(tmp_path) -> None:
    """The retry registers a name only when this process had cached it. A document written straight to the catalog under
    a cold name (no pre-flight: the pre-flight is the next test) is refused and stays refused."""
    assert _COLLECTION not in corpus._REGISTERED_COLLECTIONS
    with _watch() as seen, HttpCatalogClient() as cat:
        owner = cat.register_owner("p31retry-cold-owner", "curator")
        with pytest.raises(httpx.HTTPStatusError) as refused:
            cat.register(owner, "cold doc", content_type="paper", physical_collection=_COLLECTION)

    assert refused.value.response.status_code == 422, "the document registration failed, as it did before"
    assert len(seen["refusals"]) == 1 and seen["registrations"] == [], "no resend, no registration"
    assert not _row_exists()


def test_the_pre_flight_registers_a_cold_collection_before_its_document(tmp_path) -> None:
    """``_register_or_lookup_doc_id`` registers the collection first (RDR-229 P3.1), so a cold name is registered
    by that call and the document lands without a refusal."""
    assert _COLLECTION not in corpus._REGISTERED_COLLECTIONS
    with _watch() as seen:
        outcome = _register_document(tmp_path, "cold-preflight")

    assert outcome[0] and outcome[1] is True, "the document registered"
    assert seen["refusals"] == [] and len(seen["registrations"]) == 1, seen
    assert _row_exists()
