# SPDX-License-Identifier: AGPL-3.0-or-later
"""Probe 3a — ``nx doctor --check-search`` name-resolution canary.

RDR-087 Phase 3.2 (nexus-yi4b.3.2). Probe walks a canary list through
``resolve_corpus`` / ``rdr_resolve`` / ``resolve_span`` and classifies
each dispatch as ``matched`` / ``empty`` / ``error``. ``error`` is a
regression (unexpected raise); ``empty`` is informational (surface
held up but didn't find data). Three-bucket semantics keep the probe
usable on a cold repo without flagging absent data as a bug.
"""
from __future__ import annotations

import json

import pytest
from click.testing import CliRunner


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


# ── Seeded canaries for test isolation ───────────────────────────────────────
#
# Three entries: one healthy, two that break a faked resolver in
# different ways (raise, return empty).


def _seeded_canaries():
    from nexus.name_canaries import NameCanary

    return [
        NameCanary(
            name="healthy-name",
            expected_surface=frozenset({"resolve_corpus"}),
            shape_note="seeded canary — resolves to a non-empty list",
        ),
        NameCanary(
            name="raises-name",
            expected_surface=frozenset({"resolve_corpus"}),
            shape_note="seeded canary — resolver raises",
        ),
        NameCanary(
            name="empty-name",
            expected_surface=frozenset({"rdr_resolve"}),
            shape_note="seeded canary — resolver reports not-found",
        ),
    ]


def _faked_corpus_runner(name, all_collections):
    if name == "healthy-name":
        return ["code__healthy-name"]
    if name == "raises-name":
        raise RuntimeError("simulated resolver crash")
    return []


def _faked_rdr_runner(name):
    # Mimic RdrResolver.resolve semantics: return str on match, raise ResolutionError on miss.
    from nexus.doc.resolvers import ResolutionError

    raise ResolutionError(f"no match for {name!r}")


def _faked_span_runner(name):
    # Syntactic regex: pass if shape looks right, empty otherwise.
    import re

    if re.fullmatch(r"chash:[0-9a-f]{64}(:\d+-\d+)?", name):
        return True
    return False


# ── ProbeResult / run_name_resolution_probe ─────────────────────────────────


class TestRunProbe:
    def test_three_bucket_outcomes(self) -> None:
        from nexus.doctor_search import run_name_resolution_probe

        results = run_name_resolution_probe(
            _seeded_canaries(),
            resolve_corpus_fn=_faked_corpus_runner,
            rdr_resolve_fn=_faked_rdr_runner,
            resolve_span_fn=_faked_span_runner,
            all_collections=[],
        )

        by_name = {r.name: r for r in results}
        assert by_name["healthy-name"].outcome == "matched"
        assert by_name["raises-name"].outcome == "error"
        assert by_name["empty-name"].outcome == "empty"

    def test_error_outcome_records_exception_repr(self) -> None:
        from nexus.doctor_search import run_name_resolution_probe

        results = run_name_resolution_probe(
            _seeded_canaries(),
            resolve_corpus_fn=_faked_corpus_runner,
            rdr_resolve_fn=_faked_rdr_runner,
            resolve_span_fn=_faked_span_runner,
            all_collections=[],
        )
        err = next(r for r in results if r.outcome == "error")
        assert err.error is not None
        assert "simulated resolver crash" in err.error

    def test_surface_name_is_recorded(self) -> None:
        from nexus.doctor_search import run_name_resolution_probe

        results = run_name_resolution_probe(
            _seeded_canaries(),
            resolve_corpus_fn=_faked_corpus_runner,
            rdr_resolve_fn=_faked_rdr_runner,
            resolve_span_fn=_faked_span_runner,
            all_collections=[],
        )
        for r in results:
            assert r.surface in {"resolve_corpus", "rdr_resolve", "resolve_span"}

    def test_one_pass_two_fail_summary_per_bead_spec(self) -> None:
        """Bead spec: '1 pass / 2 fail with surface names'."""
        from nexus.doctor_search import run_name_resolution_probe

        results = run_name_resolution_probe(
            _seeded_canaries(),
            resolve_corpus_fn=_faked_corpus_runner,
            rdr_resolve_fn=_faked_rdr_runner,
            resolve_span_fn=_faked_span_runner,
            all_collections=[],
        )
        passed = [r for r in results if r.outcome == "matched"]
        failed = [r for r in results if r.outcome != "matched"]
        assert len(passed) == 1
        assert len(failed) == 2
        for r in failed:
            assert r.surface  # surface-of-failure identified


