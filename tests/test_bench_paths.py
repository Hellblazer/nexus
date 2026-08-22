# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-hmu02: the RDR-090 retrieval bench read the legacy ``source_path``
chunk key, which catalog-aware indexing no longer writes (``_display_path``
is the catalog-resolved path, ``formatters._display_path``), so every
NDCG@3 was 0.0 against a freshly indexed corpus. Path B/C additionally
resolved chash -> path through a raw chromadb client retired at RDR-155.

These tests pin: path-key priority, the catalog-backed resolver, the
injectable ``nx_answer`` seam, and the non-vacuity guard (a result set
whose every chunk has an empty path is an ERROR, not a 0.0 score).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from bench.paths import (  # noqa: E402
    _chunk_path,
    _resolve_paths_via_catalog,
    _run_nx_answer,
    run_path_a,
    run_path_c,
)
from bench.schema import Query  # noqa: E402


def _q() -> Query:
    return Query(
        qid="Q1", text="tumblers", category="factual",
        ground_truth={"rdr-049-": 3, "rdr-053-": 2},
    )


class TestChunkPath:
    def test_prefers_display_path(self) -> None:
        c = {"_display_path": "/a/rdr-049-x.md", "source_path": "/legacy.md"}
        assert _chunk_path(c) == "/a/rdr-049-x.md"

    def test_falls_back_to_source_path_then_file_path(self) -> None:
        assert _chunk_path({"source_path": "/s.md"}) == "/s.md"
        assert _chunk_path({"file_path": "/f.md"}) == "/f.md"
        assert _chunk_path({"_display_path": None, "source_path": ""}) == ""
        assert _chunk_path({}) == ""


class _FakeCatalog:
    def __init__(self, chash_to_docs: dict[str, list[str]], doc_paths: dict[str, str]):
        self._c2d = chash_to_docs
        self._paths = doc_paths
        self.docs_calls: list[list[str]] = []
        self.resolve_calls: list[list[str]] = []

    def docs_for_chashes(self, chashes: list[str]) -> dict[str, list[str]]:
        self.docs_calls.append(list(chashes))
        return {c: self._c2d[c] for c in chashes if c in self._c2d}

    def resolve_many(self, doc_ids: list[str]) -> dict:
        self.resolve_calls.append(list(doc_ids))
        return {
            d: SimpleNamespace(file_path=self._paths[d])
            for d in doc_ids if d in self._paths
        }


class TestResolvePathsViaCatalog:
    def test_batches_two_round_trips_and_maps_chash_to_path(self) -> None:
        cat = _FakeCatalog(
            {"c1": ["1.1"], "c2": ["1.2"], "c3": ["1.1"]},
            {"1.1": "/x/rdr-049-a.md", "1.2": "/x/rdr-053-b.md"},
        )
        out = _resolve_paths_via_catalog(cat, ["c1", "c2", "c3", "c2", ""])
        assert out == {
            "c1": "/x/rdr-049-a.md", "c2": "/x/rdr-053-b.md", "c3": "/x/rdr-049-a.md",
        }
        assert len(cat.docs_calls) == 1 and len(cat.resolve_calls) == 1
        assert sorted(cat.docs_calls[0]) == ["c1", "c2", "c3"]

    def test_shared_chash_picks_lowest_doc_id_deterministically(self) -> None:
        cat = _FakeCatalog(
            {"c1": ["1.9", "1.2", "1.5"]},
            {"1.9": "/x/nine.md", "1.2": "/x/two.md", "1.5": "/x/five.md"},
        )
        assert _resolve_paths_via_catalog(cat, ["c1"]) == {"c1": "/x/two.md"}

    def test_unknown_chash_maps_to_empty(self) -> None:
        cat = _FakeCatalog({}, {})
        assert _resolve_paths_via_catalog(cat, ["zz"]) == {"zz": ""}

    def test_none_catalog_yields_empty_paths(self) -> None:
        assert _resolve_paths_via_catalog(None, ["c1"]) == {"c1": ""}


def _fake_search_proc(chunks: list[dict], rc: int = 0) -> object:
    return subprocess.CompletedProcess(
        args=[], returncode=rc, stdout=json.dumps(chunks), stderr="",
    )


