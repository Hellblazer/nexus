# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deterministic tests for the RDR-094 Spike B race-probe harness.

The harness itself spawns real subprocesses (nx-mcp + a Python child
that constructs T1Database), so end-to-end exercise is out of scope
for unit tests -- the 40-cycle evidence pass is run by Hal on demand
and persisted to T2 ``nexus_rdr/094-spike-b-subagent-race``. What we
*can* pin here are the pure-python pieces: outcome classification +
aggregation + module structure (nexus-zsqf).
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SPIKE_PATH = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "spikes"
    / "spike_rdr094_b_subagent_race.py"
)


@pytest.fixture(scope="module")
def harness():
    spec = importlib.util.spec_from_file_location("spike_rdr094_b", _SPIKE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["spike_rdr094_b"] = mod
    spec.loader.exec_module(mod)
    return mod


# ── _classify_outcome ───────────────────────────────────────────────────────


class TestClassifyOutcome:
    """Three diagnostic verdicts encode CA-2's pass/fail rule:

    * ``connected_to_parent`` -- subagent's T1Database._client is an
      HttpClient pointing at the parent's chroma. CA-2 holds.
    * ``ephemeral_downgrade`` -- _client is an EphemeralClient. The
      silent-downgrade signature; CA-2 fails for this cycle.
    * ``setup_failed`` -- harness couldn't get nx-mcp + chroma up.
      Excluded from the pass/fail tally.
    """

    def test_http_client_classifies_as_connected(self, harness):
        stdout = json.dumps({
            "client_class": "HttpClient",
            "session_id": "abc",
        }) + "\n"
        assert harness._classify_outcome(stdout, setup_failed=False) == "connected_to_parent"

    def test_ephemeral_client_classifies_as_downgrade(self, harness):
        stdout = json.dumps({
            "client_class": "EphemeralClient",
            "session_id": "abc",
        }) + "\n"
        assert harness._classify_outcome(stdout, setup_failed=False) == "ephemeral_downgrade"

    def test_setup_failed_short_circuits(self, harness):
        """Harness-detected setup failure outranks any client output."""
        stdout = json.dumps({"client_class": "HttpClient"}) + "\n"
        assert harness._classify_outcome(stdout, setup_failed=True) == "setup_failed"

    def test_blank_stdout_is_unknown(self, harness):
        assert harness._classify_outcome("", setup_failed=False) == "unknown"

    def test_garbage_stdout_is_unknown(self, harness):
        assert harness._classify_outcome("not-json\n", setup_failed=False) == "unknown"

    def test_last_json_line_wins(self, harness):
        """Probe may print warnings before the final JSON line."""
        stdout = (
            "warning: something happened\n"
            "more text\n"
            + json.dumps({"client_class": "HttpClient"})
            + "\n"
        )
        assert harness._classify_outcome(stdout, setup_failed=False) == "connected_to_parent"

    def test_persistent_client_class_classifies_as_connected(self, harness):
        """Some chromadb versions name the http client variant slightly
        differently; substring 'HttpClient' is the load-bearing token."""
        stdout = json.dumps({
            "client_class": "FastAPIHttpClient",
            "session_id": "abc",
        }) + "\n"
        assert harness._classify_outcome(stdout, setup_failed=False) == "connected_to_parent"


# ── _aggregate ──────────────────────────────────────────────────────────────


def _make_record(harness, timing_ms: int, run_id: int, outcome: str):
    return harness.RunRecord(
        timing_variant_ms=timing_ms,
        run_id=run_id,
        outcome=outcome,
        elapsed_ms=10.0,
        setup_failed=outcome == "setup_failed",
        error=None,
    )


class TestAggregate:

    def test_zero_downgrade_verifies_ca2(self, harness):
        records = [
            _make_record(harness, t, i, "connected_to_parent")
            for t in (0, 50)
            for i in range(3)
        ]
        summary = harness._aggregate(records)
        assert summary["totals"]["ephemeral_downgrade"] == 0
        assert summary["totals"]["connected_to_parent"] == 6
        assert summary["interpretation"] == "ca2_verified_race_not_reproducible"

    def test_any_downgrade_fails_ca2(self, harness):
        records = [
            _make_record(harness, 0, 0, "connected_to_parent"),
            _make_record(harness, 0, 1, "ephemeral_downgrade"),  # the failure
            _make_record(harness, 50, 0, "connected_to_parent"),
        ]
        summary = harness._aggregate(records)
        assert summary["totals"]["ephemeral_downgrade"] == 1
        assert summary["interpretation"] == "ca2_failed_race_reproducible"

    def test_all_setup_failed_is_inconclusive(self, harness):
        records = [_make_record(harness, 0, i, "setup_failed") for i in range(3)]
        summary = harness._aggregate(records)
        assert summary["totals"]["setup_failed"] == 3
        # Without any usable cycles, we cannot conclude either way.
        assert summary["interpretation"] == "inconclusive"

    def test_per_timing_breakdown_includes_all_outcomes(self, harness):
        records = [
            _make_record(harness, 0, 0, "connected_to_parent"),
            _make_record(harness, 0, 1, "ephemeral_downgrade"),
            _make_record(harness, 0, 2, "setup_failed"),
            _make_record(harness, 0, 3, "unknown"),
        ]
        summary = harness._aggregate(records)
        bucket = summary["by_timing"]["0"]
        assert bucket["runs"] == 4
        assert bucket["connected_to_parent"] == 1
        assert bucket["ephemeral_downgrade"] == 1
        assert bucket["setup_failed"] == 1
        assert bucket["unknown"] == 1


# ── Module structure ────────────────────────────────────────────────────────


class TestSpikeStructure:
    """Verify the harness exposes the surfaces other harnesses (and the
    runtime evidence pass on Hal's machine) rely on."""

    def test_default_timings_match_rdr_protocol(self, harness):
        """RDR §CA-2 specifies 0/5/50/200 ms dispatch delays."""
        assert harness.DEFAULT_TIMINGS_MS == (0, 5, 50, 200)

    def test_run_record_has_required_fields(self, harness):
        rec = _make_record(harness, 0, 0, "connected_to_parent")
        # asdict round-trip ensures the dataclass schema is stable.
        from dataclasses import asdict
        d = asdict(rec)
        for key in (
            "timing_variant_ms", "run_id", "outcome",
            "elapsed_ms", "setup_failed", "error",
        ):
            assert key in d, f"RunRecord missing field {key!r}"

    def test_subagent_probe_imports_t1database(self, harness):
        """The probe code injected into the child must end up calling
        T1Database; otherwise the spike measures nothing."""
        assert "T1Database" in harness._SUBAGENT_PROBE
        assert "T1Database()" in harness._SUBAGENT_PROBE
        assert "client_class" in harness._SUBAGENT_PROBE

    def test_run_cycle_function_exists(self, harness):
        assert callable(harness._run_cycle)

    def test_main_dispatcher_callable(self, harness):
        assert callable(harness.main)
