# SPDX-License-Identifier: AGPL-3.0-or-later
"""E2E tests for the PDF indexing pipeline.

Real PDF extraction + chunking + local embedding + EphemeralClient.
No API keys required — uses DefaultEmbeddingFunction (ONNX MiniLM-L6-v2)
and chromadb.EphemeralClient.

AC-E1 through AC-E4 from RDR-011.
"""
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

from nexus.corpus import index_model_for_collection
from nexus.doc_indexer import index_pdf
from nexus.indexer import _git_metadata, _index_pdf_file

_local_ef = DefaultEmbeddingFunction()


def _local_embed(chunks, model, api_key, input_type="document", timeout=120.0):
    """Local embed stub: wraps ONNX MiniLM-L6-v2 — no API keys needed.

    Returns (embeddings, model) to match _embed_with_fallback's
    (list[list[float]], str) signature.  Passing model through (not "test-local")
    keeps the stored embedding_model matching target_model, which is required for
    the staleness guard (AC-E3) to trigger correctly on re-index.
    Uses .tolist() to convert numpy float32 to Python native floats.
    """
    return [v.tolist() for v in _local_ef(chunks)], model


# ── AC-E1 / AC-E2 / AC-E3 — index_pdf E2E ────────────────────────────────────

class TestIndexPdfE2E:
    """AC-E1 / AC-E2 / AC-E3: index_pdf with real extraction + local embedding."""

    def test_e2e_simple_pdf_queryable(self, simple_pdf: Path, local_t3) -> None:
        """AC-E1: simple.pdf indexed → query returns a pdf chunk with distance < 1.0."""
        with patch("nexus.config.get_credential", side_effect=lambda k: "test-key"), \
             patch("nexus.doc_indexer._embed_with_fallback", side_effect=_local_embed):
            count = index_pdf(simple_pdf, "pdf-e2e-simple", t3=local_t3)

        assert count > 0, "Expected at least one chunk indexed"
        results = local_t3.search("hello world test document", ["docs__pdf-e2e-simple"])
        assert results, "Expected search results after indexing"
        assert results[0]["distance"] < 1.0, f"distance={results[0]['distance']} too large"
        assert results[0]["store_type"] == "pdf"

    def test_e2e_multipage_page_attribution(self, multipage_pdf: Path, local_t3) -> None:
        """AC-E2: multipage.pdf → query for 'database transactions' returns page 2 chunk."""
        with patch("nexus.config.get_credential", side_effect=lambda k: "test-key"), \
             patch("nexus.doc_indexer._embed_with_fallback", side_effect=_local_embed):
            count = index_pdf(multipage_pdf, "pdf-e2e-multipage", t3=local_t3)

        assert count > 1, (
            "Expected multiple chunks from multipage_pdf so page attribution is testable"
        )
        results = local_t3.search(
            "database transactions ACID consistency storage systems",
            ["docs__pdf-e2e-multipage"],
            n_results=3,
        )
        assert results, "Expected search results for database transactions query"
        page_numbers = [r.get("page_number") for r in results]
        assert 2 in page_numbers, (
            f"Expected at least one page 2 chunk in top-3 results, got: {page_numbers}"
        )

    def test_e2e_staleness_guard(self, simple_pdf: Path, local_t3) -> None:
        """AC-E3: Re-indexing the same PDF returns 0 and document count is unchanged."""
        with patch("nexus.config.get_credential", side_effect=lambda k: "test-key"), \
             patch("nexus.doc_indexer._embed_with_fallback", side_effect=_local_embed):
            first = index_pdf(simple_pdf, "pdf-e2e-staleness", t3=local_t3)
            second = index_pdf(simple_pdf, "pdf-e2e-staleness", t3=local_t3)

        assert first > 0
        assert second == 0, "Second index of unchanged PDF must return 0"
        col = local_t3.get_or_create_collection("docs__pdf-e2e-staleness")
        assert col.count() == first, "Document count must not change after staleness skip"


# ── AC-E4 — _index_pdf_file E2E with real git repo ───────────────────────────

@pytest.fixture(scope="module")
def pdf_git_repo_e2e(tmp_path_factory: pytest.TempPathFactory, simple_pdf: Path) -> Path:
    """Real git repo with simple.pdf committed — module-scoped, created once."""
    import shutil
    repo = tmp_path_factory.mktemp("pdf-e2e-git")
    dest = repo / "docs" / "simple.pdf"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(simple_pdf, dest)

    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@nexus"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Nexus Test"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Add PDF fixture"], cwd=repo, check=True, capture_output=True)
    return repo


class TestIndexPdfFileE2E:
    """AC-E4: _index_pdf_file E2E with real git repo + local embedding."""

    def test_git_project_name_in_results(
        self, pdf_git_repo_e2e: Path, local_t3
    ) -> None:
        """AC-E4: indexed PDF chunks are queryable; git_project_name equals repo dir name."""
        pdf = pdf_git_repo_e2e / "docs" / "simple.pdf"
        collection_name = "docs__pdf-e2e-git"
        model = index_model_for_collection(collection_name)
        col = local_t3.get_or_create_collection(collection_name)
        git_meta = _git_metadata(pdf_git_repo_e2e)
        now_iso = datetime.now(UTC).isoformat()

        with patch("nexus.doc_indexer._embed_with_fallback", side_effect=_local_embed):
            _index_pdf_file(
                file=pdf,
                repo=pdf_git_repo_e2e,
                collection_name=collection_name,
                target_model=model,
                col=col,
                db=local_t3,
                voyage_key="vk_test",
                git_meta=git_meta,
                now_iso=now_iso,
                score=0.5,
            )

        results = local_t3.search("hello world test document", [collection_name])
        assert results, "Expected search results after indexing"
        assert results[0]["git_project_name"] == pdf_git_repo_e2e.name, (
            f"Expected git_project_name={pdf_git_repo_e2e.name!r}, "
            f"got {results[0]['git_project_name']!r}"
        )
