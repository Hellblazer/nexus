# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-wi1uv round-2 (code-review-expert + substantive-critic Critical,
both independently, 2026-08-06): a PDF failing the post-extraction quality
gate must fail THAT file only during ``nx index repo`` — never propagate
through ``run_file_loop``'s first-exception-cancels-all contract and abort
the whole run (code/prose/PDF/RDR alike). Same containment shape as
``_contain_transient_upsert`` (tests/test_index_transient_containment.py),
tested the same way: pure unit tests on the containment function, plus a
CLI-level test on the non-zero-exit wiring.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus.errors import ExtractionQualityError
from nexus.indexer import _contain_extraction_quality_gate

_FILE = Path("/repo/paper.pdf")


def test_success_returns_value() -> None:
    failed: list[str] = []
    assert _contain_extraction_quality_gate(lambda: 7, _FILE, failed) == 7
    assert failed == []


def test_quality_gate_failure_contained_returns_zero() -> None:
    def boom() -> int:
        raise ExtractionQualityError("PDF paper.pdf failed the post-extraction quality gate: boom")

    failed: list[str] = []
    assert _contain_extraction_quality_gate(boom, _FILE, failed) == 0


def test_quality_gate_failure_recorded_in_failed_list() -> None:
    def boom() -> int:
        raise ExtractionQualityError("boom")

    failed: list[str] = []
    _contain_extraction_quality_gate(boom, _FILE, failed)
    assert failed == [str(_FILE)]


def test_multiple_failures_all_recorded() -> None:
    def boom() -> int:
        raise ExtractionQualityError("boom")

    failed: list[str] = []
    _contain_extraction_quality_gate(boom, Path("/repo/a.pdf"), failed)
    _contain_extraction_quality_gate(boom, Path("/repo/b.pdf"), failed)
    assert sorted(failed) == ["/repo/a.pdf", "/repo/b.pdf"]


def test_non_quality_gate_error_propagates() -> None:
    """A genuine bug (or any other exception class) must never be
    swallowed by this containment — only ExtractionQualityError is
    per-file-recoverable here."""
    def boom() -> int:
        raise ValueError("unrelated")

    failed: list[str] = []
    with pytest.raises(ValueError):
        _contain_extraction_quality_gate(boom, _FILE, failed)
    assert failed == []


def test_other_nexus_error_propagates() -> None:
    from nexus.errors import IndexingError

    def boom() -> int:
        raise IndexingError("unrelated indexing failure")

    failed: list[str] = []
    with pytest.raises(IndexingError):
        _contain_extraction_quality_gate(boom, _FILE, failed)
    assert failed == []


def test_composes_with_transient_upsert_containment() -> None:
    """The production wiring nests this containment inside
    _contain_transient_upsert (indexer._index_one_pdf) — verify the two
    compose without one masking the other's exception class."""
    from nexus.db.http_vector_client import VectorServiceError
    from nexus.indexer import _contain_transient_upsert

    def quality_boom() -> int:
        raise ExtractionQualityError("boom")

    def transient_boom() -> int:
        raise VectorServiceError("gateway said 504", code=504)

    failed: list[str] = []
    # Quality-gate failure: contained by the inner wrapper, outer sees a
    # plain 0 return (not an exception) and passes it through.
    assert _contain_transient_upsert(
        lambda: _contain_extraction_quality_gate(quality_boom, _FILE, failed), _FILE,
    ) == 0
    assert failed == [str(_FILE)]

    # Transient upsert failure: the inner quality-gate wrapper doesn't
    # catch VectorServiceError, so it propagates to the outer wrapper,
    # which contains it exactly as before (nexus-7yfe6, unaffected).
    failed2: list[str] = []
    assert _contain_transient_upsert(
        lambda: _contain_extraction_quality_gate(transient_boom, _FILE, failed2), _FILE,
    ) == 0
    assert failed2 == []
