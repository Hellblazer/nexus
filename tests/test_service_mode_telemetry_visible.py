# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-pyzk7: direct-SQLite telemetry writers must degrade VISIBLY (not silently
drop rows) when T2 is service-backed (no raw .conn)."""
from __future__ import annotations

import nexus.mcp.core as core


def test_nx_answer_record_run_skips_visibly_when_no_conn(monkeypatch, caplog):
    core._telemetry_unavailable_warned.clear()
    warned = {}
    monkeypatch.setattr(core, "_warn_telemetry_unavailable", lambda t: warned.setdefault(t, True))
    # conn=None (service mode) -> must return without touching a DB.
    core._nx_answer_record_run(
        None, question="q", plan_id=None, matched_confidence=None,
        step_count=1, final_text="t", cost_usd=0.0, duration_ms=1, trace=True,
    )
    assert warned.get("nx_answer_runs") is True


def test_warn_telemetry_unavailable_fires_once(monkeypatch):
    core._telemetry_unavailable_warned.clear()
    calls = []
    import structlog
    monkeypatch.setattr(structlog, "get_logger", lambda *a, **k: type("L", (), {"warning": lambda self, *a, **k: calls.append(1)})())
    core._warn_telemetry_unavailable("tier_writes")
    core._warn_telemetry_unavailable("tier_writes")
    assert len(calls) == 1  # once per table per process


def test_telemetry_conn_none_for_service_store():
    class _SvcTel:  # no .conn, like HttpTelemetryStore
        pass
    class _DB:
        telemetry = _SvcTel()
    assert core._telemetry_conn(_DB()) is None