class TestRunPathA:
    def test_grades_from_display_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        chunks = [
            {"id": "a", "_display_path": "/r/rdr-049-x.md", "distance": 0.1},
            {"id": "b", "_display_path": "/r/rdr-049-x.md", "distance": 0.2},
            {"id": "c", "_display_path": "/r/rdr-053-y.md", "distance": 0.3},
            {"id": "d", "_display_path": "/r/rdr-100-z.md", "distance": 0.4},
        ]
        monkeypatch.setattr(
            "bench.paths.subprocess.run", lambda *a, **k: _fake_search_proc(chunks),
        )
        row = run_path_a(_q(), corpus="rdr__x")
        assert row["error"] is None and row["unresolved_count"] == 0
        assert [c["source_path"] for c in row["chunks"]] == [
            "/r/rdr-049-x.md", "/r/rdr-053-y.md", "/r/rdr-100-z.md",
        ]
        assert row["grades"] == [3, 2, 0]
        assert row["ndcg_at_3"] == pytest.approx(1.0)

    def test_all_empty_paths_is_an_error_not_a_zero(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        chunks = [{"id": "a", "distance": 0.1}, {"id": "b", "source_path": ""}]
        monkeypatch.setattr(
            "bench.paths.subprocess.run", lambda *a, **k: _fake_search_proc(chunks),
        )
        row = run_path_a(_q(), corpus="rdr__x")
        assert row["error"] is not None and "vacuous" in row["error"]
        assert row["raw_chunk_count"] == 2 and row["unresolved_count"] == 2
        assert row["grades"] == [] and row["ndcg_at_3"] == 0.0

    def test_partial_unresolved_scores_but_reports_count(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        chunks = [
            {"id": "a", "distance": 0.1},
            {"id": "b", "_display_path": "/r/rdr-049-x.md", "distance": 0.2},
        ]
        monkeypatch.setattr(
            "bench.paths.subprocess.run", lambda *a, **k: _fake_search_proc(chunks),
        )
        row = run_path_a(_q(), corpus="rdr__x")
        assert row["error"] is None
        assert row["unresolved_count"] == 1
        assert row["grades"] == [0, 3]

    def test_zero_chunks_is_not_flagged_vacuous(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "bench.paths.subprocess.run", lambda *a, **k: _fake_search_proc([]),
        )
        row = run_path_a(_q(), corpus="rdr__x")
        assert row["error"] is None and row["ndcg_at_3"] == 0.0


class TestRunNxAnswer:
    def test_resolves_envelope_chunks_through_catalog(self) -> None:
        cat = _FakeCatalog(
            {"c1": ["1.1"], "c2": ["1.2"]},
            {"1.1": "/x/rdr-053-b.md", "1.2": "/x/rdr-049-a.md"},
        )

        async def fake_answer(**kw):
            assert kw["structured"] is True
            return {
                "final_text": "t", "plan_id": 7, "step_count": 2,
                "chunks": [
                    {"id": "c1", "chash": "c1", "collection": "rdr__x", "distance": 0.1},
                    {"id": "c2", "chash": "c2", "collection": "rdr__x", "distance": 0.2},
                ],
            }

        row = _run_nx_answer(
            _q(), cat, path_label="B", answer_kwargs={"scope": "rdr"},
            answer_fn=fake_answer,
        )
        assert row["error"] is None
        assert row["grades"] == [2, 3]
        assert row["plan_id"] == 7 and row["step_count"] == 2
        assert 0.0 < row["ndcg_at_3"] < 1.0

    def test_all_unresolved_chunks_is_vacuous_error(self) -> None:
        cat = _FakeCatalog({}, {})

        async def fake_answer(**kw):
            return {"final_text": "t", "plan_id": 1, "step_count": 1,
                    "chunks": [{"id": "c9", "chash": "c9", "collection": "rdr__x"}]}

        row = _run_nx_answer(
            _q(), cat, path_label="C", answer_kwargs={"force_dynamic": True},
            answer_fn=fake_answer,
        )
        assert row["error"] is not None and "vacuous" in row["error"]

    def test_catalog_failure_is_a_typed_error_row(self) -> None:
        class _Broken:
            def docs_for_chashes(self, chashes):
                raise ConnectionError("engine down")

        async def fake_answer(**kw):
            return {"final_text": "t", "plan_id": 3, "step_count": 1,
                    "chunks": [{"id": "c1", "chash": "c1", "collection": "rdr__x"}]}

        row = _run_nx_answer(
            _q(), _Broken(), path_label="B", answer_kwargs={}, answer_fn=fake_answer,
        )
        assert row["error"].startswith("catalog_resolve: ConnectionError")
        assert row["plan_id"] == 3 and row["raw_chunk_count"] == 1

    def test_no_chunks_envelope_is_a_plain_zero(self) -> None:
        async def fake_answer(**kw):
            return {"final_text": "t", "plan_id": 1, "step_count": 1, "chunks": []}

        row = _run_nx_answer(
            _q(), None, path_label="B", answer_kwargs={}, answer_fn=fake_answer,
        )
        assert row["error"] is None and row["ndcg_at_3"] == 0.0


class TestRunPathC:
    def test_successful_run_is_not_mistaken_for_unsupported_kwarg(self) -> None:
        """Pre-fix: res.get("error", "") was None on success -> AttributeError."""
        cat = _FakeCatalog({"c1": ["1.1"]}, {"1.1": "/x/rdr-049-a.md"})
        calls: list[dict] = []

        async def fake_answer(**kw):
            calls.append(kw)
            return {"final_text": "t", "plan_id": 0, "step_count": 1,
                    "chunks": [{"id": "c1", "chash": "c1", "collection": "rdr__x"}]}

        import bench.paths as bp
        orig = bp._run_nx_answer
        bp._run_nx_answer = lambda q, c, **kw: orig(q, c, answer_fn=fake_answer, **kw)
        try:
            row = run_path_c(_q(), cat, corpus="rdr__x")
        finally:
            bp._run_nx_answer = orig
        assert row["error"] is None and row["grades"] == [3]
        assert "fallback" not in row
        assert len(calls) == 1 and calls[0]["force_dynamic"] is True
