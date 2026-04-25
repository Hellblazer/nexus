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
    """Four diagnostic verdicts encode CA-2's pass/fail rule:

    * ``connected_to_parent`` -- subagent's T1Database resolved a
      session record and connected via HttpClient. CA-2 holds.
    * ``ephemeral_downgrade`` -- T1Database fell through to
      chromadb.EphemeralClient (the silent-downgrade signature).
    * ``setup_failed`` -- harness couldn't get nx-mcp + chroma up.
    * ``unknown`` -- probe ran but gave no parseable signal.

    chromadb.HttpClient and chromadb.EphemeralClient are factory
    functions that BOTH return ``chromadb.api.client.Client``, so
    ``type(client).__name__`` cannot distinguish them. The probe
    inspects the warnings stream and emits ``outcome`` directly;
    classifier reads that field with a warnings-fallback for
    robustness.
    """

    def test_explicit_outcome_connected(self, harness):
        stdout = json.dumps({"outcome": "connected_to_parent"}) + "\n"
        assert harness._classify_outcome(stdout, setup_failed=False) == "connected_to_parent"

    def test_explicit_outcome_downgrade(self, harness):
        stdout = json.dumps({"outcome": "ephemeral_downgrade"}) + "\n"
        assert harness._classify_outcome(stdout, setup_failed=False) == "ephemeral_downgrade"

    def test_warnings_fallback_classifies_downgrade(self, harness):
        """Old harness output without explicit outcome: warnings inspection."""
        stdout = json.dumps({
            "warnings": [
                "No T1 server found; falling back to local EphemeralClient. "
                "Cross-agent scratch sharing is unavailable for this session.",
            ],
        }) + "\n"
        assert harness._classify_outcome(stdout, setup_failed=False) == "ephemeral_downgrade"

    def test_no_outcome_no_warnings_is_unknown(self, harness):
        """Probe ran but produced neither explicit outcome nor downgrade
        warning. Conservative: return unknown, do NOT silently classify
        as connected_to_parent (otherwise a probe with broken warning
        capture would falsely confirm CA-2)."""
        stdout = json.dumps({"client_class": "Client"}) + "\n"
        assert harness._classify_outcome(stdout, setup_failed=False) == "unknown"

    def test_setup_failed_short_circuits(self, harness):
        """Harness-detected setup failure outranks any probe output."""
        stdout = json.dumps({"outcome": "connected_to_parent"}) + "\n"
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
            + json.dumps({"outcome": "connected_to_parent"})
            + "\n"
        )
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

    def test_all_unknown_is_inconclusive_not_verified(self, harness):
        """Regression sentinel for the bug found on the first 40-cycle
        run: every cycle classified as 'unknown' (probe never produced
        a parseable signal) was wrongly interpreted as
        ca2_verified_race_not_reproducible. Verification must require
        positive evidence (>=1 connected_to_parent), not merely zero
        downgrades.
        """
        records = [_make_record(harness, t, i, "unknown")
                   for t in (0, 5, 50, 200)
                   for i in range(10)]
        summary = harness._aggregate(records)
        assert summary["totals"]["ephemeral_downgrade"] == 0
        assert summary["totals"]["connected_to_parent"] == 0
        assert summary["interpretation"] == "inconclusive", (
            "All-unknown must NOT verify CA-2 -- positive evidence required"
        )

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

    def test_subagent_probe_emits_explicit_outcome(self, harness):
        """Probe must emit an explicit outcome field (not rely on
        type(client).__name__ which collapses HttpClient and
        EphemeralClient to 'Client')."""
        assert '"outcome"' in harness._SUBAGENT_PROBE
        assert "ephemeral_downgrade" in harness._SUBAGENT_PROBE
        assert "connected_to_parent" in harness._SUBAGENT_PROBE
        # The decision predicate must inspect the warnings stream.
        assert "falling back to local EphemeralClient" in harness._SUBAGENT_PROBE

    def test_run_cycle_function_exists(self, harness):
        assert callable(harness._run_cycle)

    def test_main_dispatcher_callable(self, harness):
        assert callable(harness.main)
