# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.db.http_vector_client import HttpVectorClient
from nexus.types import SearchResult
from tests.conftest import catalog_row_for_collection_name, patched_mcp_infra_t3


# ── Helpers ──────────────────────────────────────────────────────────────────


def _sr(
    id: str = "r1",
    content: str = "some content",
    distance: float = 0.3,
    collection: str = "code__repo",
    file_path: str = "/repo/file.py",
    **extra_meta: object,
) -> SearchResult:
    meta = {"source_path": file_path, "file_path": file_path, "frecency_score": 0.5}
    meta.update(extra_meta)
    return SearchResult(
        id=id, content=content, distance=distance,
        collection=collection, metadata=meta,
    )


def _cli_main():
    return __import__("nexus.cli", fromlist=["main"]).main


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Set up env vars for CLI integration tests.

    It also used to point search_cmd._CONFIG_DIR at tmp_path, which existed
    solely so ripgrep cache discovery could be sandboxed. Both the constant
    and the discovery are gone (nexus-06aei).
    """
    for k, v in [("CHROMA_API_KEY", "k"), ("VOYAGE_API_KEY", "v"),
                 ("CHROMA_TENANT", "t"), ("CHROMA_DATABASE", "d")]:
        monkeypatch.setenv(k, v)
    return tmp_path


@pytest.fixture()
def mock_t3():
    # cli_env sets cloud creds -> real _t3() would be HttpVectorClient.
    t3 = MagicMock(spec=HttpVectorClient)
    # RDR-204 Phase 3 fixture-seam fix (nexus-ft04v.26): resolve_corpus's
    # bare-corpus fan-out reads a catalog-row shape per collection.
    t3.list_collections.return_value = [
        {"name": n, **catalog_row_for_collection_name(n)}
        for n in ("code__repo-abcd1234",)
    ]
    return t3


# ── Scoring unit tests ───────────────────────────────────────────────────────


def test_scoring_is_monotonic_in_distance():
    from nexus.scoring import apply_hybrid_scoring

    results = apply_hybrid_scoring([
        _sr(id="v1", distance=0.0, collection="code__repo"),
        _sr(id="v2", distance=0.5, collection="code__repo"),
        _sr(id="v3", distance=1.0, collection="code__repo"),
    ], hybrid=True)
    scores = {r.id: r.hybrid_score for r in results}
    assert scores["v1"] > scores["v2"] > scores["v3"]


def test_hybrid_score_in_valid_range():
    """Scores stay within [0.0, 1.0] for ordinary code results.

    This used to build one of its two results as an ``rg__cache`` row, so it
    exercised ``apply_hybrid_scoring``'s rg floor-score special case. That
    branch is deleted (nexus-06aei) and the name is now just an unregistered
    collection, treated like any other non-code string — so the test kept
    passing for a reason unrelated to what it was written to check. Rebuilt
    on two ordinary code results, which is the invariant actually worth
    pinning.

    It survived the deletion sweep because its NAME contains no "rg".
    """
    from nexus.scoring import apply_hybrid_scoring

    results = apply_hybrid_scoring([
        _sr(id="v1", distance=0.2, collection="code__repo"),
        _sr(id="v2", distance=0.9, collection="code__repo"),
    ], hybrid=True)
    assert all(0.0 <= r.hybrid_score <= 1.0 for r in results), \
        [(r.id, r.hybrid_score) for r in results]

def test_catalog_chunk_count_no_longer_consulted_for_scoring():
    """nexus-0bmhd (2026-09-01): RDR-006's file-size penalty — and its
    catalog ``documents.chunk_count`` lookup for post-RDR-108 Phase-3
    chunks, nexus-dxly — is removed from scoring entirely. Domination
    control moved to the render layer
    (``search_engine.apply_file_diversity_cap``). Two code__ results at
    an identical vector distance must score IDENTICALLY regardless of
    doc_id/chunk_count, and ``chunk_counts_for_docs`` must not even be
    CALLED — scoring has nothing left to resolve it for.
    """
    from nexus.scoring import apply_hybrid_scoring

    calls: list[list] = []

    class _FakeCatalog:
        def chunk_counts_for_docs(self, doc_ids: list) -> dict:
            calls.append(list(doc_ids))
            return {"ART-small": 5, "ART-large": 5000}

    # Two code__ results, identical vector distance, no chunk_count
    # in metadata (Phase-3 shape).  Only doc_id differs.
    small = _sr(id="s", distance=0.3, collection="code__repo", doc_id="ART-small")
    large = _sr(id="l", distance=0.3, collection="code__repo", doc_id="ART-large")
    # Drop chunk_count from metadata to simulate Phase-3 chunk shape.
    for r in (small, large):
        r.metadata.pop("chunk_count", None)

    results = apply_hybrid_scoring(
        [small, large], hybrid=False, catalog=_FakeCatalog(),
    )
    score = {r.id: r.hybrid_score for r in results}
    assert score["s"] == pytest.approx(score["l"], abs=1e-9), (
        f"chunk-count-blind scoring must score equal-distance results "
        f"identically regardless of file size; got {score!r}"
    )
    assert calls == [], (
        "chunk_counts_for_docs must not be consulted for scoring any more"
    )


def test_metadata_chunk_count_no_longer_consulted_for_scoring():
    """Same pin via the legacy ``metadata["chunk_count"]`` path (no
    catalog supplied) — also chunk-count-blind now.
    """
    from nexus.scoring import apply_hybrid_scoring

    small = _sr(id="s", distance=0.3, collection="code__repo", chunk_count=5)
    large = _sr(id="l", distance=0.3, collection="code__repo", chunk_count=5000)

    results = apply_hybrid_scoring([small, large], hybrid=False)
    score = {r.id: r.hybrid_score for r in results}
    assert score["s"] == pytest.approx(score["l"], abs=1e-9)


def test_non_hybrid_search_unaffected():
    from nexus.scoring import apply_hybrid_scoring

    results = apply_hybrid_scoring([
        _sr(id="v1", distance=0.2, collection="code__repo"),
        _sr(id="v2", distance=0.8, collection="code__repo"),
    ], hybrid=False)
    assert results[0].id == "v1"
    assert results[1].id == "v2"


@pytest.fixture()
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.commands.search_cmd._CONFIG_DIR", tmp_path)
    (tmp_path / "nexus-a1b2c3d4.cache").touch()
    (tmp_path / "other-e5f6g7h8.cache").touch()
    return tmp_path


@pytest.mark.parametrize("hybrid_default", [True, False])
def test_hybrid_default_config_reaches_the_scorer(cli_env, mock_t3, hybrid_default):
    """`search.hybrid_default` still drives the `hybrid` flag, post-ripgrep.

    The flag used to gate two things: the ripgrep merge and frecency blending
    in apply_hybrid_scoring. nexus-06aei removed the first; the second is the
    whole of what it means now, so the observable this asserts is what the
    scorer is CALLED with, not whether a subprocess ran.
    """
    seen: list[bool] = []

    def fake_boosts(results, *, hybrid=False, **kw):
        seen.append(hybrid)
        return results

    runner = CliRunner()
    with (
        patch("nexus.commands.search_cmd._t3", return_value=mock_t3),
        patched_mcp_infra_t3(mock_t3),
        # A non-empty result set is required: with zero hits the command
        # prints "No results." and returns BEFORE the scorer, so an empty
        # mock would make this test pass vacuously for both parameters.
        patch("nexus.commands.search_cmd.search_cross_corpus",
              return_value=[_sr(id="v1", collection="code__repo")]),
        patch("nexus.commands.search_cmd.apply_ranking_boosts", side_effect=fake_boosts),
        patch("nexus.commands.search_cmd.load_config",
              return_value={"embeddings": {"rerankerModel": "rerank-2.5"},
                            "search": {"hybrid_default": hybrid_default}}),
    ):
        # No --hybrid flag — relies on config
        result = runner.invoke(
            _cli_main(),
            ["search", "query", "--corpus", "code", "--no-rerank"],
        )
    assert result.exit_code == 0, result.output
    assert seen and all(v == hybrid_default for v in seen), seen


