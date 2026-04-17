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
    def test_flag_invokes_probe(
        self, runner: CliRunner, tmp_path, monkeypatch,
    ) -> None:
        """``nx doctor --check-search`` runs the probe and exits 0 on no errors."""
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
        """``--check-search --json`` emits a parseable JSON array."""
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

        result = runner.invoke(main, ["doctor", "--check-search", "--json"])
        # exit_code == 2 because the seeded set includes 'raises-name';
        # JSON payload is still emitted on regression.
        payload = json.loads(result.output)
        assert isinstance(payload, dict)
        assert payload["probe"] == "name_resolution"
        assert isinstance(payload["results"], list)
        assert len(payload["results"]) == 3
        outcomes = {r["name"]: r["outcome"] for r in payload["results"]}
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

        result = runner.invoke(main, ["doctor", "--check-search"])
        # At least one canary raises → regression → exit 2.
        assert result.exit_code == 2
