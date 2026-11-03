# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ypgbz: a PDF or markdown document registered by the single-file indexers carries its file hash in its
own catalog ``meta.content_hash`` before the run is stamped complete.

The export witness (``exporter._resolve_export_owners``, nexus-3ajpd) ships ``key_registered`` when
``meta.content_hash == index_content_hash`` on a complete document: the catalog's own evidence that the fence key
is a file hash, independent of any chunk row (a row's ``content_hash`` is the last writer's). Only repo files
stamped it at registration; ``nx index pdf`` / ``nx index md`` did not, so for them the importer's witness was the
chunk-row allow-list alone and missed when every chunk had been restamped by another document.

Real engine. The hash is not known at the single-file pre-flight registration for markdown (``_index_document``
hashes the file), so it is stamped where every writer of these paths already holds it: just before the document's
writer begins, i.e. before the completion stamp. The tests cover both PDF routes (streaming and all-at-once) and
markdown, and each ends with the exporter reporting ``key_registered`` from the catalog alone. The positive
control removes the stamp and shows the same assertions fail.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from tests._catalog_fixture_ops import documents_by_file_path
from tests.integration.test_rdr223_pdf_journey import _COLLECTION, _FakeChunker, _FakeExtractor, _index, _lines, _write_pdf

pytestmark = [pytest.mark.integration]


@pytest.fixture()
def fake_pdf(monkeypatch):
    ex = type("Extractor", (_FakeExtractor,), {"lines": [], "calls": []})
    for mod in ("nexus.doc_indexer", "nexus.pipeline_stages"):
        monkeypatch.setattr(f"{mod}.PDFExtractor", ex)
        monkeypatch.setattr(f"{mod}.PDFChunker", _FakeChunker)
    return ex


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _only(path: Path):
    (row,) = documents_by_file_path(str(path))
    return row


def _key_registered(row) -> list[bool]:
    """What the exporter ships for the document's chunks, read through the real catalog reader."""
    from nexus.catalog.factory import make_catalog_reader
    from nexus.exporter import _resolve_export_owners

    reader = make_catalog_reader()
    chashes = [m.chash for m in reader.get_manifest(str(row.tumbler))]
    assert chashes, "non-vacuity: the document has a manifest"
    owners = _resolve_export_owners(reader, chashes, row.physical_collection)
    assert set(owners) == set(chashes), "non-vacuity: the exporter resolved every chunk to the document"
    return [owners[c]["key_registered"] for c in chashes]


def _index_markdown(md: Path, marker: str) -> None:
    from nexus.db.http_vector_client import HttpVectorClient
    from nexus.doc_indexer import index_markdown

    assert index_markdown(md, corpus=f"ypgbz-{marker}", t3=HttpVectorClient(), collection_name=_COLLECTION, force=True) > 0


def _assert_witnessed(row, file_hash: str) -> None:
    assert row.index_state == "complete" and row.index_content_hash == file_hash, (row.index_state, row.index_content_hash)
    assert row.meta.get("content_hash") == file_hash, row.meta
    assert all(_key_registered(row)) and _key_registered(row), "the exporter reports the registration's own evidence"


def test_markdown_registration_stamps_the_file_hash_and_the_export_witnesses_it(tmp_path: Path) -> None:
    md = tmp_path / "ypgbz-md.md"
    md.write_text("# H\n\nBody of ypgbz markdown, unique text one.\n\nSecond paragraph of ypgbz markdown.\n")

    _index_markdown(md, "md")

    _assert_witnessed(_only(md.resolve()), _sha(md))


@pytest.mark.parametrize("streaming", ["always", "never"], ids=["streaming", "all-at-once"])
def test_pdf_registration_stamps_the_file_hash_and_the_export_witnesses_it(tmp_path: Path, fake_pdf, streaming) -> None:
    marker = f"ypgbz-pdf-{streaming}"
    fake_pdf.lines = _lines(marker, 3)
    path = _write_pdf(tmp_path, marker)

    assert _index(path, marker, streaming=streaming) == 3

    _assert_witnessed(_only(path), _sha(path))


def test_a_changed_markdown_file_is_restamped_with_its_new_hash(tmp_path: Path) -> None:
    """The registration moves with the key: a stale old hash would leave ``key_registered`` false for ever."""
    md = tmp_path / "ypgbz-restamp.md"
    md.write_text("# H\n\nFirst version of ypgbz restamp.\n")
    _index_markdown(md, "restamp")
    first = _sha(md)
    _assert_witnessed(_only(md.resolve()), first)

    md.write_text("# H\n\nSecond version of ypgbz restamp, different bytes.\n")
    _index_markdown(md, "restamp")

    assert _sha(md) != first
    _assert_witnessed(_only(md.resolve()), _sha(md))


def test_positive_control_without_the_stamp_the_witness_is_absent(tmp_path: Path, fake_pdf, monkeypatch) -> None:
    """Remove the stamp and the same documents carry no ``meta.content_hash``, complete as they are, and the
    exporter does not report ``key_registered``: the assertions above see the stamp, not something else."""
    monkeypatch.setattr("nexus.catalog.multi_batch_write.stamp_registered_content_hash", lambda *a, **k: False)
    md = tmp_path / "ypgbz-control.md"
    md.write_text("# H\n\nBody of ypgbz control markdown.\n")
    _index_markdown(md, "control")
    fake_pdf.lines = _lines("ypgbz-control-pdf", 3)
    pdf = _write_pdf(tmp_path, "ypgbz-control-pdf")
    assert _index(pdf, "ypgbz-control-pdf", streaming="always") == 3

    for path, file_hash in ((md.resolve(), _sha(md)), (pdf, _sha(pdf))):
        row = _only(path)
        assert row.index_state == "complete" and row.index_content_hash == file_hash
        assert "content_hash" not in row.meta, row.meta
        assert not any(_key_registered(row))
