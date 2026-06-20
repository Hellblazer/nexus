# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-pyzk7: tier_writes + nx_answer_runs persist via the telemetry STORE
(SQLite raw OR the service endpoint), never by reaching for a raw .conn the
service-backed store lacks (which silently dropped every row)."""
from __future__ import annotations

from unittest.mock import MagicMock

import nexus.mcp.core as core


def test_nx_answer_record_run_routes_through_store_with_redaction():
    tel = MagicMock()
    core._nx_answer_record_run(
        tel, question="secret q", plan_id=3, matched_confidence=0.9,
        step_count=2, final_text="secret a", cost_usd=0.1, duration_ms=5,
        trace=False,  # redact
    )
    tel.record_nx_answer_run.assert_called_once()
    kw = tel.record_nx_answer_run.call_args.kwargs
    assert kw["question"] == "[redacted]" and kw["final_text"] == "[redacted]"
    assert kw["plan_id"] == 3 and kw["step_count"] == 2


def test_nx_answer_record_run_trace_true_keeps_text():
    tel = MagicMock()
    core._nx_answer_record_run(
        tel, question="q", plan_id=None, matched_confidence=None,
        step_count=1, final_text="a", cost_usd=0.0, duration_ms=1, trace=True,
    )
    kw = tel.record_nx_answer_run.call_args.kwargs
    assert kw["question"] == "q" and kw["final_text"] == "a"


def test_canonical_and_http_telemetry_have_record_methods():
    # Both backends expose the same record API so the consumer is backend-blind.
    from nexus.db.t2.telemetry import Telemetry
    from nexus.db.t2.http_telemetry_store import HttpTelemetryStore
    for cls in (Telemetry, HttpTelemetryStore):
        assert callable(getattr(cls, "record_tier_write", None)), cls
        assert callable(getattr(cls, "record_nx_answer_run", None)), cls


def test_canonical_telemetry_record_persists(tmp_path):
    # Real SQLite round-trip through the canonical store method.
    from nexus.db.t2.telemetry import Telemetry
    t = Telemetry(tmp_path / "tel.db")
    t.record_tier_write(session_id="s", ts="2026-01-01T00:00:00+00:00",
                        tool="x", tier="T2", project="p")
    t.record_nx_answer_run(question="q", plan_id=None, matched_confidence=None,
                           step_count=1, final_text="a", cost_usd=0.0, duration_ms=1)
    n_tw = t.conn.execute("SELECT count(*) FROM tier_writes").fetchone()[0]
    n_ar = t.conn.execute("SELECT count(*) FROM nx_answer_runs").fetchone()[0]
    t.close()
    assert n_tw == 1 and n_ar == 1


def test_record_run_store_failure_warns_once_and_does_not_raise():
    # nexus-pyzk7: a failing persist (e.g. service 5xx) must be VISIBLE (warn
    # once) and never propagate — telemetry is best-effort but not silent.
    core._telemetry_drop_warned.discard("nx_answer_runs")
    tel = MagicMock()
    tel.record_nx_answer_run.side_effect = RuntimeError("service 503")
    # Must not raise.
    core._nx_answer_record_run(
        tel, question="q", plan_id=None, matched_confidence=None,
        step_count=1, final_text="a", cost_usd=0.0, duration_ms=1, trace=True,
    )
    assert "nx_answer_runs" in core._telemetry_drop_warned


def test_tier_write_store_failure_warns_once_and_does_not_raise(monkeypatch):
    core._telemetry_drop_warned.discard("tier_writes")
    from contextlib import contextmanager

    class _BoomTelemetry:
        def record_tier_write(self, **kwargs):
            raise RuntimeError("service 503")

    class _FakeT2:
        telemetry = _BoomTelemetry()

    @contextmanager
    def _fake_t2_ctx():
        yield _FakeT2()

    monkeypatch.setattr("nexus.mcp_infra.t2_ctx", _fake_t2_ctx)
    # Must not raise.
    core._record_tier_write(tool="t", tier="T1")
    assert "tier_writes" in core._telemetry_drop_warned


def test_http_telemetry_record_posts_to_endpoints():
    from nexus.db.t2.http_telemetry_store import HttpTelemetryStore
    store = HttpTelemetryStore.__new__(HttpTelemetryStore)  # bypass network init
    posts = []
    store._post = lambda path, body: posts.append((path, body)) or {}
    store.record_tier_write(session_id="s", ts="t", tool="x", tier="T2")
    store.record_nx_answer_run(question="q", plan_id=None, matched_confidence=None,
                              step_count=1, final_text="a", cost_usd=0.0, duration_ms=1)
    paths = [p for p, _ in posts]
    assert "/v1/telemetry/tier_writes/record" in paths
    assert "/v1/telemetry/nx_answer_runs/record" in paths
