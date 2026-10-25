"""nexus-2k8wi: ``nx taxonomy project`` leaves superseded collections out of its default target set.

A supersede tombstone keeps its topics, so ``get_distinct_collections`` lists it and ``--persist`` would send
projection rows naming its topic ids. The engine skips those rows on its own; the CLI cut keeps the run from
reading the tombstone's centroids in the first place. Both the single-source default and ``--backfill``.
"""

from __future__ import annotations

from typing import Any

from click.testing import CliRunner

from nexus.commands import taxonomy_cmd
from nexus.commands.taxonomy_cmd import _run_backfill, _without_superseded, taxonomy

LIVE_A = "code__1-1__voyage-code-3__v1"
LIVE_B = "docs__1-1__voyage-context-3__v1"
RETIRED = "code__1-1__voyage-code-3__v0"


class _Catalog:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def list_collections(self) -> list[dict[str, Any]]:
        return self._rows


def _registry(monkeypatch, rows: list[dict[str, Any]] | Exception) -> None:
    def make() -> _Catalog:
        if isinstance(rows, Exception):
            raise rows
        return _Catalog(rows)

    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", make)


_ROWS = [
    {"name": LIVE_A, "superseded_by": ""},
    {"name": LIVE_B, "superseded_by": ""},
    {"name": RETIRED, "superseded_by": LIVE_A},
]


def test_without_superseded_drops_only_collections_whose_row_names_a_successor(monkeypatch) -> None:
    _registry(monkeypatch, _ROWS)
    assert _without_superseded([LIVE_A, RETIRED, LIVE_B, "unregistered"]) == [LIVE_A, LIVE_B, "unregistered"]


def test_without_superseded_keeps_the_input_when_the_registry_cannot_be_read(monkeypatch) -> None:
    _registry(monkeypatch, RuntimeError("catalog down"))
    assert _without_superseded([LIVE_A, RETIRED]) == [LIVE_A, RETIRED]


class _Store:
    """The slice of HttpTaxonomyStore that _run_backfill reads."""

    def __init__(self) -> None:
        self.projected: list[tuple[str, list[str]]] = []

    def get_distinct_collections(self) -> list[str]:
        return [LIVE_A, LIVE_B, RETIRED]

    def project_against(self, src: str, targets: list[str], _handle: Any, **_kw: Any) -> dict[str, Any]:
        self.projected.append((src, list(targets)))
        return {"matched_topics": [], "novel_chunks": [], "chunk_assignments": [], "total_chunks": 0,
                "total_centroids": 0}


def test_backfill_neither_projects_from_nor_onto_a_superseded_collection(monkeypatch) -> None:
    _registry(monkeypatch, _ROWS)
    store = _Store()

    _run_backfill(store, object(), persist=True)  # type: ignore[arg-type]

    assert store.projected == [(LIVE_A, [LIVE_B]), (LIVE_B, [LIVE_A])]


def test_single_source_default_targets_leave_a_superseded_collection_out(monkeypatch) -> None:
    _registry(monkeypatch, _ROWS)
    store = _Store()

    class _Db:
        taxonomy = store

        def __enter__(self) -> "_Db":
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr(taxonomy_cmd, "_T2Database", lambda *_a, **_k: _Db())
    monkeypatch.setattr(taxonomy_cmd, "_command_shared_t2_client", lambda: None)
    monkeypatch.setattr("nexus.db.make_t3", lambda: object())
    store.compute_icf_map = lambda **_kw: None  # type: ignore[attr-defined]

    result = CliRunner().invoke(taxonomy, ["project", LIVE_B])

    assert result.exit_code == 0, result.output
    assert store.projected == [(LIVE_B, [LIVE_A])], result.output
