# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-pyzk7: tier_writes + nx_answer_runs persist via the telemetry STORE
(``HttpTelemetryStore`` — the only telemetry store since nexus-i711w Stage 2
sub-stage A deleted the SQLite ``Telemetry``), never by reaching for a raw
``.conn`` the HTTP store lacks (which silently dropped every row)."""
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


def test_http_telemetry_has_record_methods():
    # nexus-i711w Stage 2 sub-stage A2: the SQLite Telemetry twin died, so the
    # backend-parity form collapsed to pinning the record API on the sole
    # surviving store — the consumer contract _nx_answer_record_run relies on.
    from nexus.db.t2.http_telemetry_store import HttpTelemetryStore
    assert callable(getattr(HttpTelemetryStore, "record_tier_write", None))
    assert callable(getattr(HttpTelemetryStore, "record_nx_answer_run", None))


# test_canonical_telemetry_record_persists DELETED (nexus-i711w Stage 2
# sub-stage A2): its subject was the SQLite Telemetry store's own round-trip;
# that store died this commit. Persistence of tier_writes / nx_answer_runs is
# covered by test_http_telemetry_record_posts_to_endpoints below plus the
# engine-side TelemetryRepositoryTest.java.


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


# ── nexus-9613q.3: hook_failures persist via the telemetry STORE ─────────────
# hook_registry._record_*_hook_failure reached t2.taxonomy.conn inside
# try/except:_log.debug, silently dropping every hook_failures row in service
# mode (the same silent-loss class as tier_writes). Route through the
# telemetry store (the Java /v1/telemetry/hook_failures/record endpoint exists)
# so consumers are backend-blind, and make a failed persist VISIBLE.


def test_http_telemetry_has_record_hook_failure():
    # nexus-i711w Stage 2 sub-stage A2: SQLite twin died; pin the sole
    # surviving store's record API (the hook_registry consumer contract).
    from nexus.db.t2.http_telemetry_store import HttpTelemetryStore
    assert callable(getattr(HttpTelemetryStore, "record_hook_failure", None))


# test_canonical_hook_failure_persists_all_chains DELETED (nexus-i711w Stage 2
# sub-stage A2): subject was the SQLite store's own hook_failures round-trip.
# Chain routing (single/batch/document) stays pinned by
# test_hook_failure_routes_through_telemetry_store below; the wire shape by
# test_http_hook_failure_posts_to_record_endpoint; persistence engine-side in
# TelemetryRepositoryTest.java (hook_failures fidelity tests).


def test_http_hook_failure_posts_to_record_endpoint():
    from nexus.db.t2.http_telemetry_store import HttpTelemetryStore
    store = HttpTelemetryStore.__new__(HttpTelemetryStore)
    posts = []
    store._post = lambda path, body: posts.append((path, body)) or {}
    store.record_hook_failure(doc_id="d", collection="c", hook_name="h",
                             error="e", chain="batch",
                             batch_doc_ids='["d"]', is_batch=True)
    assert posts and posts[0][0] == "/v1/telemetry/hook_failures/record"
    body = posts[0][1]
    assert body["hook_name"] == "h" and body["chain"] == "batch"
    assert body["is_batch"] is True and body["batch_doc_ids"] == '["d"]'


def test_hook_failure_store_failure_warns_once_and_does_not_raise(monkeypatch):
    import nexus.hook_registry as hr
    from contextlib import contextmanager

    hr._hook_failure_drop_warned.discard(("single", "h"))

    class _BoomTelemetry:
        def record_hook_failure(self, **kwargs):
            raise RuntimeError("service 503")

    class _FakeT2:
        telemetry = _BoomTelemetry()

    @contextmanager
    def _fake_t2_ctx():
        yield _FakeT2()

    monkeypatch.setattr("nexus.mcp_infra.t2_ctx", _fake_t2_ctx)
    hr._record_hook_failure(doc_id="d", collection="c", hook_name="h", error="e")
    assert ("single", "h") in hr._hook_failure_drop_warned


def test_hook_failure_warn_once_is_per_hook_not_per_chain(monkeypatch):
    # nexus-9613q review M1: a transient failure of one hook must NOT silence
    # every other hook of the same chain. The warn-once key is (chain, hook).
    import nexus.hook_registry as hr
    from contextlib import contextmanager

    hr._hook_failure_drop_warned.discard(("single", "hookA"))
    hr._hook_failure_drop_warned.discard(("single", "hookB"))

    class _BoomTelemetry:
        def record_hook_failure(self, **kwargs):
            raise RuntimeError("service 503")

    class _FakeT2:
        telemetry = _BoomTelemetry()

    @contextmanager
    def _fake_t2_ctx():
        yield _FakeT2()

    monkeypatch.setattr("nexus.mcp_infra.t2_ctx", _fake_t2_ctx)
    hr._record_hook_failure(doc_id="d", collection="c", hook_name="hookA", error="e")
    hr._record_hook_failure(doc_id="d", collection="c", hook_name="hookB", error="e")
    # Both distinct hooks recorded a warning key — neither was masked by the other.
    assert ("single", "hookA") in hr._hook_failure_drop_warned
    assert ("single", "hookB") in hr._hook_failure_drop_warned


def test_hook_failure_routes_through_telemetry_store(monkeypatch):
    import nexus.hook_registry as hr
    from contextlib import contextmanager
    from unittest.mock import MagicMock

    tel = MagicMock()

    class _FakeT2:
        telemetry = tel

    @contextmanager
    def _fake_t2_ctx():
        yield _FakeT2()

    monkeypatch.setattr("nexus.mcp_infra.t2_ctx", _fake_t2_ctx)
    hr._record_hook_failure(doc_id="d", collection="c", hook_name="h", error="e")
    hr._record_batch_hook_failure(doc_ids=["a", "b"], collection="c",
                                  hook_name="h", error="e")
    hr._record_document_hook_failure(source_path="/p", collection="c",
                                     hook_name="h", error="e")
    chains = [c.kwargs["chain"] for c in tel.record_hook_failure.call_args_list]
    assert chains == ["single", "batch", "document"]
    batch_call = tel.record_hook_failure.call_args_list[1].kwargs
    assert batch_call["is_batch"] is True
    assert batch_call["batch_doc_ids"] == '["a", "b"]'