# ── CLI wiring: nx doctor --check-search ────────────────────────────────────


class TestDoctorCheckSearchCli:
    @pytest.mark.slow
    def test_flag_invokes_probe(
        self, runner: CliRunner, tmp_path, monkeypatch,
    ) -> None:
        """``nx doctor --check-search`` runs the probe and exits 0 on no errors.

        Marked ``slow`` — the full CLI invocation through
        ``CliRunner`` loads the complete nexus import graph (the T3
        ChromaDB client + Voyage + MinerU + FastAPI + all CLI
        command modules) from cold, which costs ~2 minutes on a dev
        machine. Excluded from default addopts; run explicitly with
        ``uv run pytest -m slow``, or let the nightly
        local-service-gate-nightly.yml gate run it for you (nexus-s6dei:
        prior wording claimed "CI and pre-release runs opt in with -m
        slow" — nothing did, until this gate leg).
        """
        from nexus.cli import main

        # Point rdr_dir at a temp dir so RdrResolver has a stable root.
        monkeypatch.setattr(
            "nexus.doctor_search._default_rdr_dir",
            lambda: tmp_path,
        )
        # Stub the canary iterator to the seeded list.
        monkeypatch.setattr(
            "nexus.doctor_search._load_canaries",
            lambda: _seeded_canaries(),
        )
        monkeypatch.setattr(
            "nexus.doctor_search._corpus_runner",
            _faked_corpus_runner,
        )
        monkeypatch.setattr(
            "nexus.doctor_search._rdr_runner",
            _faked_rdr_runner,
        )

        result = runner.invoke(main, ["doctor", "--check-search"])
        # Exit code 2 because 'raises-name' in the seeded set triggers
        # a regression; 'test_nonzero_exit_on_regression' pins that.
        # Here we just pin that the probe ran and emitted its output.
        assert "matched" in result.output.lower()
        assert "healthy-name" in result.output

    def test_json_flag_emits_parseable_output(
        self, runner: CliRunner, tmp_path, monkeypatch,
    ) -> None:
        """``--check-search --json`` emits a parseable JSON payload that
        wraps both probes."""
        from nexus.cli import main

        monkeypatch.setattr(
            "nexus.doctor_search._default_rdr_dir",
            lambda: tmp_path,
        )
        monkeypatch.setattr(
            "nexus.doctor_search._load_canaries",
            lambda: _seeded_canaries(),
        )
        monkeypatch.setattr(
            "nexus.doctor_search._corpus_runner",
            _faked_corpus_runner,
        )
        monkeypatch.setattr(
            "nexus.doctor_search._rdr_runner",
            _faked_rdr_runner,
        )
        # Isolate from live T3 for probe 3b.
        monkeypatch.setattr(
            "nexus.doctor_search._list_collections",
            lambda: [],
        )

        result = runner.invoke(main, ["doctor", "--check-search", "--json"])
        # exit_code == 2 because the seeded set includes 'raises-name';
        # JSON payload is still emitted on regression.
        payload = json.loads(result.stdout)
        assert isinstance(payload, dict)
        assert "probes" in payload and len(payload["probes"]) == 2
        probe_names = [p["probe"] for p in payload["probes"]]
        assert probe_names == ["name_resolution", "retrieval_quality"]
        name_res = payload["probes"][0]
        assert isinstance(name_res["results"], list)
        assert len(name_res["results"]) == 3
        outcomes = {r["name"]: r["outcome"] for r in name_res["results"]}
        assert outcomes == {
            "healthy-name": "matched",
            "raises-name": "error",
            "empty-name": "empty",
        }

    def test_nonzero_exit_on_regression(
        self, runner: CliRunner, tmp_path, monkeypatch,
    ) -> None:
        """Any ``error`` outcome = probe failure = exit 2 (regression signal)."""
        from nexus.cli import main

        monkeypatch.setattr(
            "nexus.doctor_search._default_rdr_dir",
            lambda: tmp_path,
        )
        monkeypatch.setattr(
            "nexus.doctor_search._load_canaries",
            lambda: _seeded_canaries(),
        )
        monkeypatch.setattr(
            "nexus.doctor_search._corpus_runner",
            _faked_corpus_runner,
        )
        monkeypatch.setattr(
            "nexus.doctor_search._rdr_runner",
            _faked_rdr_runner,
        )
        # Stub retrieval probe so CLI test doesn't exercise live T3.
        monkeypatch.setattr(
            "nexus.doctor_search.run_retrieval_quality_probe",
            lambda **kwargs: [],
        )
        # Short-circuit the enumerator so we don't try to list T3.
        monkeypatch.setattr(
            "nexus.doctor_search._list_collections",
            lambda: [],
        )

        result = runner.invoke(main, ["doctor", "--check-search"])
        # At least one canary raises → regression → exit 2.
        assert result.exit_code == 2


