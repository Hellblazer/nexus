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


# ── nexus-tr1rj.13: a MinerU stop is contained like the quality gate, and stops only the PDFs ──


def _trip(name: str = "b.pdf"):
    from nexus.pdf_extractor import MineruSystemicFailure

    return MineruSystemicFailure("timeout", "", [1, 2, 3], name)


def _machine():
    from tests._mineru_machine_error import real_machine_error

    return real_machine_error()


@pytest.mark.parametrize("make", [_trip, lambda: _machine()], ids=["breaker-trip", "machine-resource"])
def test_a_mineru_stop_is_contained_and_recorded(make) -> None:
    """The run's other files (code, prose, RDR loops) must not be cancelled by run_file_loop's
    first-exception rule: the stop is contained, the file recorded as the one that tripped."""
    from nexus.indexer import _MineruStop, _contain_mineru_stop

    stop = _MineruStop()

    def boom() -> int:
        raise make()

    assert _contain_mineru_stop(boom, Path("/repo/b.pdf"), stop) == 0
    assert stop.tripped == ["/repo/b.pdf"]
    assert stop.stopped_at == "/repo/b.pdf"
    assert stop.reason, "the run output can say why it stopped"


def test_after_a_mineru_stop_the_remaining_pdfs_are_listed_not_attempted() -> None:
    from nexus.indexer import _MineruStop, _contain_mineru_stop

    stop = _MineruStop()
    calls: list[str] = []

    def index(name: str):
        def _run() -> int:
            calls.append(name)
            if name == "b.pdf":
                raise _trip(name)
            return 3
        return _run

    results = [_contain_mineru_stop(index(n), Path("/repo") / n, stop) for n in ("a.pdf", "b.pdf", "c.pdf", "d.pdf")]

    assert calls == ["a.pdf", "b.pdf"], "no PDF after the stop is attempted"
    assert results == [3, 0, 0, 0]
    assert stop.tripped == ["/repo/b.pdf"]
    assert stop.not_attempted == ["/repo/c.pdf", "/repo/d.pdf"]


def test_a_mineru_stop_inside_run_file_loop_does_not_abort_the_loop() -> None:
    """Through the real loop: without the containment the first MinerU stop re-raises and cancels
    every not-yet-started file (the control); with it the loop returns, counting only what wrote."""
    from nexus.indexer import _MineruStop, _contain_mineru_stop
    from nexus.indexer_utils import run_file_loop

    files = [(1.0, Path(f"/repo/{n}.pdf")) for n in "abcd"]

    def index_one(file: Path, score: float, timers: object) -> int:
        if file.name == "b.pdf":
            raise _trip(file.name)
        return 2

    with pytest.raises(type(_trip())):
        run_file_loop(files, index_one, concurrency=1, on_file=None, on_stage_timers=None)

    stop = _MineruStop()
    written = run_file_loop(
        files, lambda f, s, t: _contain_mineru_stop(lambda: index_one(f, s, t), f, stop),
        concurrency=1, on_file=None, on_stage_timers=None,
    )
    assert written == 1, "only a.pdf wrote"
    assert stop.not_attempted == ["/repo/c.pdf", "/repo/d.pdf"]


def test_a_non_mineru_error_still_propagates_through_the_mineru_containment() -> None:
    from nexus.indexer import _MineruStop, _contain_mineru_stop

    stop = _MineruStop()

    def boom() -> int:
        raise ValueError("unrelated")

    with pytest.raises(ValueError):
        _contain_mineru_stop(boom, _FILE, stop)
    assert stop.tripped == [] and stop.stopped_at is None


def test_the_stop_reason_names_the_failure_class_or_the_machine() -> None:
    from nexus.pdf_extractor import mineru_stop_reason

    what, detail, remedy = mineru_stop_reason(_trip())
    assert (what, detail) == ("MinerU failed systemically", "failure class timeout")
    assert "nx doctor" in remedy
    what, detail, remedy = mineru_stop_reason(_machine())
    assert (what, detail) == ("MinerU cannot run on this machine", "POSIX named semaphores exhausted")
    assert remedy == str(_machine()), "the exception's own remedy text, once"
    assert len(detail) < 60
