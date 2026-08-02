# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-5xn3k.5 RUNFENCE C3 (client, AC6): re-route update-metadata misses.

Design memo (T2 nexus "5xn3k-design-2026-08-02") §3.6 — the defect:
``_upsert_skip_reembed`` (doc_indexer.py) splits ``db.existing_ids(...)``
into new/old. ``old_idx`` (chashes the probe reported present) goes to
``db.update_chunks(...)`` — a METADATA-ONLY update, no content, no
embedding. When the probe is a STALE POSITIVE — the id is reported
present but the row is actually gone (a concurrent delete, a prior
partial run) — the metadata-only update touches nothing and the content
is silently dropped. This is the mechanism behind the parent bug's worst
observed state: manifest rows with no backing chunk, reported as a clean
index run.

The fix, per nexus-5xn3k.2 (engine half, already shipped): the service's
``/v1/vectors/update-metadata`` response is ``{"updated": N, "missing":
[ids...]}``. This file pins the CLIENT half: every id in ``missing`` is
re-routed through a full upsert (content + embeddings), and an engine
that omits the ``"missing"`` key entirely (pre-fence engine) degrades to
today's behaviour — no reroute — WITHOUT ever being silently treated as
"zero misses".

These tests exercise ``_upsert_skip_reembed`` end-to-end against a real
``HttpVectorClient`` with a fake in-process "engine" behind ``_post`` /
``_get`` — not a call-count mock — so a passing test proves the content
LANDS in the (fake) store, not merely that some method was invoked.
"""
from __future__ import annotations

import logging

import pytest
import structlog

from nexus.db.http_vector_client import HttpVectorClient
from nexus.db.limits import ServiceLimits
from nexus.doc_indexer import _upsert_skip_reembed

_COLL = "code__nexus-1-1__voyage-code-3__v1"


def _route_structlog_to_stdlib() -> None:
    """Route structlog's ``_log.warning(...)`` calls through stdlib logging
    so ``caplog`` can observe them — same pattern as
    tests/test_5xn3k_staleness_verifies.py."""
    structlog.configure(
        processors=[structlog.stdlib.render_to_log_kwargs],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
    )


class _FakeEngine:
    """Minimal in-process stand-in for the vector-service HTTP surface.

    Two independent pieces of state, deliberately decoupled so a STALE
    POSITIVE can be modeled directly: ``probe_present`` is what
    ``/v1/vectors/store-get`` (``existing_ids``) reports as present;
    ``rows`` is what is ACTUALLY stored (read by update-metadata to
    compute ``missing``, and read back by the test to assert the reroute
    landed real content).
    """

    def __init__(self) -> None:
        self.probe_present: set[str] = set()
        self.rows: dict[str, dict] = {}  # id -> {"document": ..., "metadata": ...}
        self.missing_key_enabled = True  # False simulates a pre-fence engine
        # Per-call override for a MIXED-VERSION engine fleet (rolling deploy
        # mid-request): page N includes/omits "missing" per this schedule,
        # index-aligned to the Nth call to /v1/vectors/update-metadata.
        # Falls back to missing_key_enabled once the schedule is exhausted.
        self.missing_key_schedule: list[bool] | None = None
        self._update_metadata_calls = 0

    def post(self, path: str, body: dict, **_kw) -> dict:
        if path == "/v1/vectors/store-get":
            found = [i for i in body["ids"] if i in self.probe_present]
            return {"ids": found}
        if path == "/v1/vectors/upsert-chunks":
            ids = body["ids"]
            docs = body["documents"]
            metas = body.get("metadatas") or [{}] * len(ids)
            for cid, doc, meta in zip(ids, docs, metas, strict=True):
                self.rows[cid] = {"document": doc, "metadata": meta}
                self.probe_present.add(cid)
            return {"upserted": len(ids)}
        if path == "/v1/vectors/update-metadata":
            ids = body["ids"]
            metas = body["metadatas"]
            updated = 0
            missing: list[str] = []
            for cid, meta in zip(ids, metas, strict=True):
                if cid in self.rows:
                    self.rows[cid]["metadata"] = meta
                    updated += 1
                else:
                    missing.append(cid)
            result = {"updated": updated}
            call_idx = self._update_metadata_calls
            self._update_metadata_calls += 1
            if self.missing_key_schedule is not None and call_idx < len(self.missing_key_schedule):
                include_key = self.missing_key_schedule[call_idx]
            else:
                include_key = self.missing_key_enabled
            if include_key:
                result["missing"] = missing
            return result
        raise AssertionError(f"unexpected path in fake engine: {path}")


@pytest.fixture
def engine(monkeypatch) -> _FakeEngine:
    fake = _FakeEngine()
    monkeypatch.setattr("nexus.db.http_vector_client._post", fake.post)
    monkeypatch.setattr(
        "nexus.db.http_vector_client.is_vector_service_mode", lambda: True,
    )
    # update_chunks_missing_unreported is a log-once-per-process throttle
    # (substantive-critic follow-up) — reset it per test so tests that
    # expect the WARNING (not the later-call DEBUG echo) get a clean slate,
    # same convention as TestSkipExistingDeprecationNotice._reset_dedup_flag
    # in tests/db/test_http_vector_client.py.
    import nexus.db.http_vector_client as hvc
    monkeypatch.setattr(hvc, "_update_chunks_missing_unreported_logged", False)
    return fake


def _client() -> HttpVectorClient:
    return HttpVectorClient()


# ── Core defect: stale-positive probe must not lose content ────────────────


def test_stale_positive_reroutes_and_content_lands(engine, caplog):
    """The chash at issue: probe says present, store says absent.

    Real regression shape — a document is re-indexed after a prior partial
    run left the manifest's existing_ids probe believing chash X already
    has a stored vector, when the row was never actually written (or was
    deleted). Without the reroute, the metadata-only update silently no-ops
    and the chunk text never lands.
    """
    db = _client()
    stale_id = "aa" * 32
    fresh_id = "bb" * 32
    # Probe reports BOTH present; only fresh_id actually has a backing row.
    engine.probe_present = {stale_id, fresh_id}
    engine.rows = {fresh_id: {"document": "old fresh content", "metadata": {}}}

    ids = [stale_id, fresh_id]
    documents = ["THE STALE CONTENT THAT MUST LAND", "new fresh content"]
    embeddings = [[], []]
    metadatas = [{"source_path": "stale.py"}, {"source_path": "fresh.py"}]

    _route_structlog_to_stdlib()
    with caplog.at_level(logging.WARNING, logger="nexus.doc_indexer"):
        _upsert_skip_reembed(db, _COLL, ids, documents, embeddings, metadatas)

    # Content-landing assertion — retrieved text, not a call count.
    assert stale_id in engine.rows, "stale id never got a backing row at all"
    assert engine.rows[stale_id]["document"] == "THE STALE CONTENT THAT MUST LAND"
    # fresh_id took the metadata-only path (probe correctly reported it
    # present, and it WAS present) — document text is untouched by design,
    # but the metadata refresh still ran.
    assert engine.rows[fresh_id]["document"] == "old fresh content"
    assert engine.rows[fresh_id]["metadata"] == {"source_path": "fresh.py"}
    # A stale-positive probe result is a real anomaly — must be surfaced.
    assert any(
        r.msg == "update_chunks_missing_rerouted" and r.levelname == "WARNING"
        for r in caplog.records
    ), f"expected update_chunks_missing_rerouted WARNING, got: {[(r.levelname, r.msg) for r in caplog.records]}"


def test_multiple_stale_ids_all_land(engine):
    db = _client()
    ids = [f"{c}" * 64 for c in "abcd"]
    engine.probe_present = set(ids)
    engine.rows = {}  # none actually present — every id is a stale positive
    documents = [f"content-{i}" for i in range(len(ids))]
    embeddings = [[] for _ in ids]
    metadatas = [{"n": i} for i in range(len(ids))]

    _upsert_skip_reembed(db, _COLL, ids, documents, embeddings, metadatas)

    for cid, doc in zip(ids, documents, strict=True):
        assert engine.rows[cid]["document"] == doc


# ── Engine-floor tolerance: absent "missing" key is "cannot tell" ──────────


def test_engine_floor_no_missing_key_no_reroute_but_warns(engine, caplog):
    """Pre-fence engine: response has no "missing" field at all.

    Today's (pre-fix) behaviour must be preserved exactly — no reroute,
    because the client cannot tell which ids (if any) were stale — but the
    degraded report must be surfaced at WARNING, never silently treated as
    "zero misses".
    """
    db = _client()
    engine.missing_key_enabled = False
    stale_id = "cc" * 32
    engine.probe_present = {stale_id}
    engine.rows = {}  # stale positive — but the engine can't report it

    _route_structlog_to_stdlib()
    with caplog.at_level(logging.WARNING, logger="nexus.db.http_vector_client"):
        _upsert_skip_reembed(
            db, _COLL, [stale_id], ["should NOT silently land"], [[]],
            [{"source_path": "x.py"}],
        )

    # No reroute happened — today's (degraded) behaviour preserved.
    assert stale_id not in engine.rows
    assert any(
        r.msg == "update_chunks_missing_unreported" and r.levelname == "WARNING"
        for r in caplog.records
    ), f"expected update_chunks_missing_unreported WARNING, got: {[(r.levelname, r.msg) for r in caplog.records]}"


def test_missing_key_present_but_empty_is_genuinely_zero_no_warning(engine, caplog):
    """"missing": [] (key present, empty) means the engine COULD report and
    found nothing wrong — this must NOT log a warning (it isn't an anomaly)
    and must NOT trigger a reroute.
    """
    db = _client()
    good_id = "dd" * 32
    engine.probe_present = {good_id}
    engine.rows = {good_id: {"document": "already correct", "metadata": {}}}

    _route_structlog_to_stdlib()
    with caplog.at_level(logging.WARNING):
        sent = _upsert_skip_reembed(
            db, _COLL, [good_id], ["already correct"], [[]],
            [{"source_path": "ok.py"}],
        )

    assert sent == 0  # nothing went down the (re-)embed path
    assert engine.rows[good_id]["document"] == "already correct"
    assert not any(
        r.msg in ("update_chunks_missing_rerouted", "update_chunks_missing_unreported")
        for r in caplog.records
    ), f"expected no warning, got: {[(r.levelname, r.msg) for r in caplog.records]}"


# ── code-review-expert follow-up: mixed-version engine fleet mid-request ───
#
# update_chunks pages at MAX_RECORDS_PER_WRITE. A rolling deploy can put a
# fleet member that reports "missing" behind the same load balancer as one
# that doesn't, so within a SINGLE multi-page update_chunks call, page 1 may
# report "missing" while page 2 omits the key entirely. OR-accumulating
# "did any page report it" is wrong: page 2's ids would be silently treated
# as zero-miss just because page 1 happened to report the field first.
# Cannot-tell must dominate — ALL pages must report for the union to be
# trusted; if even one page omits it, the whole call degrades to None.


def test_mixed_page_missing_field_omission_is_cannot_tell_no_reroute(engine, caplog):
    """4 ids, page size forced to 2 -> exactly 2 pages. Page 1's response
    includes "missing" (and would, on its own, look like actionable data —
    every one of its ids is a stale positive); page 2's response omits the
    key entirely (older/mixed engine). The overall call must NOT reroute
    ANY id — not even page 1's — because "missing" is only trustworthy when
    every page reports it. Falsifies the OR-accumulation bug directly:
    that bug would reroute page 1's ids and log nothing about page 2.
    """
    db = _client()
    ids = [f"{c}" * 64 for c in "abcd"]  # 4 ids
    engine.probe_present = set(ids)  # probe reports all 4 present
    engine.rows = {}  # none actually present — every id is a stale positive
    engine.missing_key_schedule = [True, False]  # page 1 reports, page 2 doesn't
    documents = [f"content-{i}" for i in range(len(ids))]
    embeddings = [[] for _ in ids]
    metadatas = [{"n": i} for i in range(len(ids))]

    _route_structlog_to_stdlib()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "nexus.db.limits.QUOTAS", ServiceLimits(MAX_RECORDS_PER_WRITE=2),
        )
        with caplog.at_level(logging.WARNING):
            _upsert_skip_reembed(db, _COLL, ids, documents, embeddings, metadatas)

    # No reroute happened for ANY id, including page 1's — cannot-tell
    # dominates the whole call, not just the page that was missing it.
    for cid in ids:
        assert cid not in engine.rows, (
            f"{cid} was rerouted despite an unreported page elsewhere in "
            "the same update_chunks call — OR-accumulation regression"
        )
    assert any(
        r.msg == "update_chunks_missing_unreported" and r.levelname == "WARNING"
        for r in caplog.records
    ), f"expected update_chunks_missing_unreported WARNING, got: {[(r.levelname, r.msg) for r in caplog.records]}"
    assert not any(
        r.msg == "update_chunks_missing_rerouted" for r in caplog.records
    ), f"reroute must not fire when any page is unreported: {[(r.levelname, r.msg) for r in caplog.records]}"


def test_all_pages_report_missing_returns_union_and_reroutes(engine, caplog):
    """Inverse control for the mixed-page test above: BOTH pages report the
    "missing" field (even though one page's is empty) -> the union is
    trusted, every stale-positive id reroutes, and NO cannot-tell warning
    fires (only the real reroute anomaly warning).
    """
    db = _client()
    ids = [f"{c}" * 64 for c in "abcd"]  # 4 ids
    engine.probe_present = set(ids)
    engine.rows = {}  # every id is a stale positive
    engine.missing_key_schedule = [True, True]  # both pages report
    documents = [f"content-{i}" for i in range(len(ids))]
    embeddings = [[] for _ in ids]
    metadatas = [{"n": i} for i in range(len(ids))]

    _route_structlog_to_stdlib()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "nexus.db.limits.QUOTAS", ServiceLimits(MAX_RECORDS_PER_WRITE=2),
        )
        with caplog.at_level(logging.WARNING):
            _upsert_skip_reembed(db, _COLL, ids, documents, embeddings, metadatas)

    for cid, doc in zip(ids, documents, strict=True):
        assert engine.rows[cid]["document"] == doc
    assert not any(
        r.msg == "update_chunks_missing_unreported" for r in caplog.records
    ), f"cannot-tell warning must not fire when every page reports: {[(r.levelname, r.msg) for r in caplog.records]}"
    assert any(
        r.msg == "update_chunks_missing_rerouted" and r.levelname == "WARNING"
        for r in caplog.records
    ), f"expected update_chunks_missing_rerouted WARNING, got: {[(r.levelname, r.msg) for r in caplog.records]}"


# ── substantive-critic follow-up: in-method anomaly signal + log-once ──────
#
# Both tests below drive HttpVectorClient.update_chunks DIRECTLY (not via
# _upsert_skip_reembed) — the in-method WARNING and the throttle are both
# properties of update_chunks itself, independent of what any particular
# caller does with the return value. structlog.testing.capture_logs() is
# used here (not caplog) to match the existing log-once convention's own
# tests — TestSkipExistingDeprecationNotice in
# tests/db/test_http_vector_client.py, the precedent this throttle mirrors.


def test_update_chunks_missing_reported_fires_even_when_caller_ignores_return(monkeypatch):
    """A genuinely non-empty ``missing`` (every page reported it) must log
    update_chunks_missing_reported from INSIDE update_chunks — unconditional,
    not gated on the caller doing anything with the return value. This is
    what makes the signal reach the other four call sites (pipeline_stages
    x2, indexer.py, T3's own metadata pass) that don't reroute at all.
    """
    from structlog.testing import capture_logs

    client = _client()

    def fake_post(path: str, body: dict, **_kw) -> dict:
        assert path == "/v1/vectors/update-metadata"
        return {"updated": 1, "missing": ["stale-id"]}

    monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)

    with capture_logs() as logs:
        # Return value deliberately ignored — this is the point of the test.
        client.update_chunks("col", ["stale-id", "fresh-id"], [{"a": 1}, {"b": 2}])

    reported = [e for e in logs if e["event"] == "update_chunks_missing_reported"]
    assert len(reported) == 1, f"expected exactly one, got: {logs}"
    assert reported[0]["log_level"] == "warning"
    assert reported[0]["count"] == 1
    assert reported[0]["total"] == 2


def test_update_chunks_missing_unreported_throttled_to_once_per_process(monkeypatch):
    """Two consecutive calls, both against an engine that omits "missing"
    entirely, must emit exactly ONE update_chunks_missing_unreported WARNING
    — the second is DEBUG. Without this, a full-repo reindex against a
    pre-fence engine would emit a WARNING per file and drown the rarer
    update_chunks_missing_reported / update_chunks_missing_rerouted signals.
    """
    import nexus.db.http_vector_client as hvc
    from structlog.testing import capture_logs

    # Other tests in this file call _route_structlog_to_stdlib(), which
    # mutates structlog's GLOBAL config (wrapper_class/logger_factory) and
    # is never restored. capture_logs() only swaps *processors*, so a
    # stdlib-routed wrapper_class from an earlier test would still filter
    # .debug() calls against the real "nexus.db.http_vector_client" stdlib
    # logger's level (WARNING by pytest default) before they ever reach
    # capture_logs' LogCapture — silently dropping the DEBUG echo this test
    # asserts on. Reset to structlog's own defaults (non-level-filtering)
    # so this test's outcome doesn't depend on what ran before it.
    structlog.reset_defaults()

    # Same reset convention as TestSkipExistingDeprecationNotice._reset_dedup_flag
    # in tests/db/test_http_vector_client.py.
    monkeypatch.setattr(hvc, "_update_chunks_missing_unreported_logged", False)

    client = _client()

    def fake_post(path: str, body: dict, **_kw) -> dict:
        return {"updated": 0}  # no "missing" key at all

    monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)

    with capture_logs() as logs:
        client.update_chunks("col", ["a"], [{"x": 1}])
        client.update_chunks("col", ["b"], [{"y": 2}])

    unreported = [e for e in logs if e["event"] == "update_chunks_missing_unreported"]
    warnings = [e for e in unreported if e["log_level"] == "warning"]
    debugs = [e for e in unreported if e["log_level"] == "debug"]
    assert len(warnings) == 1, f"expected exactly one WARNING, got: {unreported}"
    assert len(debugs) == 1, f"expected the second call to echo at DEBUG, got: {unreported}"