# ── Probe 3b: retrieval quality ─────────────────────────────────────────────


def _stub_search_fn(dist_table):
    """Return a search_fn that populates diag_per_collection from *dist_table*.

    dist_table maps collection_name → (raw, dropped) tuples.
    """
    def _fn(query, collections, n_results, t3, *, diagnostics_out=None, **_):
        from nexus.search_engine import SearchDiagnostics

        per_col = {}
        total_raw = 0
        total_dropped = 0
        for col in collections:
            raw, dropped = dist_table.get(col, (0, 0))
            per_col[col] = (raw, dropped, 0.45, None)
            total_raw += raw
            total_dropped += dropped
        if diagnostics_out is not None:
            diagnostics_out.append(
                SearchDiagnostics(
                    per_collection=per_col,
                    total_dropped=total_dropped,
                    total_raw=total_raw,
                )
            )
        return []
    return _fn


def _stub_model_for(col: str) -> str:
    if col.startswith(("docs__", "knowledge__", "rdr__")):
        return "model-ctx"
    return "model-code"


class TestRetrievalQualityProbe:
    """Probe 3b — one search per registered collection, classify outcomes."""

    def test_matched_when_raw_positive_and_kept_positive(self) -> None:
        from nexus.doctor_search import run_retrieval_quality_probe

        results = run_retrieval_quality_probe(
            t3=None,
            collections=["code__clean"],
            search_fn=_stub_search_fn({"code__clean": (5, 2)}),
            model_for=_stub_model_for,
            metadata_fn=lambda col: {"embedding_model": "model-code"},
        )
        assert len(results) == 1
        assert results[0].outcome == "matched"
        assert results[0].raw_count == 5
        assert results[0].kept_count == 3

    def test_empty_when_raw_is_zero(self) -> None:
        from nexus.doctor_search import run_retrieval_quality_probe

        results = run_retrieval_quality_probe(
            t3=None,
            collections=["knowledge__empty"],
            search_fn=_stub_search_fn({"knowledge__empty": (0, 0)}),
            model_for=_stub_model_for,
            metadata_fn=lambda col: {"embedding_model": "model-ctx"},
        )
        assert results[0].outcome == "empty"
        assert results[0].raw_count == 0

    def test_threshold_drop_when_raw_positive_kept_zero(self) -> None:
        """nexus-rc45 class — raw>0 but threshold filtered everything."""
        from nexus.doctor_search import run_retrieval_quality_probe

        results = run_retrieval_quality_probe(
            t3=None,
            collections=["docs__dropped"],
            search_fn=_stub_search_fn({"docs__dropped": (4, 4)}),
            model_for=_stub_model_for,
            metadata_fn=lambda col: {"embedding_model": "model-ctx"},
        )
        assert results[0].outcome == "threshold_drop"
        assert results[0].raw_count == 4
        assert results[0].kept_count == 0

    def test_model_drift_when_metadata_disagrees(self) -> None:
        """Registered embedding_model doesn't match expected for prefix."""
        from nexus.doctor_search import run_retrieval_quality_probe

        results = run_retrieval_quality_probe(
            t3=None,
            collections=["knowledge__drifted"],
            search_fn=_stub_search_fn({"knowledge__drifted": (3, 1)}),
            model_for=_stub_model_for,
            # Expected model-ctx for knowledge__, but metadata says model-code.
            metadata_fn=lambda col: {"embedding_model": "model-code"},
        )
        # Model drift overrides the match-class — it's a regression-level signal
        # even when the query happened to return results.
        assert results[0].outcome == "model_drift"
        assert results[0].expected_model == "model-ctx"
        assert results[0].actual_model == "model-code"

    def test_error_when_search_raises(self) -> None:
        from nexus.doctor_search import run_retrieval_quality_probe

        def _raise(*a, **kw):
            raise RuntimeError("t3 connection crashed")

        results = run_retrieval_quality_probe(
            t3=None,
            collections=["code__boom"],
            search_fn=_raise,
            model_for=_stub_model_for,
            metadata_fn=lambda col: {"embedding_model": "model-code"},
        )
        assert results[0].outcome == "error"
        assert results[0].error is not None
        assert "t3 connection crashed" in results[0].error

    def test_all_five_outcome_classes_together(self) -> None:
        """One call over a mixed collection list covers every bucket."""
        from nexus.doctor_search import run_retrieval_quality_probe

        metas = {
            "code__healthy":    {"embedding_model": "model-code"},
            "docs__empty":      {"embedding_model": "model-ctx"},
            "knowledge__drop":  {"embedding_model": "model-ctx"},
            "docs__drifted":    {"embedding_model": "model-code"},  # wrong
        }

        def _search(query, collections, n_results, t3, *, diagnostics_out=None, **_):
            from nexus.search_engine import SearchDiagnostics

            if "code__boom" in collections:
                raise RuntimeError("simulated")
            per_col = {}
            for col in collections:
                if col == "code__healthy":
                    per_col[col] = (5, 2, 0.45, None)
                elif col == "docs__empty":
                    per_col[col] = (0, 0, 0.45, None)
                elif col == "knowledge__drop":
                    per_col[col] = (4, 4, 0.45, None)
                elif col == "docs__drifted":
                    per_col[col] = (3, 1, 0.45, None)
                else:
                    per_col[col] = (0, 0, 0.45, None)
            if diagnostics_out is not None:
                diagnostics_out.append(
                    SearchDiagnostics(
                        per_collection=per_col,
                        total_dropped=0,
                        total_raw=0,
                    )
                )
            return []

        def _outer_search(query, collections, n_results, t3, *, diagnostics_out=None, **kw):
            if "code__boom" in collections:
                raise RuntimeError("simulated")
            return _search(
                query, collections, n_results, t3,
                diagnostics_out=diagnostics_out, **kw,
            )

        results = run_retrieval_quality_probe(
            t3=None,
            collections=[
                "code__healthy",
                "docs__empty",
                "knowledge__drop",
                "docs__drifted",
                "code__boom",
            ],
            search_fn=_outer_search,
            model_for=_stub_model_for,
            metadata_fn=lambda col: metas.get(col, {}),
        )
        by_name = {r.name: r.outcome for r in results}
        assert by_name["code__healthy"] == "matched"
        assert by_name["docs__empty"] == "empty"
        assert by_name["knowledge__drop"] == "threshold_drop"
        assert by_name["docs__drifted"] == "model_drift"
        assert by_name["code__boom"] == "error"


