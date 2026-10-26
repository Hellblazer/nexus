"""nexus-h2v0l: the projection CLI and pipelines report rows WRITTEN, say how many the engine skipped
as superseded, and leave superseded collections out of the discover and index projection targets.

The engine (vectors-038, engine-service-v0.1.165) skips a projection row whose topic belongs to a
superseded collection and answers ``skipped_superseded``. Engine 0.1.164 answers no such field and
never skips, so a store that reports ``(N, 0)`` must print exactly what the CLI printed before.
"""

from __future__ import annotations

from typing import Any

import pytest

import nexus.mcp_infra as mcp_infra
from nexus.commands import taxonomy_cmd
from nexus.commands.index import _project_cross_collections, _run_projection_pass
from nexus.commands.taxonomy_cmd import _persist_assignments, _run_backfill, _run_discover_projection

LIVE_A = "code__1-1__voyage-code-3__v1"
LIVE_B = "docs__1-1__voyage-context-3__v1"
RETIRED = "code__1-1__voyage-code-3__v0"

_ROWS = [
    {"name": LIVE_A, "superseded_by": ""},
    {"name": LIVE_B, "superseded_by": ""},
    {"name": RETIRED, "superseded_by": LIVE_A},
]


class _Catalog:
    def list_collections(self) -> list[dict[str, Any]]:
        return _ROWS


@pytest.fixture
def registry(monkeypatch) -> None:
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda: _Catalog())


class _Store:
    """The slice of HttpTaxonomyStore the persist helper and the projection passes touch."""

    def __init__(self, written: int | None = None, skipped: int = 0) -> None:
        self._written = written
        self._skipped = skipped
        self.persisted: list[list[dict[str, Any]]] = []
        self.projected: list[tuple[str, list[str]]] = []

    def persist_assignments_counted(self, assignments: list[dict[str, Any]]) -> tuple[int, int]:
        self.persisted.append(assignments)
        written = len(assignments) if self._written is None else self._written
        return written, self._skipped

    def persist_assignments(self, assignments: list[dict[str, Any]]) -> int:
        return self.persist_assignments_counted(assignments)[0]

    def refresh_projection_links(self) -> int:
        return 0

    def generate_cooccurrence_links(self) -> int:
        return 0

    def project_against(self, src: str, targets: list[str], _handle: Any, **_kw: Any) -> dict[str, Any]:
        self.projected.append((src, list(targets)))
        return {
            "matched_topics": [], "novel_chunks": [], "total_chunks": 2, "total_centroids": 0,
            "chunk_assignments": [("d1", 1, 0.9), ("d2", 2, 0.8)],
        }


def _route_writes_to(monkeypatch, store: _Store) -> None:
    class _Db:
        taxonomy = store

    monkeypatch.setattr(mcp_infra, "t2_index_write", lambda fn, **_kw: fn(_Db()))


# ── _persist_assignments: rows written, not submitted ───────────────────────────


def test_persist_reports_rows_written_and_the_superseded_skips(monkeypatch, capsys) -> None:
    store = _Store(written=3, skipped=2)
    _route_writes_to(monkeypatch, store)

    written = _persist_assignments([("d1", 1, 0.9)] * 5, LIVE_A)

    assert written == 3
    assert capsys.readouterr().out.strip() == (
        "Persisted 3 projection assignment(s), skipped 2 (superseded targets)."
    )


def test_persist_prints_what_it_always_printed_when_the_engine_skips_nothing(monkeypatch, capsys) -> None:
    store = _Store()
    _route_writes_to(monkeypatch, store)

    assert _persist_assignments([("d1", 1, 0.9)] * 5, LIVE_A) == 5
    assert capsys.readouterr().out.strip() == "Persisted 5 projection assignment(s)."


# ── the pipelines count what was written ───────────────────────────────────────


def test_backfill_total_counts_rows_written_not_submitted(monkeypatch, registry, capsys) -> None:
    store = _Store(written=1, skipped=1)
    store.get_distinct_collections = lambda: [LIVE_A, LIVE_B, RETIRED]  # type: ignore[attr-defined]
    _route_writes_to(monkeypatch, store)

    _run_backfill(store, object(), persist=True)  # type: ignore[arg-type]

    # LIVE_A -> LIVE_B and LIVE_B -> LIVE_A each submit 2 rows, the engine writes 1 and skips 1.
    assert "Backfill complete: 2 assignments (2 skipped, superseded targets)," in capsys.readouterr().out


def test_discover_projection_counts_rows_written_and_says_what_was_skipped(
    monkeypatch, registry, capsys,
) -> None:
    store = _Store(written=1, skipped=1)
    _route_writes_to(monkeypatch, store)

    _run_discover_projection(store, [LIVE_A, LIVE_B], object())

    out = capsys.readouterr().out
    assert "Projection: 2 cross-collection assignments" in out
    assert "2 skipped (superseded targets)" in out


def test_discover_projection_line_is_unchanged_when_nothing_was_skipped(
    monkeypatch, registry, capsys,
) -> None:
    store = _Store()
    _route_writes_to(monkeypatch, store)

    _run_discover_projection(store, [LIVE_A, LIVE_B], object())

    out = capsys.readouterr().out
    assert "Projection: 4 cross-collection assignments" in out
    assert "skipped" not in out


def test_index_pass_returns_rows_written_and_the_skip_count(monkeypatch, registry) -> None:
    store = _Store(written=1, skipped=1)
    _route_writes_to(monkeypatch, store)

    total, incomplete, nonfinite, skipped = _project_cross_collections(
        store, [LIVE_A, LIVE_B], None,
    )

    assert (total, incomplete, nonfinite, skipped) == (2, 0, 0, 2)


# ── discover and index leave a superseded collection out of the targets ────────


def test_discover_projection_neither_projects_from_nor_onto_a_superseded_collection(
    monkeypatch, registry,
) -> None:
    store = _Store()
    _route_writes_to(monkeypatch, store)

    _run_discover_projection(store, [LIVE_A, RETIRED, LIVE_B], object())

    assert store.projected == [(LIVE_A, [LIVE_B]), (LIVE_B, [LIVE_A])]


def test_index_pass_neither_projects_from_nor_onto_a_superseded_collection(
    monkeypatch, registry,
) -> None:
    store = _Store()
    _route_writes_to(monkeypatch, store)

    _project_cross_collections(store, [LIVE_A, RETIRED, LIVE_B], None)

    assert store.projected == [(LIVE_A, [LIVE_B]), (LIVE_B, [LIVE_A])]


def test_index_pass_says_how_many_assignments_were_skipped(monkeypatch, registry) -> None:
    store = _Store(written=1, skipped=1)
    _route_writes_to(monkeypatch, store)
    said: list[str] = []

    _run_projection_pass(store, [LIVE_A, LIVE_B], None, said.append)

    assert any("2 cross-collection assignments" in line for line in said)
    assert any("2 assignment(s) skipped (superseded targets)" in line for line in said)


def test_index_pass_is_silent_about_skips_when_the_engine_skipped_none(monkeypatch, registry) -> None:
    store = _Store()
    _route_writes_to(monkeypatch, store)
    said: list[str] = []

    _run_projection_pass(store, [LIVE_A, LIVE_B], None, said.append)

    assert any("4 cross-collection assignments" in line for line in said)
    assert not any("skipped" in line for line in said)
