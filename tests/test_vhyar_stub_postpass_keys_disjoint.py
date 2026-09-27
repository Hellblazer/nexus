# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-vhyar: a late stub duplicate cannot undo the streaming post-pass.

The engine merges chunk metadata on write (``chunks.metadata || EXCLUDED.metadata``,
nexus-w94eo), so a late-committing duplicate of the streaming stub upsert (the
504-retry shape) can only re-assert keys the stub itself carries. The post-pass
(:func:`_enrich_metadata_from_extraction`) sets or deletes a small key set. If
the two sets never overlap, the stub and the post-pass commute, and no arrival
order of a stub duplicate can revert a title, extraction_method or a cleared
``quality_gate_overridden``. The same holds for the frecency-only reindex, which
owns ``frecency_score``. This pins that disjointness on the client, where it
is decided; the engine half is ``PgVectorRepositoryContractTest``'s
``upsertChunks_stubLandingAfterEnrichmentWrite_doesNotEraseEnrichment``.
"""
from pathlib import Path
from typing import Any

import pytest

from nexus.pdf_extractor import ExtractionResult
from nexus.pipeline_stages import _build_chunk_metadata, _enrich_metadata_from_extraction


class _Chunk:
    text = "stub chunk text"
    metadata = {"chunk_start_char": 0, "chunk_end_char": 15, "page_number": 1}


class _Col:
    def get(self, **_: Any) -> dict[str, list[str]]:
        return {"ids": ["c1"]}


class _RecordingT3:
    def __init__(self) -> None:
        self.calls: list[tuple[list[dict[str, Any]], list[str]]] = []

    def update_chunks(
        self, collection: str, ids: list[str], metadatas: list[dict[str, Any]],
        *, delete_keys: list[str] | None = None,
    ) -> None:
        self.calls.append((metadatas, list(delete_keys or [])))


def _stub_keys() -> set[str]:
    return set(_build_chunk_metadata(
        _Chunk(), content_hash="h" * 64, pdf_path="/repo/x.pdf", corpus="c",
        embedding_model="m", now_iso="2026-09-27T00:00:00+00:00",
    ))


@pytest.mark.parametrize("overridden", [True, False], ids=["gate-overridden", "gate-clean"])
def test_stub_and_post_pass_touch_disjoint_keys(overridden: bool) -> None:
    meta: dict[str, Any] = {"extraction_method": "docling", "page_count": 1, "pdf_author": "A"}
    if overridden:
        meta["quality_gate_overridden"] = True
    t3 = _RecordingT3()

    ok = _enrich_metadata_from_extraction(
        "h" * 64, ExtractionResult(text="body", metadata=meta), Path("/repo/x.pdf"),
        t3, _Col(), "docs__test",
    )

    assert ok is True
    assert len(t3.calls) == 1
    metadatas, delete_keys = t3.calls[0]
    post_pass_keys = set(metadatas[0]) | set(delete_keys)
    stub_keys = _stub_keys()
    # Non-vacuity: both sides are populated and quality_gate_overridden is touched
    # on both branches, so an empty intersection is a real statement.
    assert stub_keys and "content_hash" in stub_keys
    assert {"title", "extraction_method", "quality_gate_overridden"} <= post_pass_keys
    assert stub_keys.isdisjoint(post_pass_keys), (
        f"stub re-asserts post-pass keys {sorted(stub_keys & post_pass_keys)}: "
        "a late stub duplicate would revert them under the merging engine"
    )


def test_stub_omits_the_key_the_frecency_reindex_owns() -> None:
    # The frecency-only reindex (indexer._run_index_frecency_only) writes
    # frecency_score alone. A stub carrying its 0.0 default would reset a bumped
    # score if a late duplicate committed after that reindex.
    assert "frecency_score" not in _stub_keys()