# nexus-dhvzx: the 7.64.1 shakeout ran --check-search on the live tenant; the
# canned query "example test probe" was irrelevant to most collections, the
# per-collection threshold correctly dropped it, 58 of 111 collections read
# threshold_drop, and the check exited 2 on every real tenant. The fix queries
# with one of the collection's own chunks and judges only that chunk's OTHER
# neighbours: the chunk finds itself at distance ~0, and a first version that
# counted the self-hit could never fire on RDR-087's incident, a healthy
# collection whose natural floor sits above its threshold (critique
# nexus/critique-6c06ab9c4-check-search-probe-self-text-tautology). The
# incident test below pins that the probe still fires.

COLS = ["code__a__voyage-code-3__v1", "rdr__a__voyage-context-3__v1"]


class _ChunkT3:
    def __init__(self, n_chunks: int = 3):
        self.n_chunks = n_chunks

    def get_or_create_collection(self, col):
        n = self.n_chunks

        class _Coll:
            def get(self, include=None, limit=None):
                k = min(n, limit or n)
                return {"ids": [f"{col}#{i}" for i in range(k)],
                        "documents": [f"text {i} of {col}" for i in range(k)]}
        return _Coll()


def _neighbour_search(neighbour_distance, self_distance=0.0, include_self=True):
    """Unthresholded search: the probe chunk's own row (at self_distance,
    or absent) plus four neighbours at neighbour_distance(col)."""
    from nexus.search_engine import SearchResult

    def search(query, cols, n_results, t3, diagnostics_out=None, threshold_override=None):
        col = cols[0]
        sid = f"{col}#{query.split()[1]}"
        rows = [SearchResult(id=f"{col}-n{j}", content="", distance=neighbour_distance(col) + j * 0.01,
                             collection=col, metadata={}) for j in range(4)]
        if include_self:
            rows.insert(0, SearchResult(id=sid, content="", distance=self_distance,
                                        collection=col, metadata={}))
        return rows
    return search


