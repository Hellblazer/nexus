# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-237 (nexus-tr1rj.24) on the real engine: ``nx index repo`` over an unusable MinerU time limit.

``pdf.mineru_page_timeout_s`` x ``pdf.mineru_page_batch`` above 240 s used to be met by the first
PDF's extraction, after that PDF's catalog row was written, and aborted the run after code and
prose were indexed. The repo walk now HOLDS the PDFs that need indexing before anything is
registered: the prose file is indexed, the held PDF has no catalog row and no chunk, and a PDF
the catalog holds complete is untouched (its row, manifest and chunks) even when it is stale only
by embedding model. The real run: real git, the real catalog pass, the real staleness cache.

Integration-marked and not an RDR-192 pin file (``tests/test_rdr192_pins_not_integration_marked_lint.py``).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pymupdf
import pytest

from tests._catalog_fixture_ops import active_reader
from nexus.db.t3 import T3Database
from nexus.registry import RepoRegistry
from tests.conftest import fake_credentials
from tests.test_cp46b_runfence_repo_staleness import (  # noqa: F401 — fixtures and helpers shared with the fence tests
    _commit,
    _entry_ending_with,
    _init_repo,
    _prime_owner,
    catalog_env,
    git_identity,
    local_t3,
    mock_voyage_client,
)

pytestmark = [pytest.mark.integration]

_BOUNDS = "pdf.mineru_page_timeout_s (180 s) x pdf.mineru_page_batch (2) = 360 s"


def _write_pdf(path: Path, marker: str) -> None:
    doc = pymupdf.open()
    for page_no in range(2):
        page = doc.new_page()
        page.insert_text((72, 72), f"{marker} page {page_no + 1}. " + "A sentence of body text. " * 12)
    doc.save(str(path))
    doc.close()


def _index(repo: Path, t3: T3Database, monkeypatch: pytest.MonkeyPatch, *, extractor: str, **kw) -> dict:
    """The real ``index_repository`` run; returns its stats. *extractor* is the repo's
    ``pdf.extractor`` for this run."""
    from nexus.indexer import index_repository
    from tests._catalog_fixture_ops import seed_manifest_chunks

    monkeypatch.setenv("NX_LOCAL", "1")
    monkeypatch.setattr("nexus.indexer._repo_pdf_extractor", lambda r: extractor)
    reg = RepoRegistry(repo.parent / "repos.json")
    reg.add(repo)

    orig_write_batch = t3._write_batch

    def _seeding_write_batch(col, collection_name, ids, documents, metadatas, embeddings=None, **kwargs):
        orig_write_batch(col, collection_name, ids, documents, metadatas, embeddings, **kwargs)
        seed_manifest_chunks(collection_name, ids)

    monkeypatch.setattr(t3, "_write_batch", _seeding_write_batch)
    with patch("nexus.db.make_t3", return_value=t3), \
            patch("nexus.config.get_credential", side_effect=fake_credentials()):
        return index_repository(repo, reg, force=False, **kw)


def _over_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nexus.config.get_mineru_page_timeout_s", lambda repo_root=None: 180)
    monkeypatch.setattr("nexus.config.get_mineru_page_batch", lambda repo_root=None: 2)


def _documents_ending_with(suffix: str) -> list:
    return [e for e in active_reader().all_documents() if str(getattr(e, "file_path", "")).endswith(suffix)]


def test_a_new_pdf_is_held_before_any_catalog_row_and_the_prose_file_is_indexed(
    tmp_path: Path, catalog_env: Path, local_t3: T3Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _prime_owner(repo, local_t3, monkeypatch)
    (repo / "notes.md").write_text("# Notes\n\nProse that is indexed next to a held PDF.\n", encoding="utf-8")
    _write_pdf(repo / "paper.pdf", "newpdf")
    _commit(repo)
    _over_the_limit(monkeypatch)
    phases: list[str] = []

    stats = _index(repo, local_t3, monkeypatch, extractor="mineru", on_phase=phases.append)

    assert stats["pdf_mineru_bounds_held"] == 1
    assert stats["pdf_mineru_bounds_held_paths"] == [str(repo / "paper.pdf")]
    assert _BOUNDS in stats["pdf_mineru_bounds_problem"]
    assert _entry_ending_with("notes.md").index_state == "complete", "the prose file was indexed"
    assert _documents_ending_with("paper.pdf") == [], "no catalog row for the held PDF"
    assert any(_BOUNDS in p and "NOT indexed" in p for p in phases), phases


def test_a_pdf_stale_only_by_embedding_model_is_held_and_its_row_and_chunks_are_untouched(
    tmp_path: Path, catalog_env: Path, local_t3: T3Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Indexed under ``pymupdf``, then the staleness cache calls it stale (the embedding model
    changed; the catalog fence still says complete) and the extractor is ``mineru`` over the
    limit: the late recheck holds it, and its catalog row (state, content hash, tumbler,
    collection) does not move. That nothing is extracted is pinned by the unit test."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _prime_owner(repo, local_t3, monkeypatch)
    _write_pdf(repo / "paper.pdf", "stalepdf")
    _commit(repo)

    first = _index(repo, local_t3, monkeypatch, extractor="pymupdf")
    assert first["pdf_mineru_bounds_held"] == 0
    before = _entry_ending_with("paper.pdf")
    assert before.index_state == "complete", "non-vacuity: the PDF was indexed on the first run"

    _over_the_limit(monkeypatch)
    monkeypatch.setattr("nexus.indexer.check_staleness", lambda *a, **k: False)
    stats = _index(repo, local_t3, monkeypatch, extractor="mineru")

    assert stats["pdf_mineru_bounds_held"] == 1
    after = _entry_ending_with("paper.pdf")
    assert after.index_state == "complete" and after.index_content_hash == before.index_content_hash
    assert str(after.tumbler) == str(before.tumbler)
    assert after.physical_collection == before.physical_collection
