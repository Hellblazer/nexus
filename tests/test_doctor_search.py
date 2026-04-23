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
        machine. Deselected from the default pytest run; nightly
        CI and pre-release runs opt in with ``-m slow``.
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
        payload = json.loads(result.output)
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
        return "voyage-context-3"
    return "voyage-code-3"


class TestRetrievalQualityProbe:
    """Probe 3b — one search per registered collection, classify outcomes."""

    def test_matched_when_raw_positive_and_kept_positive(self) -> None:
        from nexus.doctor_search import run_retrieval_quality_probe

        results = run_retrieval_quality_probe(
            t3=None,
            collections=["code__clean"],
            search_fn=_stub_search_fn({"code__clean": (5, 2)}),
            model_for=_stub_model_for,
            metadata_fn=lambda col: {"embedding_model": "voyage-code-3"},
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
            metadata_fn=lambda col: {"embedding_model": "voyage-context-3"},
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
            metadata_fn=lambda col: {"embedding_model": "voyage-context-3"},
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
            # Expected voyage-context-3 for knowledge__, but metadata says voyage-code-3.
            metadata_fn=lambda col: {"embedding_model": "voyage-code-3"},
        )
        # Model drift overrides the match-class — it's a regression-level signal
        # even when the query happened to return results.
        assert results[0].outcome == "model_drift"
        assert results[0].expected_model == "voyage-context-3"
        assert results[0].actual_model == "voyage-code-3"

    def test_error_when_search_raises(self) -> None:
        from nexus.doctor_search import run_retrieval_quality_probe

        def _raise(*a, **kw):
            raise RuntimeError("t3 connection crashed")

        results = run_retrieval_quality_probe(
            t3=None,
            collections=["code__boom"],
            search_fn=_raise,
            model_for=_stub_model_for,
            metadata_fn=lambda col: {"embedding_model": "voyage-code-3"},
        )
        assert results[0].outcome == "error"
        assert results[0].error is not None
        assert "t3 connection crashed" in results[0].error

    def test_all_five_outcome_classes_together(self) -> None:
        """One call over a mixed collection list covers every bucket."""
        from nexus.doctor_search import run_retrieval_quality_probe

        metas = {
            "code__healthy":    {"embedding_model": "voyage-code-3"},
            "docs__empty":      {"embedding_model": "voyage-context-3"},
            "knowledge__drop":  {"embedding_model": "voyage-context-3"},
            "docs__drifted":    {"embedding_model": "voyage-code-3"},  # wrong
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