def _probe(t3, search, **kw):
    from nexus.doctor_search import run_retrieval_quality_probe

    return {r.name: r for r in run_retrieval_quality_probe(
        t3=t3, collections=COLS, search_fn=search,
        model_for=lambda c: "", metadata_fn=lambda c: {},
        threshold_for=lambda c: 0.5, **kw)}


def _default(t3):
    from nexus.doctor_search import _default_probe_for
    return {"probe_for": lambda c: _default_probe_for(t3, c)}


def test_the_canned_query_reads_threshold_drop_on_healthy_collections() -> None:
    """The defect, reproduced: a canned query far from every collection."""
    from nexus.search_engine import SearchDiagnostics

    def canned_search(query, cols, n_results, t3, diagnostics_out=None):
        diagnostics_out.append(SearchDiagnostics(
            per_collection={cols[0]: (4, 4, 0.5, 0.8)}, total_dropped=4, total_raw=4,
            failed_collections={}))
        return []

    rows = _probe(_ChunkT3(), canned_search)
    assert {r.outcome for r in rows.values()} == {"threshold_drop"}


def test_a_collection_with_close_neighbours_matches() -> None:
    t3 = _ChunkT3()
    rows = _probe(t3, _neighbour_search(lambda c: 0.3), **_default(t3))
    assert {r.outcome for r in rows.values()} == {"matched"}
    assert {r.probe_chunks for r in rows.values()} == {3}


def test_a_collection_whose_floor_sits_above_its_threshold_reads_drop() -> None:
    """RDR-087's incident: related text exists, but every real neighbour
    lands past the threshold."""
    t3 = _ChunkT3()
    high = COLS[1]
    rows = _probe(t3, _neighbour_search(lambda c: 0.7 if c == high else 0.3), **_default(t3))
    assert rows[COLS[0]].outcome == "matched"
    assert rows[high].outcome == "threshold_drop"
    assert rows[high].nearest_distance == 0.7


def test_a_self_hit_that_lands_far_is_excluded_by_id_not_assumed() -> None:
    """Critique of 29fab0df4: a re-embedded snippet of a context-embedded
    chunk need not sit near its own stored vector. The probe must neither
    count that far self-row as a neighbour nor subtract a self-hit that is
    not there."""
    t3 = _ChunkT3()
    far_self = _probe(t3, _neighbour_search(lambda c: 0.3, self_distance=0.9), **_default(t3))
    no_self = _probe(t3, _neighbour_search(lambda c: 0.3, include_self=False), **_default(t3))
    assert {r.outcome for r in far_self.values()} == {"matched"}
    assert {r.outcome for r in no_self.values()} == {"matched"}
    high_floor_no_self = _probe(t3, _neighbour_search(lambda c: 0.7, include_self=False), **_default(t3))
    assert {r.outcome for r in high_floor_no_self.values()} == {"threshold_drop"}


def test_the_verdict_is_the_median_over_the_samples() -> None:
    """One borderline chunk cannot decide the verdict alone."""
    t3 = _ChunkT3()
    from nexus.search_engine import SearchResult

    def search(query, cols, n_results, t3_, diagnostics_out=None, threshold_override=None):
        col = cols[0]
        i = int(query.split()[1])
        d = 0.6 if i == 0 else 0.3  # sample 0 alone sits past the threshold
        return [SearchResult(id=f"{col}-n", content="", distance=d, collection=col, metadata={})]

    rows = _probe(t3, search, **_default(t3))
    assert {r.outcome for r in rows.values()} == {"matched"}


def test_a_collection_with_no_readable_chunk_falls_back_to_the_canned_query() -> None:
    from nexus.search_engine import SearchDiagnostics

    seen: list[str] = []

    def search(query, cols, n_results, t3, diagnostics_out=None, **kw):
        seen.append(query)
        diagnostics_out.append(SearchDiagnostics(
            per_collection={cols[0]: (1, 0, 0.5, None)}, total_dropped=0, total_raw=1,
            failed_collections={}))
        return []

    t3 = _ChunkT3(n_chunks=0)
    _probe(t3, search, **_default(t3))
    assert set(seen) == {"example test probe"}


