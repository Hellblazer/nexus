# SPDX-License-Identifier: AGPL-3.0-or-later
"""GH-1061 E1: nx doctor reports FAILURE (not green) on embedding-dimension mismatch.

When stored collections are 768d (bge) but the active embedder is 384d (MiniLM
fallback), every collection is unqueryable.  Previously doctor printed:
  ✓ Local collections: could not query

That is a GREEN check on a total search outage.  The fix: compare active embedder
dimension against stored collection dimensions via probe query; flag partial or
total mismatch as FAILURE or WARN respectively, with actionable remediation text.

Tests drive ``_check_t3_local()`` directly (not via the full doctor command) to
isolate the dimension-mismatch logic without needing to stub every other health
sub-check.  Integration-style assertions check ``HealthResult.ok``, ``.warn``,
``.fatal``, and ``.fix_suggestions``.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _make_col(name: str, stored_dim: int, count: int = 10) -> MagicMock:
    """Chromadb collection mock that accepts/rejects queries by dimension."""
    col = MagicMock()
    col.name = name
    col.count.return_value = count

    def _query(query_embeddings, n_results=1, **kw):
        given_dim = len(query_embeddings[0]) if query_embeddings else 0
        if given_dim != stored_dim:
            raise Exception(
                f"Collection expecting embedding with dimension of {stored_dim}, "
                f"got {given_dim}"
            )
        return {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}

    col.query.side_effect = _query
    return col


def _make_ef(model_name: str, dimensions: int) -> MagicMock:
    ef = MagicMock()
    ef.model_name = model_name
    ef.dimensions = dimensions
    return ef


def _make_t3_client_stub(cols: list) -> MagicMock:
    """Stub make_t3_client() return value."""
    t3 = MagicMock()
    t3._client.list_collections.return_value = cols
    return t3


def _run_check_t3_local(*, ef, cols, tmp_path: Path):
    """Call _check_t3_local() with mocked embedding function and T3 client."""
    from nexus.health import _check_t3_local

    chroma_path = tmp_path / "chroma"
    chroma_path.mkdir()
    (chroma_path / "dummy").write_bytes(b"x" * 1024)

    t3_stub = _make_t3_client_stub(cols)

    with (
        patch("nexus.config._default_local_path", return_value=chroma_path),
        patch("nexus.db.local_ef.LocalEmbeddingFunction", return_value=ef),
        patch("nexus.daemon.t3_client.make_t3_client", return_value=t3_stub),
    ):
        return _check_t3_local()


class TestDoctorDimensionMismatchTotalOutage:
    """When ALL non-empty collections are 768d but active ef is 384d, result must FAIL."""

    def test_total_mismatch_has_fail_result(self, tmp_path: Path) -> None:
        ef = _make_ef("all-MiniLM-L6-v2", 384)
        cols = [_make_col("knowledge__test__minilm__v1", 768)]
        results = _run_check_t3_local(ef=ef, cols=cols, tmp_path=tmp_path)

        dim_results = [r for r in results if "dimension" in r.label.lower()]
        assert dim_results, (
            f"Expected a dimension-check HealthResult, got none.\n"
            f"All results: {[(r.label, r.ok, r.warn, r.fatal) for r in results]}"
        )

    def test_total_mismatch_is_not_ok(self, tmp_path: Path) -> None:
        ef = _make_ef("all-MiniLM-L6-v2", 384)
        cols = [_make_col("knowledge__test__bge__v1", 768)]
        results = _run_check_t3_local(ef=ef, cols=cols, tmp_path=tmp_path)

        dim_results = [r for r in results if "dimension" in r.label.lower()]
        assert dim_results, "Expected a dimension HealthResult"
        dim_r = dim_results[0]
        assert not dim_r.ok, (
            f"Total dimension mismatch must not be ok=True; got ok={dim_r.ok}"
        )

    def test_total_mismatch_is_fatal(self, tmp_path: Path) -> None:
        """A total mismatch (no queryable collections) must be fatal."""
        ef = _make_ef("all-MiniLM-L6-v2", 384)
        cols = [
            _make_col("knowledge__a__bge__v1", 768),
            _make_col("code__b__bge__v1", 768),
        ]
        results = _run_check_t3_local(ef=ef, cols=cols, tmp_path=tmp_path)

        dim_results = [r for r in results if "dimension" in r.label.lower()]
        assert dim_results, "Expected a dimension HealthResult"
        dim_r = dim_results[0]
        assert dim_r.fatal, (
            f"Total mismatch must be fatal=True; got fatal={dim_r.fatal}, warn={dim_r.warn}"
        )
        # Total mismatch is hard fail, not soft warn
        assert not dim_r.warn, (
            f"Total mismatch must not be warn=True; got warn={dim_r.warn}"
        )

    def test_total_mismatch_has_remediation(self, tmp_path: Path) -> None:
        ef = _make_ef("all-MiniLM-L6-v2", 384)
        cols = [_make_col("knowledge__test__bge__v1", 768)]
        results = _run_check_t3_local(ef=ef, cols=cols, tmp_path=tmp_path)

        dim_results = [r for r in results if "dimension" in r.label.lower()]
        assert dim_results, "Expected a dimension HealthResult"
        dim_r = dim_results[0]
        # Must have fix suggestions
        assert dim_r.fix_suggestions, (
            f"Expected fix_suggestions on dimension mismatch result, got none.\ndetail={dim_r.detail!r}"
        )
        # Detail must mention dimensions
        detail_lower = dim_r.detail.lower()
        assert "384" in dim_r.detail or "dimension" in detail_lower, (
            f"Expected dimension info in detail: {dim_r.detail!r}"
        )
        # Fix suggestions must mention remediation
        fixes_text = " ".join(dim_r.fix_suggestions)
        assert "conexus[local]" in fixes_text or "reindex" in fixes_text, (
            f"Expected remediation in fix_suggestions: {dim_r.fix_suggestions}"
        )


class TestDoctorDimensionMatchStaysGreen:
    """When ef dimension matches stored collections, no mismatch result is added."""

    def test_matching_dims_no_mismatch_result(self, tmp_path: Path) -> None:
        ef = _make_ef("bge-base-en-v1.5", 768)
        cols = [_make_col("knowledge__test__bge__v1", 768, count=5)]
        results = _run_check_t3_local(ef=ef, cols=cols, tmp_path=tmp_path)

        dim_results = [r for r in results if "dimension" in r.label.lower()]
        assert not dim_results, (
            f"Matching dims should not produce a dimension HealthResult; got: "
            f"{[(r.label, r.ok, r.fatal) for r in dim_results]}"
        )

    def test_no_collections_no_mismatch(self, tmp_path: Path) -> None:
        """Zero stored collections: no mismatch to report."""
        ef = _make_ef("all-MiniLM-L6-v2", 384)
        cols: list = []
        results = _run_check_t3_local(ef=ef, cols=cols, tmp_path=tmp_path)

        dim_results = [r for r in results if "dimension" in r.label.lower()]
        assert not dim_results, (
            f"Zero collections must not produce a dimension check:\n"
            f"{[(r.label, r.ok) for r in dim_results]}"
        )

    def test_empty_only_collections_no_mismatch(self, tmp_path: Path) -> None:
        """Empty collections (count=0) are excluded from probe."""
        ef = _make_ef("all-MiniLM-L6-v2", 384)
        cols = [_make_col("knowledge__empty__bge__v1", 768, count=0)]
        results = _run_check_t3_local(ef=ef, cols=cols, tmp_path=tmp_path)

        dim_results = [r for r in results if "dimension" in r.label.lower()]
        assert not dim_results, (
            f"Empty collections should not trigger dimension check:\n"
            f"{[(r.label, r.ok) for r in dim_results]}"
        )


class TestDoctorDimensionMismatchPartial:
    """Partial mismatch (some match, some don't) must warn (not hard-fail)."""

    def test_partial_mismatch_is_warn_not_fatal(self, tmp_path: Path) -> None:
        ef = _make_ef("bge-base-en-v1.5", 768)
        cols = [
            _make_col("knowledge__a__bge__v1", 768),   # matches
            _make_col("code__b__minilm__v1", 384),      # mismatches
        ]
        results = _run_check_t3_local(ef=ef, cols=cols, tmp_path=tmp_path)

        dim_results = [r for r in results if "dimension" in r.label.lower()]
        assert dim_results, (
            "Expected a dimension HealthResult for partial mismatch"
        )
        dim_r = dim_results[0]
        # Partial: some collections work — must NOT be hard fatal
        assert not dim_r.fatal, (
            f"Partial mismatch must not be fatal; got fatal={dim_r.fatal}, warn={dim_r.warn}"
        )
        # Must be soft warn, not hard fail
        assert dim_r.warn, (
            f"Partial mismatch must be warn=True; got warn={dim_r.warn}"
        )
        assert not dim_r.ok, (
            f"Partial mismatch must not be ok=True; got ok={dim_r.ok}"
        )

    def test_partial_mismatch_is_not_ok(self, tmp_path: Path) -> None:
        ef = _make_ef("bge-base-en-v1.5", 768)
        cols = [
            _make_col("knowledge__a__bge__v1", 768),
            _make_col("code__b__minilm__v1", 384),
        ]
        results = _run_check_t3_local(ef=ef, cols=cols, tmp_path=tmp_path)

        dim_results = [r for r in results if "dimension" in r.label.lower()]
        assert dim_results, "Expected a dimension HealthResult"
        dim_r = dim_results[0]
        assert not dim_r.ok, (
            f"Partial mismatch must not be ok=True; got ok={dim_r.ok}"
        )
