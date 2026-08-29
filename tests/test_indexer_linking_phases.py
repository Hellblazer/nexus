# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-jg3x5: the catalog hook's three link generators are their own
``[post]`` sub-phases.

``Catalog: linking N new entries…`` was followed by tens of seconds of
silence on a real ``nx index repo`` even though rdr / prose / pdf were
already timed individually into ``catalog_hook_stage_timing``. The data
existed; only the emission was missing. These tests pin:

- each generator that CAN run for the batch emits a start marker and a
  done marker carrying the SAME duration the stage-timing event records
  (a deterministic clock, so the number is the measured one — never a
  re-measurement, never zero);
- the pairs appear in generator order;
- a batch with no document of a generator's source type emits NO pair for
  it — an unconditional emitter would claim a phase that never ran, the
  honesty class this family exists to fix;
- no line is a ``[N/N]`` progress line and none carries ``\r``.
"""

from __future__ import annotations

import re
import time
import types
from pathlib import Path

import pytest

import nexus.catalog.link_generator as link_generator
import nexus.indexer as indexer_mod
from tests._catalog_fixture_ops import ActiveCatalog

_PROGRESS_LINE = re.compile(r"^\s*\[[0-9]+/[0-9]+\]")


@pytest.fixture(autouse=True)
def git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@test.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@test.invalid")


@pytest.fixture(autouse=True)
def _point_catalog_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(tmp_path / "catalog"))


class _Clock:
    """Monotonic clock that only moves when a stub generator moves it."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    clk = _Clock()
    fake_time = types.SimpleNamespace(
        **{k: getattr(time, k) for k in dir(time) if not k.startswith("__")},
    )
    fake_time.monotonic = clk.monotonic
    monkeypatch.setattr(indexer_mod, "time", fake_time)
    return clk


def _stub_generators(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock, durations: dict[str, float],
) -> list[str]:
    """Replace the three generators with stubs that advance *clock* by the
    configured seconds and record their call order."""
    calls: list[str] = []

    def make(kind: str):
        def _gen(cat, *, writer=None, new_tumblers=None, new_content_types=None):  # noqa: ANN001
            calls.append(kind)
            clock.now += durations[kind]
            return 1
        return _gen

    monkeypatch.setattr(link_generator, "generate_rdr_filepath_links", make("rdr"))
    monkeypatch.setattr(link_generator, "generate_prose_filepath_links", make("prose"))
    monkeypatch.setattr(link_generator, "generate_pdf_corpus_links", make("pdf"))
    return calls


def _write(repo: Path, rel: str, text: str) -> Path:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def _run_hook(repo: Path, indexed_files: list[tuple[Path, str, str]]) -> list[str]:
    ActiveCatalog()
    phases: list[str] = []
    indexer_mod._catalog_hook(
        repo=repo, repo_name="nexus", repo_hash="571b8edd", head_hash="abc",
        indexed_files=indexed_files, on_phase=phases.append,
    )
    return phases


def _linking_lines(phases: list[str]) -> list[str]:
    return [m for m in phases if m.startswith("Catalog linking")]