def test_a_threshold_drop_row_names_its_evidence() -> None:
    """Sam's ruling (2026-09-28): a threshold_drop warns, and its row says
    what a reader needs to judge it."""
    from nexus.doctor_search import ProbeResult, format_combined_human

    row = ProbeResult(name="docs__1-45__voyage-context-3__v1", surface="retrieval_quality",
                      outcome="threshold_drop", raw_count=18, kept_count=0,
                      nearest_distance=0.6954, threshold=0.65, probe_chunks=1)
    text = format_combined_human([], [row])
    line = next(ln for ln in text.splitlines() if "docs__1-45" in ln)
    assert line.lstrip().startswith("[!]")
    assert "d=0.695 > threshold 0.650" in line
    assert "rests on 1 probe chunk(s) and 18 neighbour(s)" in line
    assert "threshold_drop (warning)" in text


def test_check_search_exits_zero_when_the_only_finding_is_a_threshold_drop(monkeypatch) -> None:
    import nexus.doctor_search as ds

    drop = ds.ProbeResult(name="c", surface="retrieval_quality", outcome="threshold_drop",
                          raw_count=4, kept_count=0, nearest_distance=0.7,
                          threshold=0.65, probe_chunks=1)
    monkeypatch.setattr(ds, "_load_canaries", lambda: [])
    monkeypatch.setattr(ds, "run_name_resolution_probe", lambda *a, **k: [])
    monkeypatch.setattr(ds, "_list_collections", lambda: ["c"])
    monkeypatch.setattr(ds, "_make_t3", lambda: object())
    monkeypatch.setattr(ds, "run_retrieval_quality_probe", lambda **k: [drop])
    ds.run_check_search(json_out=False)  # returns: no SystemExit(2)

    drift = ds.ProbeResult(name="c", surface="retrieval_quality", outcome="model_drift")
    monkeypatch.setattr(ds, "run_retrieval_quality_probe", lambda **k: [drift])
    with pytest.raises(SystemExit) as exc:
        ds.run_check_search(json_out=False)
    assert exc.value.code == 2


def test_a_near_duplicate_neighbour_does_not_make_a_high_floor_collection_healthy() -> None:
    """Review of 29fab0df4: a second chunk with nearly the same text sits at
    distance ~0 and says nothing about the floor; counting it read a real
    threshold_drop as matched."""
    from nexus.search_engine import SearchResult

    def search(query, cols, n_results, t3, diagnostics_out=None, threshold_override=None):
        col = cols[0]
        return [SearchResult(id=f"{col}-dup", content="", distance=0.001, collection=col, metadata={}),
                SearchResult(id=f"{col}-far", content="", distance=0.7, collection=col, metadata={})]

    t3 = _ChunkT3()
    rows = _probe(t3, search, **_default(t3))
    assert {r.outcome for r in rows.values()} == {"threshold_drop"}


def test_the_neighbour_count_is_the_window_judged_not_the_overfetched_pool() -> None:
    """Critique of 634cc2b66: search_cross_corpus returns its whole
    over-fetched pool, so the evidence line counted up to 4x more
    neighbours than the probe's own depth."""
    from nexus.search_engine import SearchResult

    def wide_search(query, cols, n_results, t3, diagnostics_out=None, threshold_override=None):
        col = cols[0]
        return [SearchResult(id=f"{col}-n{j}", content="", distance=0.3 + j * 0.01,
                             collection=col, metadata={}) for j in range(24)]

    t3 = _ChunkT3(n_chunks=1)
    rows = _probe(t3, wide_search, **_default(t3))
    assert {r.raw_count for r in rows.values()} == {5}


def test_a_close_self_hit_is_excluded_by_id_when_real_neighbours_are_far() -> None:
    """Review of 634cc2b66: the far-self test cannot tell whether the id
    exclusion exists (a far self-row is never the minimum). The shape the
    exclusion guards: the self-row lands close (past the duplicate filter,
    inside the threshold) while every real neighbour is past the threshold.
    Counting the self-row would read the collection healthy."""
    t3 = _ChunkT3()
    rows = _probe(t3, _neighbour_search(lambda c: 0.7, self_distance=0.1), **_default(t3))
    assert {r.outcome for r in rows.values()} == {"threshold_drop"}
