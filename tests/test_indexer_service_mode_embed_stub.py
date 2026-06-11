# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-fsquc: service-mode embed stub for prose_indexer and code_indexer.

doc_indexer carries the RDR-152 Seam-B stub (service mode is checked FIRST;
"no Python embed" — the JVM embeds server-side and the client's embeddings
argument is discarded). prose_indexer and code_indexer lacked the gate: in
service mode with a Voyage key present they client-embedded every chunk via
the Voyage API, then HttpVectorClient discarded the vectors and the service
embedded AGAIN — double Voyage spend per chunk since RDR-155 P4a (proven by
voyageai contextualized_embed tracebacks in index.log during the 2026-06-11
re-index runs).

These tests pin the stub: in service mode, NO client-side embedding call is
made by either indexer, and the upsert receives placeholder embeddings that
the http client ignores.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus.index_context import IndexContext


class _RecordingDb:
    def __init__(self) -> None:
        self.upserts: list[dict] = []

    def upsert_chunks_with_embeddings(
        self, collection_name, ids, documents, embeddings, metadatas
    ) -> None:
        self.upserts.append({
            "collection_name": collection_name,
            "ids": ids,
            "documents": documents,
            "embeddings": embeddings,
            "metadatas": metadatas,
        })


class _ForbiddenVoyageClient:
    def embed(self, *a, **k):
        raise AssertionError(
            "client-side Voyage embed called in service mode — double spend"
        )


def _forbidden_embed_with_fallback(*a, **k):
    raise AssertionError(
        "_embed_with_fallback called in service mode — double spend"
    )


def _make_ctx(tmp_path: Path, db: _RecordingDb, corpus: str, model: str) -> IndexContext:
    return IndexContext(
        col=object(),
        db=db,
        voyage_key="key-present-but-must-not-be-used",
        voyage_client=_ForbiddenVoyageClient(),
        repo_path=tmp_path,
        corpus=corpus,
        embedding_model=model,
        git_meta={},
        now_iso="2026-06-11T00:00:00+00:00",
        force=True,  # bypass staleness — no col roundtrip
    )


@pytest.fixture(autouse=True)
def _service_mode(monkeypatch):
    monkeypatch.setattr(
        "nexus.db.http_vector_client.is_vector_service_mode", lambda: True
    )
    # Both indexers import the legacy embedder lazily from doc_indexer.
    monkeypatch.setattr(
        "nexus.doc_indexer._embed_with_fallback", _forbidden_embed_with_fallback
    )


def test_prose_indexer_service_mode_skips_client_embed(tmp_path, monkeypatch):
    from nexus.prose_indexer import index_prose_file

    f = tmp_path / "note.md"
    f.write_text("# Title\n\nSome prose content long enough to chunk.\n")
    db = _RecordingDb()
    ctx = _make_ctx(tmp_path, db, "rdr__t__minilm-l6-v2-384__v1", "minilm-l6-v2-384")

    count = index_prose_file(ctx, f)

    assert count >= 1
    assert len(db.upserts) == 1
    up = db.upserts[0]
    assert len(up["embeddings"]) == len(up["ids"])
    assert all(e == [] for e in up["embeddings"]), (
        "service mode passes placeholder embeddings the http client ignores"
    )
    # Model identity preserved in metadata (no fallback-model rewrite).
    assert all(
        m.get("embedding_model") == "minilm-l6-v2-384" for m in up["metadatas"]
    )


def test_code_indexer_service_mode_skips_client_embed(tmp_path, monkeypatch):
    from nexus.code_indexer import index_code_file

    f = tmp_path / "mod.py"
    f.write_text(
        "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n"
    )
    db = _RecordingDb()
    ctx = _make_ctx(tmp_path, db, "code__t__minilm-l6-v2-384__v1", "minilm-l6-v2-384")

    count = index_code_file(ctx, f)

    assert count >= 1
    assert len(db.upserts) == 1
    up = db.upserts[0]
    assert len(up["embeddings"]) == len(up["ids"])
    assert all(e == [] for e in up["embeddings"])