class TestLinkingSubPhases:
    def test_pairs_in_order_with_measured_durations_and_no_phantom_pdf(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: _Clock,
    ) -> None:
        calls = _stub_generators(
            monkeypatch, clock, {"rdr": 8.1, "prose": 25.9, "pdf": 3.4},
        )
        rdr = _write(tmp_path, "docs/rdr/rdr-001-x.md", "# RDR-001\n")
        prose = _write(tmp_path, "docs/guide.md", "# guide\n")

        phases = _run_hook(tmp_path, [
            (rdr, "rdr", "rdr__nexus"), (prose, "prose", "docs__nexus"),
        ])

        assert _linking_lines(phases) == [
            "Catalog linking: rdr…",
            "Catalog linking: rdr done (8.1s)",
            "Catalog linking: prose…",
            "Catalog linking: prose done (25.9s)",
        ]
        # The pdf generator still RUNS (its own seed check makes it a no-op);
        # it just must not claim a phase for a batch with no pdf document.
        assert calls == ["rdr", "prose", "pdf"]
        assert not any("pdf" in m for m in _linking_lines(phases))
        for m in phases:
            assert "\r" not in m
            assert not _PROGRESS_LINE.match(m)

    def test_all_three_categories_emit_six_lines(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: _Clock,
    ) -> None:
        _stub_generators(
            monkeypatch, clock, {"rdr": 8.1, "prose": 25.9, "pdf": 3.4},
        )
        rdr = _write(tmp_path, "docs/rdr/rdr-001-x.md", "# RDR-001\n")
        prose = _write(tmp_path, "docs/guide.md", "# guide\n")
        pdf = _write(tmp_path, "papers/p.pdf", "%PDF-1.4 stub\n")

        phases = _run_hook(tmp_path, [
            (rdr, "rdr", "rdr__nexus"), (prose, "prose", "docs__nexus"),
            (pdf, "pdf", "knowledge__nexus"),
        ])

        assert _linking_lines(phases) == [
            "Catalog linking: rdr…",
            "Catalog linking: rdr done (8.1s)",
            "Catalog linking: prose…",
            "Catalog linking: prose done (25.9s)",
            "Catalog linking: pdf…",
            "Catalog linking: pdf done (3.4s)",
        ]

    def test_no_on_phase_means_no_emission_and_no_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: _Clock,
    ) -> None:
        """Callers that pass no ``on_phase`` (every pre-existing call site
        and test) see byte-identical behavior."""
        calls = _stub_generators(monkeypatch, clock, {"rdr": 1.0, "prose": 1.0, "pdf": 1.0})
        rdr = _write(tmp_path, "docs/rdr/rdr-002-y.md", "# RDR-002\n")
        ActiveCatalog()
        indexer_mod._catalog_hook(
            repo=tmp_path, repo_name="nexus", repo_hash="571b8edd", head_hash="abc",
            indexed_files=[(rdr, "rdr", "rdr__nexus")],
        )
        assert calls == ["rdr", "prose", "pdf"]


class TestGeneratorFailureClosesItsPhase:
    def test_raising_generator_emits_failed_marker_not_done(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: _Clock,
    ) -> None:
        """A generator that raises AFTER its start marker must close the
        phase with a ``failed`` marker: the orchestrator's heartbeat
        repeats the last label, and the next on_phase line only fires
        after housekeeping — otherwise the operator sees "Catalog
        linking: prose… still running" for a phase that already died.
        The hook itself still returns (the outer handler logs + audits)."""
        calls = _stub_generators(monkeypatch, clock, {"rdr": 8.1, "prose": 0.0, "pdf": 3.4})

        def boom(cat, *, writer=None, new_tumblers=None, new_content_types=None):  # noqa: ANN001
            calls.append("prose")
            clock.now += 2.5
            raise RuntimeError("prose generator exploded")

        monkeypatch.setattr(link_generator, "generate_prose_filepath_links", boom)
        rdr = _write(tmp_path, "docs/rdr/rdr-003-z.md", "# RDR-003\n")
        prose = _write(tmp_path, "docs/guide.md", "# guide\n")

        phases = _run_hook(tmp_path, [
            (rdr, "rdr", "rdr__nexus"), (prose, "prose", "docs__nexus"),
        ])

        assert _linking_lines(phases) == [
            "Catalog linking: rdr…",
            "Catalog linking: rdr done (8.1s)",
            "Catalog linking: prose…",
            "Catalog linking: prose failed (2.5s)",
        ]
        # The failure short-circuits the remaining generators, exactly as
        # before this emission existed (links_created stays 0).
        assert calls == ["rdr", "prose"]


class TestIncrementalGeneratorApplies:
    def test_kind_applies_only_with_a_qualifying_new_tumbler(self) -> None:
        applies = link_generator.incremental_generator_applies
        assert applies("rdr", ["1.2.3"], {"rdr", "prose"}) is True
        assert applies("prose", ["1.2.3"], {"rdr", "prose"}) is True
        assert applies("pdf", ["1.2.3"], {"rdr", "prose"}) is False
        assert applies("pdf", ["1.2.3"], {"paper"}) is True
        # Unknown content types (None) cannot prove the generator idle.
        assert applies("pdf", ["1.2.3"], None) is True
        # An EMPTY batch has nothing to link: no phase.
        assert applies("rdr", [], {"rdr"}) is False
        # None is the FULL-SCAN shape: every generator runs unconditionally
        # (mirrors _no_qualifying_seed, which cannot skip on None).
        assert applies("rdr", None, {"rdr"}) is True
        assert applies("pdf", None, {"rdr"}) is True

    def test_unknown_kind_is_a_programming_error(self) -> None:
        with pytest.raises(KeyError):
            link_generator.incremental_generator_applies("code", ["1.2.3"], {"code"})
