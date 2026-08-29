# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contract tests for HttpTelemetryStore.

Test approach: in-process fake HTTP server implementing the /v1/telemetry/*
contract. The fake server mirrors the REAL Java TelemetryHandler shape faithfully.

This verifies:
  - HttpTelemetryStore makes correct HTTP calls (right paths, headers, payloads)
  - HTTP error codes map to the expected Python exceptions
  - Auth header and X-Nexus-Tenant header are sent on every request
  - import_* methods route to POST /v1/telemetry/import with correct table field
  - TIMESTAMP PRESERVATION: import paths forward the source timestamp verbatim
    (not now()); verifying the store sends the correct field, not the server-side
    behavior which is tested in TelemetryRepositoryTest.java
  - rename_collection returns the dict shape {search_telemetry, hook_failures}

Full cross-language end-to-end is in tests/db/test_http_telemetry_store_integration.py
(marked integration).
"""
from __future__ import annotations

import threading
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import pytest

from nexus.db.t2.http_telemetry_store import DEFAULT_TENANT, HttpTelemetryStore
from tests.db._fake_t2_server import FakeT2HandlerBase, fake_http_server

TOKEN = "fake-telemetry-token-abc"
PAST_TS = "2024-01-15T10:30:00Z"


# ── In-process fake server ─────────────────────────────────────────────────────

_relevance_log: list[dict[str, Any]] = []
_search_telemetry: list[dict[str, Any]] = []
_tier_writes: list[dict[str, Any]] = []
_retention_markers: dict[str, int] = {}
_nx_answer_runs: list[dict[str, Any]] = []
_hook_failures: list[dict[str, Any]] = []
_frecency: dict[str, dict[str, Any]] = {}  # keyed by chunk_id
_STORE_LOCK = threading.Lock()
_ID_SEQ: dict[str, int] = defaultdict(int)

IMPORT_LOG: list[dict[str, Any]] = []  # captures /import payloads for assertion

#: RDR-196 .p1d (nexus-nyry9.10): the fake ``GET /version`` body, toggled per
#: test to drive :meth:`HttpTelemetryStore._supports_nx_answer_steps`.
#: Defaults to a fresh-jar engine (matches this session's real substrate,
#: which carries the .p1c capability field unconditionally true).
_VERSION_RESPONSE: dict[str, Any] = {"nx_answer_steps_supported": True}
#: Counts ``GET /version`` hits — proves the per-instance probe cache
#: actually avoids a re-probe on a second call against the SAME client.
_VERSION_REQUEST_COUNT: dict[str, int] = {"n": 0}


def _clear_all() -> None:
    with _STORE_LOCK:
        _relevance_log.clear()
        _search_telemetry.clear()
        _tier_writes.clear()
        _retention_markers.clear()
        _nx_answer_runs.clear()
        _hook_failures.clear()
        _frecency.clear()
        _ID_SEQ.clear()
        IMPORT_LOG.clear()
        _VERSION_RESPONSE.clear()
        _VERSION_RESPONSE.update({"nx_answer_steps_supported": True})
        _VERSION_REQUEST_COUNT["n"] = 0


class _FakeTelemetryHandler(FakeT2HandlerBase):
    """In-process stub of TelemetryHandler (Java)."""

    TOKEN = TOKEN

    def do_POST(self):
        if not self._check_auth():
            return
        pp = urlparse(self.path).path
        body = self._body()

        if pp == "/v1/telemetry/relevance/log":
            with _STORE_LOCK:
                _ID_SEQ["rel"] += 1
                row = {
                    "id":         _ID_SEQ["rel"],
                    "query":      body.get("query", ""),
                    "chunk_id":   body.get("chunk_id", ""),
                    "collection": body.get("collection", ""),
                    "action":     body.get("action", ""),
                    "session_id": body.get("session_id", ""),
                    "timestamp":  datetime.now(UTC).isoformat(),
                }
                _relevance_log.append(row)
            self._send(200, {"id": row["id"]})

        elif pp == "/v1/telemetry/relevance/batch":
            rows = body.get("rows", [])
            with _STORE_LOCK:
                for r in rows:
                    _ID_SEQ["rel"] += 1
                    _relevance_log.append({
                        "id":         _ID_SEQ["rel"],
                        "query":      r[0] if len(r) > 0 else "",
                        "chunk_id":   r[1] if len(r) > 1 else "",
                        "collection": r[2] if len(r) > 2 else "",
                        "action":     r[3] if len(r) > 3 else "",
                        "session_id": r[4] if len(r) > 4 else "",
                        "timestamp":  datetime.now(UTC).isoformat(),
                    })
            self._send(200, {"inserted": len(rows)})

        elif pp == "/v1/telemetry/relevance/expire":
            days = int(body.get("days", 90))
            cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
            with _STORE_LOCK:
                before = len(_relevance_log)
                _relevance_log[:] = [r for r in _relevance_log if r["timestamp"] >= cutoff]
                deleted = before - len(_relevance_log)
            self._send(200, {"deleted": deleted})

        elif pp == "/v1/telemetry/search/batch":
            rows = body.get("rows", [])
            with _STORE_LOCK:
                for r in rows:
                    _search_telemetry.append({
                        "ts":           r[0] if len(r) > 0 else "",
                        "query_hash":   r[1] if len(r) > 1 else "",
                        "collection":   r[2] if len(r) > 2 else "",
                        "raw_count":    r[3] if len(r) > 3 else 0,
                        "kept_count":   r[4] if len(r) > 4 else 0,
                        "top_distance": r[5] if len(r) > 5 else None,
                        "threshold":    r[6] if len(r) > 6 else None,
                    })
            self._send(200, {"inserted": len(rows)})

        elif pp == "/v1/telemetry/search/trim":
            days = int(body.get("days", 30))
            dry_run = bool(body.get("dry_run", False))
            cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
            with _STORE_LOCK:
                if dry_run:
                    # Mirrors TelemetryRepository.trimSearchTelemetry: the
                    # SAME predicate as the delete branch, just counted
                    # instead of applied.
                    deleted = sum(1 for r in _search_telemetry if r["ts"] < cutoff)
                else:
                    before = len(_search_telemetry)
                    _search_telemetry[:] = [r for r in _search_telemetry if r["ts"] >= cutoff]
                    deleted = before - len(_search_telemetry)
            self._send(200, {"deleted": deleted, "dry_run": dry_run})

        elif pp == "/v1/telemetry/rename_collection":
            old = body.get("old", "")
            new = body.get("new", "")
            with _STORE_LOCK:
                st_count = sum(
                    1 for r in _search_telemetry
                    if r["collection"] == old
                )
                for r in _search_telemetry:
                    if r["collection"] == old:
                        r["collection"] = new
                hf_count = sum(
                    1 for r in _hook_failures
                    if r.get("collection") == old
                )
                for r in _hook_failures:
                    if r.get("collection") == old:
                        r["collection"] = new
            self._send(200, {
                "search_telemetry": st_count,
                "hook_failures":    hf_count,
            })

        elif pp == "/v1/telemetry/tier_writes/record":
            with _STORE_LOCK:
                _ID_SEQ["tw"] += 1
                _tier_writes.append({
                    "id":           _ID_SEQ["tw"],
                    "session_id":   body.get("session_id", ""),
                    "ts":           body.get("ts", datetime.now(UTC).isoformat()),
                    "tool":         body.get("tool", ""),
                    "tier":         body.get("tier", ""),
                    "agent":        body.get("agent"),
                    "project":      body.get("project"),
                    "target_title": body.get("target_title"),
                })
            self._send(200, {"ok": True})

        elif pp == "/v1/telemetry/nx_answer_runs/record":
            with _STORE_LOCK:
                _ID_SEQ["nar"] += 1
                # cost_usd: mirrors the REAL engine POST telemetry-007-3
                # (RDR-196 .p1c-b, nexus-lme1s) — nx_answer_runs.cost_usd
                # DROPped its NOT NULL DEFAULT 0.0, and
                # TelemetryHandler.handleNxAnswerRunRecord now passes the
                # client's cost straight through (optDoubleNull, no more
                # `cost != null ? cost : 0.0` coercion). Absent key AND an
                # explicit JSON null both mean "no usage observed" and both
                # must be preserved as None, never fabricated to 0.0 (the
                # superseded .p1d "known-limitation" note this comment used
                # to cite no longer applies to the PARENT row).
                _raw_cost = body.get("cost_usd")
                cost_usd = float(_raw_cost) if _raw_cost is not None else None
                _nx_answer_runs.append({
                    "id":                 _ID_SEQ["nar"],
                    "question":           body.get("question", ""),
                    "plan_id":            body.get("plan_id"),
                    "matched_confidence": body.get("matched_confidence"),
                    "step_count":         int(body.get("step_count", 0) or 0),
                    "final_text":         body.get("final_text", ""),
                    "cost_usd":           cost_usd,
                    "duration_ms":        int(body.get("duration_ms", 0) or 0),
                    "created_at":         body.get("created_at") or datetime.now(UTC).isoformat(),
                    # RDR-196 .p1d (nexus-nyry9.10): captured verbatim (or
                    # absent) so tests can assert whether the client sent
                    # per-step telemetry — never re-derived/defaulted here.
                    "steps":              body.get("steps"),
                })
            self._send(200, {"ok": True})

        elif pp == "/v1/telemetry/hook_failures/record":
            with _STORE_LOCK:
                _ID_SEQ["hf"] += 1
                _hook_failures.append({
                    "id":          _ID_SEQ["hf"],
                    "hook_name":   body.get("hook_name", ""),
                    "occurred_at": body.get("occurred_at", datetime.now(UTC).isoformat()),
                    "collection":  body.get("collection", ""),
                })
            self._send(200, {"ok": True})

        elif pp == "/v1/telemetry/hook_failures/trim":
            days = int(body.get("days", 30))
            dry_run = bool(body.get("dry_run", False))
            cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
            with _STORE_LOCK:
                if dry_run:
                    deleted = sum(1 for r in _hook_failures if r["occurred_at"] < cutoff)
                else:
                    before = len(_hook_failures)
                    _hook_failures[:] = [
                        r for r in _hook_failures if r["occurred_at"] >= cutoff
                    ]
                    deleted = before - len(_hook_failures)
            self._send(200, {"deleted": deleted, "dry_run": dry_run})

        elif pp == "/v1/telemetry/frecency/upsert":
            chunk_id = body.get("chunk_id", "")
            with _STORE_LOCK:
                existing = _frecency.get(chunk_id)
                if existing is None:
                    _frecency[chunk_id] = dict(body)
                else:
                    # GREATEST for score/count/last_hit_at, LEAST for embedded_at
                    def _gts(a: str | None, b: str | None) -> str | None:
                        if a is None: return b
                        if b is None: return a
                        return max(a, b)
                    def _lts(a: str | None, b: str | None) -> str | None:
                        if a is None: return b
                        if b is None: return a
                        return min(a, b)
                    existing["frecency_score"] = max(
                        float(existing.get("frecency_score", 0.0) or 0.0),
                        float(body.get("frecency_score", 0.0) or 0.0),
                    )
                    existing["miss_count"] = max(
                        int(existing.get("miss_count", 0) or 0),
                        int(body.get("miss_count", 0) or 0),
                    )
                    existing["last_hit_at"] = _gts(
                        existing.get("last_hit_at"), body.get("last_hit_at")
                    )
                    existing["embedded_at"] = _lts(
                        existing.get("embedded_at"), body.get("embedded_at")
                    )
            self._send(200, {"ok": True})

        elif pp == "/v1/telemetry/import":
            # Capture payload for assertion; do same routing as above
            with _STORE_LOCK:
                IMPORT_LOG.append(dict(body))
            table = body.get("table", "")
            if table == "relevance_log":
                with _STORE_LOCK:
                    _ID_SEQ["rel"] += 1
                    _relevance_log.append({
                        "id":         _ID_SEQ["rel"],
                        "query":      body.get("query", ""),
                        "chunk_id":   body.get("chunk_id", ""),
                        "collection": body.get("collection", ""),
                        "action":     body.get("action", ""),
                        "session_id": body.get("session_id", ""),
                        # VERBATIM from source — fidelity-preserving import
                        "timestamp":  body.get("timestamp", ""),
                    })
            elif table == "frecency":
                chunk_id = body.get("chunk_id", "")
                with _STORE_LOCK:
                    existing = _frecency.get(chunk_id)
                    if existing is None:
                        _frecency[chunk_id] = dict(body)
                    else:
                        # GREATEST/LEAST conflict
                        def _g(a, b, default=0.0):
                            a = a if a is not None else default
                            b = b if b is not None else default
                            return max(a, b)
                        def _gts(a, b):
                            if a is None: return b
                            if b is None: return a
                            return max(a, b)
                        def _lts(a, b):
                            if a is None: return b
                            if b is None: return a
                            return min(a, b)
                        existing["frecency_score"] = _g(
                            body.get("frecency_score"), existing.get("frecency_score")
                        )
                        existing["miss_count"] = int(_g(
                            body.get("miss_count"), existing.get("miss_count"), 0
                        ))
                        existing["last_hit_at"] = _gts(
                            existing.get("last_hit_at"), body.get("last_hit_at")
                        )
                        existing["embedded_at"] = _lts(
                            existing.get("embedded_at"), body.get("embedded_at")
                        )
            # For other tables: just record in IMPORT_LOG (already done above)
            self._send(200, {"ok": True})

        else:
            self._send(404, {"error": "not found"})

    def do_GET(self):
        pp = urlparse(self.path).path
        # RDR-196 .p1d (nexus-nyry9.10): mirrors the real ``VersionHandler``
        # contract — ``/version`` is unauthenticated (checked BEFORE
        # ``_check_auth()``, unlike every other route below).
        if pp == "/version":
            with _STORE_LOCK:
                _VERSION_REQUEST_COUNT["n"] += 1
                body = dict(_VERSION_RESPONSE)
            self._send(200, body)
            return
        if not self._check_auth():
            return
        qs = self._qs()

        if pp == "/v1/telemetry/relevance/query":
            q         = qs.get("query", "")
            chunk_id  = qs.get("chunk_id", "")
            action    = qs.get("action", "")
            session_id = qs.get("session_id", "")
            limit     = int(qs.get("limit", "100"))
            with _STORE_LOCK:
                results = [
                    r for r in _relevance_log
                    if (not q         or r["query"]      == q)
                    and (not chunk_id  or r["chunk_id"]   == chunk_id)
                    and (not action    or r["action"]     == action)
                    and (not session_id or r["session_id"] == session_id)
                ]
                results = sorted(results, key=lambda x: x["timestamp"], reverse=True)[:limit]
            self._send(200, results)

        elif pp == "/v1/telemetry/relevance/stats":
            # nexus-v0x32: mirror TelemetryRepository.relevanceStats — whole-
            # tenant count/oldest/newest, no filters, no paging.
            with _STORE_LOCK:
                timestamps = [r["timestamp"] for r in _relevance_log]
            self._send(200, {
                "count":  len(timestamps),
                "oldest": min(timestamps) if timestamps else None,
                "newest": max(timestamps) if timestamps else None,
            })

        elif pp == "/v1/telemetry/tier_writes/query":
            # nexus-59wjj: mirror TelemetryRepository.queryTierWrites — filter
            # precedence last_n (sessions) > session_id > since; group by
            # (tool, tier, agent, project); "" for NULL agent/project.
            session_id = qs.get("session_id", "")
            since      = qs.get("since", "")
            last_n     = int(qs.get("last_n", "0"))
            with _STORE_LOCK:
                rows = list(_tier_writes)
            if last_n > 0:
                latest: dict[str, str] = {}
                for r in rows:
                    if r["session_id"] not in latest or r["ts"] > latest[r["session_id"]]:
                        latest[r["session_id"]] = r["ts"]
                keep = {
                    s for s, _ in sorted(
                        latest.items(), key=lambda kv: kv[1], reverse=True,
                    )[:last_n]
                }
                rows = [r for r in rows if r["session_id"] in keep]
            elif session_id:
                rows = [r for r in rows if r["session_id"] == session_id]
            elif since:
                rows = [r for r in rows if r["ts"] >= since]
            groups: dict[tuple, int] = {}
            for r in rows:
                key = (r["tool"], r["tier"], r["agent"] or "", r["project"] or "")
                groups[key] = groups.get(key, 0) + 1
            out = [
                {"tool": t, "tier": ti, "agent": a, "project": p, "count": n}
                for (t, ti, a, p), n in sorted(
                    groups.items(), key=lambda kv: (kv[0][1], kv[0][0]),
                )
            ]
            self._send(200, out)
            return

        elif pp == "/v1/telemetry/tier_writes/list":
            # nexus-onjvy gap 4: mirror TelemetryRepository.listTierWrites —
            # same filter precedence as /tier_writes/query, but UNAGGREGATED
            # per-row detail including target_title, ordered ts desc, id desc.
            # Review finding (reviewer/critic convergence): capped page +
            # exact total, same envelope discipline as hook_failures/list —
            # "total" is computed over the FULL filtered set, before "limit".
            session_id = qs.get("session_id", "")
            since      = qs.get("since", "")
            last_n     = int(qs.get("last_n", "0"))
            limit      = int(qs.get("limit", "100"))
            with _STORE_LOCK:
                rows = list(_tier_writes)
            if last_n > 0:
                latest: dict[str, str] = {}
                for r in rows:
                    if r["session_id"] not in latest or r["ts"] > latest[r["session_id"]]:
                        latest[r["session_id"]] = r["ts"]
                keep = {
                    s for s, _ in sorted(
                        latest.items(), key=lambda kv: kv[1], reverse=True,
                    )[:last_n]
                }
                rows = [r for r in rows if r["session_id"] in keep]
            elif session_id:
                rows = [r for r in rows if r["session_id"] == session_id]
            elif since:
                rows = [r for r in rows if r["ts"] >= since]
            rows = sorted(rows, key=lambda r: (r["ts"], r["id"]), reverse=True)
            total = len(rows)
            page = rows[:limit]
            out = {
                "rows": [
                    {
                        "session_id":   r["session_id"],
                        "ts":           r["ts"],
                        "tool":         r["tool"],
                        "tier":         r["tier"],
                        "agent":        r["agent"],
                        "project":      r["project"],
                        "target_title": r["target_title"],
                    }
                    for r in page
                ],
                "total": total,
            }
            self._send(200, out)
            return

        elif pp == "/v1/telemetry/retention/markers":
            relations = [r for r in qs.get("relations", "").split(",") if r]
            with _STORE_LOCK:
                markers = {r: _retention_markers[r] for r in relations
                           if r in _retention_markers}
            self._send(200, {"markers": markers})
            return

        elif pp == "/v1/telemetry/search/stats":
            collection = qs.get("collection", "")
            days       = int(qs.get("days", "30"))
            cutoff     = (datetime.now(UTC) - timedelta(days=days)).isoformat()
            with _STORE_LOCK:
                rows = [r for r in _search_telemetry
                        if r["collection"] == collection and r["ts"] >= cutoff]
            row_count = len(rows)
            zero_count = sum(1 for r in rows if r.get("kept_count", 1) == 0)
            zero_hit_rate = zero_count / row_count if row_count else None
            dists = [r["top_distance"] for r in rows
                     if r.get("raw_count", 0) > 0 and r.get("top_distance") is not None]
            median = None
            if dists:
                dists.sort()
                n = len(dists)
                median = (dists[n // 2] if n % 2 == 1
                          else (dists[n // 2 - 1] + dists[n // 2]) / 2)
            self._send(200, {
                "row_count":           row_count,
                "zero_hit_rate":       zero_hit_rate,
                "median_top_distance": median,
            })

        elif pp == "/v1/telemetry/nx_answer_runs/query":
            # nexus-eho3u: mirror TelemetryRepository.queryNxAnswerRuns —
            # aggregates (total/oldest/hit-fallback/buckets/averages) over
            # the WHOLE since-filtered set, page capped by limit.
            since = qs.get("since", "")
            limit = int(qs.get("limit", "20"))
            # RDR-196 .p1c-b (nexus-lme1s): optional include_steps=true —
            # mirrors TelemetryRepository.queryNxAnswerRuns's includeSteps
            # overload. Absent/false is byte-for-byte the pre-existing
            # response shape (no "steps" key at all).
            include_steps = qs.get("include_steps", "").lower() == "true"
            with _STORE_LOCK:
                rows = list(_nx_answer_runs)
            if since:
                rows = [r for r in rows if r["created_at"] >= since]
            rows.sort(key=lambda r: (r["created_at"], r["id"]), reverse=True)

            total = len(rows)
            oldest = min((r["created_at"] for r in rows), default="")
            # plan_id 0 is the ad-hoc Match sentinel every successful
            # inline-planner run carries (core.py::_nx_answer_plan_miss) —
            # NOT a real plan (plans.id is BIGSERIAL). Mirrors the
            # corrected TelemetryRepository.queryNxAnswerRuns predicate
            # (nexus-eho3u review fix).
            hit_count = sum(
                1 for r in rows
                if r.get("plan_id") is not None and r.get("plan_id") != 0
            )
            fallback_count = total - hit_count
            durations = [r["duration_ms"] for r in rows]
            # None-filtered — mirrors SQL AVG()'s ignore-null semantics (now
            # reachable here since cost_usd can be None, RDR-196 .p1c-b):
            # a null-cost row must not crash sum() nor silently avg in as 0.
            costs = [r["cost_usd"] for r in rows if r["cost_usd"] is not None]
            avg_duration_ms = sum(durations) / len(durations) if durations else None
            avg_cost_usd = sum(costs) / len(costs) if costs else None

            buckets = {
                "under_5s": 0, "5s_to_30s": 0, "30s_to_2min": 0,
                "2min_to_5min": 0, "over_5min": 0,
            }
            for d in durations:
                if d < 5_000:
                    buckets["under_5s"] += 1
                elif d < 30_000:
                    buckets["5s_to_30s"] += 1
                elif d < 120_000:
                    buckets["30s_to_2min"] += 1
                elif d <= 300_000:
                    buckets["2min_to_5min"] += 1
                else:
                    buckets["over_5min"] += 1

            page = rows[:limit]
            self._send(200, {
                "rows": [
                    {
                        "id": r["id"], "question": r["question"],
                        "plan_id": r.get("plan_id"),
                        "matched_confidence": r.get("matched_confidence"),
                        "step_count": r.get("step_count", 0),
                        "final_text": r.get("final_text", ""),
                        "cost_usd": r.get("cost_usd", 0.0),
                        "duration_ms": r.get("duration_ms", 0),
                        "created_at": r["created_at"],
                        **({"steps": r.get("steps") or []} if include_steps else {}),
                    }
                    for r in page
                ],
                "total": total,
                "oldest_created_at": oldest,
                "hit_count": hit_count,
                "fallback_count": fallback_count,
                "avg_duration_ms": avg_duration_ms,
                "avg_cost_usd": avg_cost_usd,
                "latency_buckets": buckets,
            })

        elif pp == "/v1/telemetry/frecency/get":
            chunk_id = qs.get("chunk_id", "")
            with _STORE_LOCK:
                row = _frecency.get(chunk_id)
            if row is None:
                self._send(404, {"error": "not found"})
            else:
                self._send(200, row)

        else:
            self._send(404, {"error": "not found"})


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def fake_server():
    """Start the fake TelemetryHandler server on a random free port."""
    with fake_http_server(_FakeTelemetryHandler) as url:
        yield url


@pytest.fixture(autouse=True)
def clear_stores():
    """Clear all in-memory stores before each test."""
    _clear_all()
    yield
    _clear_all()


@pytest.fixture
def client(fake_server):
    """HttpTelemetryStore connected to the fake server."""
    c = HttpTelemetryStore(base_url=fake_server, _token=TOKEN)
    yield c
    c.close()


# ── Tests ──────────────────────────────────────────────────────────────────────

class TestLogRelevance:
    def test_log_returns_id(self, client):
        rid = client.log_relevance("test query", "chunk-1", "store_put")
        assert isinstance(rid, int)
        assert rid > 0

    def test_log_roundtrip_query(self, client):
        client.log_relevance("round-trip query", "chunk-rt", "catalog_link",
                             collection="knowledge__nexus")
        rows = client.get_relevance_log(query="round-trip query")
        assert len(rows) == 1
        assert rows[0]["chunk_id"] == "chunk-rt"
        assert rows[0]["action"] == "catalog_link"
        assert rows[0]["collection"] == "knowledge__nexus"

    # test_log_sends_auth_headers moved to the store-parametrized auth
    # contract in test_t2_store_crud_contract.py (test-suite-compression
    # P1d) — telemetry, plan_library, and chash_index all adopt
    # RefreshableHttpStoreMixin and raise the identical "cannot self-heal"
    # RuntimeError for a fully-pinned wrong-token store.


class TestLogRelevanceBatch:
    def test_batch_returns_count(self, client):
        rows = [
            ("q1", "c1", "coll1", "store_put", "sess1"),
            ("q2", "c2", "coll2", "catalog_link", "sess2"),
        ]
        count = client.log_relevance_batch(rows)
        assert count == 2

    def test_batch_empty_returns_zero(self, client):
        assert client.log_relevance_batch([]) == 0


class TestGetRelevanceLog:
    def test_filter_by_query(self, client):
        client.log_relevance("find-me", "c1", "a1")
        client.log_relevance("skip-me", "c2", "a2")
        rows = client.get_relevance_log(query="find-me")
        assert len(rows) == 1
        assert rows[0]["query"] == "find-me"

    def test_filter_by_chunk_id(self, client):
        client.log_relevance("q", "target-chunk", "store_put")
        client.log_relevance("q", "other-chunk", "store_put")
        rows = client.get_relevance_log(chunk_id="target-chunk")
        assert len(rows) == 1
        assert rows[0]["chunk_id"] == "target-chunk"

    def test_empty_filter_returns_all(self, client):
        client.log_relevance("q1", "c1", "a1")
        client.log_relevance("q2", "c2", "a2")
        rows = client.get_relevance_log()
        assert len(rows) >= 2

    def test_limit_honored(self, client):
        for i in range(5):
            client.log_relevance(f"q{i}", f"c{i}", "a")
        rows = client.get_relevance_log(limit=3)
        assert len(rows) <= 3


class TestGetRelevanceStats:
    """nexus-v0x32: playbook §4.5 telemetry baseline — whole-tenant
    count/oldest/newest, GET /v1/telemetry/relevance/stats."""

    def test_empty_reports_zero_and_none(self, client):
        stats = client.get_relevance_stats()
        assert stats == {"count": 0, "oldest": None, "newest": None}

    def test_counts_rows_and_reports_oldest_newest(self, client):
        # import_relevance_row writes the timestamp VERBATIM (fidelity ETL
        # path), so oldest/newest are deterministic rather than racing the
        # fake server's now()-stamped /relevance/log path.
        client.import_relevance_row(
            query="q1", chunk_id="c1", collection="", action="click",
            session_id="s", timestamp="2020-01-01T00:00:00Z",
        )
        client.import_relevance_row(
            query="q2", chunk_id="c2", collection="", action="click",
            session_id="s", timestamp="2020-06-15T12:30:00Z",
        )
        client.import_relevance_row(
            query="q3", chunk_id="c3", collection="", action="click",
            session_id="s", timestamp="2019-03-10T08:00:00Z",
        )
        stats = client.get_relevance_stats()
        assert stats["count"] == 3
        assert stats["oldest"] == "2019-03-10T08:00:00Z"
        assert stats["newest"] == "2020-06-15T12:30:00Z"


class TestExpireRelevanceLog:
    def test_expire_deletes_old_rows_on_fake(self, client):
        # Fake server uses server-side now() for log_relevance — to test expire,
        # seed an old row via import and then expire
        client.import_relevance_row(
            query="old",
            chunk_id="old-chunk",
            collection="",
            action="store_put",
            session_id="",
            timestamp="2020-01-01T00:00:00Z",  # 4+ years ago
        )
        rows_before = client.get_relevance_log(query="old")
        assert len(rows_before) == 1
        deleted = client.expire_relevance_log(days=365 * 3)
        assert deleted >= 1


class TestLogSearchBatch:
    def test_batch_inserts(self, client):
        rows = [
            ("2024-01-01T00:00:00Z", "hash1", "code__nexus", 10, 8, 0.25, 0.3),
            ("2024-01-02T00:00:00Z", "hash2", "knowledge__nexus", 5, 5, None, None),
        ]
        count = client.log_search_batch(rows)
        assert count == 2

    def test_batch_empty(self, client):
        assert client.log_search_batch([]) == 0


class TestQueryCollectionStats:
    def test_stats_empty_collection(self, client):
        stats = client.query_collection_stats("nonexistent__coll")
        assert stats["row_count"] == 0
        assert stats["zero_hit_rate"] is None
        assert stats["median_top_distance"] is None

    def test_stats_with_data(self, client):
        rows = [
            ("2025-01-01T00:00:00Z", "h1", "code__nexus", 10, 8, 0.2, 0.3),
            ("2025-01-02T00:00:00Z", "h2", "code__nexus",  5, 0, 0.4, 0.3),
        ]
        client.log_search_batch(rows)
        stats = client.query_collection_stats("code__nexus", days=365 * 5)
        assert stats["row_count"] == 2
        assert stats["zero_hit_rate"] == 0.5  # 1 of 2 rows has kept_count=0


class TestTrimSearchTelemetry:
    def test_trim_days_validation(self, client):
        with pytest.raises(ValueError, match="days must be >= 1"):
            client.trim_search_telemetry(days=0)

    def test_trim_removes_old(self, client):
        rows = [
            ("2020-01-01T00:00:00Z", "old-hash", "code__nexus", 1, 1, None, None),
        ]
        client.log_search_batch(rows)
        deleted = client.trim_search_telemetry(days=365 * 3)
        assert deleted >= 1

    def test_dry_run_previews_then_matches_the_real_trim(self, client):
        """Non-vacuity: seed rows straddling the cutoff, preview, run the real
        trim, and prove the numbers match AND the dry run left the table
        untouched (the nexus-3rr3x class this design explicitly avoids)."""
        rows = [
            ("2020-01-01T00:00:00Z", "dr-old-1", "code__nexus", 1, 1, None, None),
            ("2020-01-02T00:00:00Z", "dr-old-2", "code__nexus", 1, 1, None, None),
        ]
        client.log_search_batch(rows)

        preview = client.trim_search_telemetry(days=365 * 3, dry_run=True)
        assert preview == 2

        # Dry run must not have deleted anything: a second dry run still
        # reports the same population.
        assert client.trim_search_telemetry(days=365 * 3, dry_run=True) == preview

        deleted = client.trim_search_telemetry(days=365 * 3, dry_run=False)
        assert deleted == preview, "real trim must delete exactly the previewed count"

        assert client.trim_search_telemetry(days=365 * 3, dry_run=True) == 0

    def test_dry_run_defaults_to_false_backward_compatible(self, client):
        """The default call shape (no dry_run kwarg) is every pre-existing
        caller — it must keep actually deleting."""
        rows = [("2020-01-01T00:00:00Z", "compat-hash", "code__nexus", 1, 1, None, None)]
        client.log_search_batch(rows)
        deleted = client.trim_search_telemetry(days=365 * 3)
        assert deleted == 1


class TestQueryTierWrites:
    """nexus-59wjj: nx tier-status read parity — GET /v1/telemetry/tier_writes/query."""

    def test_session_filter_groups_and_maps_nulls(self, client):
        client.record_tier_write(
            session_id="s1", ts="2026-07-15T10:00:00Z",
            tool="memory_put", tier="T2", agent="developer", project="nexus",
        )
        client.record_tier_write(
            session_id="s1", ts="2026-07-15T10:01:00Z",
            tool="memory_put", tier="T2", agent="developer", project="nexus",
        )
        client.record_tier_write(
            session_id="s1", ts="2026-07-15T10:02:00Z",
            tool="store_put", tier="T3",
        )
        client.record_tier_write(
            session_id="s2", ts="2026-07-15T10:03:00Z",
            tool="scratch", tier="T1",
        )

        rows = client.query_tier_writes(session_id="s1")
        assert rows == [
            ("memory_put", "T2", "developer", "nexus", 2),
            ("store_put", "T3", None, None, 1),  # "" from wire → None
        ]

    def test_last_n_sessions_and_since(self, client):
        client.record_tier_write(
            session_id="old", ts="2026-07-01T00:00:00Z", tool="a", tier="T1",
        )
        client.record_tier_write(
            session_id="new", ts="2026-07-15T00:00:00Z", tool="b", tier="T2",
        )

        recent = client.query_tier_writes(last_n=1)
        assert recent == [("b", "T2", None, None, 1)]

        since = client.query_tier_writes(since="2026-07-10T00:00:00Z")
        assert since == [("b", "T2", None, None, 1)]

    def test_empty_returns_empty_list(self, client):
        assert client.query_tier_writes(session_id="nope") == []

    def test_query_tier_writes_once_real_transport_bypasses_retry(
        self, client, monkeypatch,
    ):
        # nexus-ov13k review: the single-attempt variant must go through the
        # REAL construction + raw client (not the mocked class the launcher
        # tests use) and must NEVER enter the retrying _send path — the
        # 20-50s worst case it exists to avoid.
        client.record_tier_write(
            session_id="once-1", ts="2026-07-16T00:00:00Z",
            tool="store_put", tier="T3",
        )

        def _no_send(*_a, **_kw):
            raise AssertionError("query_tier_writes_once must bypass _send")

        monkeypatch.setattr(type(client), "_send", _no_send)
        rows = client.query_tier_writes_once(session_id="once-1", timeout=2.0)
        assert rows == [("store_put", "T3", None, None, 1)]

    def test_query_tier_writes_once_raises_on_http_error(
        self, client, monkeypatch,
    ):
        # Single attempt: an HTTP error surfaces raw to the caller (whose
        # contract is best-effort) — no swallow, no self-heal retry. A bad
        # bearer makes the fake server 401.
        import httpx
        import pytest as _pytest

        monkeypatch.setattr(
            type(client), "_auth_headers",
            lambda self: {"Authorization": "Bearer wrong", "X-Nexus-Tenant": "default"},
        )
        with _pytest.raises(httpx.HTTPStatusError):
            client.query_tier_writes_once(session_id="once-1", timeout=2.0)


class TestListTierWrites:
    """nexus-onjvy gap 4: per-row tier-write detail — GET /v1/telemetry/tier_writes/list.

    Unaggregated twin of TestQueryTierWrites: same filters, but target_title
    (unreadable through query_tier_writes' aggregate) must round-trip, and two
    rows with identical (tool, tier, agent, project) must stay two rows, not
    collapse into one grouped count. Returns {"rows": [...], "total": N} —
    same capped-page-plus-exact-total envelope discipline as
    list_hook_failures (review finding: this was the sole tier_writes read
    path with no page cap).
    """

    def test_session_filter_returns_per_row_detail_not_aggregated(self, client):
        client.record_tier_write(
            session_id="s1", ts="2026-07-15T10:00:00Z",
            tool="memory_put", tier="T2", agent="developer", project="nexus",
            target_title="notes.md",
        )
        client.record_tier_write(
            session_id="s1", ts="2026-07-15T10:01:00Z",
            tool="memory_put", tier="T2", agent="developer", project="nexus",
            target_title="other.md",
        )
        client.record_tier_write(
            session_id="s1", ts="2026-07-15T10:02:00Z",
            tool="store_put", tier="T3",
        )

        result = client.list_tier_writes(session_id="s1")
        # NOT collapsed into one (tool, tier, agent, project) group the way
        # query_tier_writes would — three writes in, three rows out.
        assert result["rows"] == [
            ("store_put", "T3", None, None, None),
            ("memory_put", "T2", "developer", "nexus", "other.md"),
            ("memory_put", "T2", "developer", "nexus", "notes.md"),
        ]
        assert result["total"] == 3

    def test_last_n_sessions_and_since(self, client):
        client.record_tier_write(
            session_id="old", ts="2026-07-01T00:00:00Z", tool="a", tier="T1",
            target_title="old-title",
        )
        client.record_tier_write(
            session_id="new", ts="2026-07-15T00:00:00Z", tool="b", tier="T2",
            target_title="new-title",
        )

        recent = client.list_tier_writes(last_n=1)
        assert recent["rows"] == [("b", "T2", None, None, "new-title")]
        assert recent["total"] == 1

        since = client.list_tier_writes(since="2026-07-10T00:00:00Z")
        assert since["rows"] == [("b", "T2", None, None, "new-title")]
        assert since["total"] == 1

    def test_empty_returns_empty_rows_and_zero_total(self, client):
        result = client.list_tier_writes(session_id="nope")
        assert result["rows"] == []
        assert result["total"] == 0

    def test_no_target_title_maps_to_none(self, client):
        client.record_tier_write(
            session_id="notitle-1", ts="2026-07-15T10:00:00Z",
            tool="scratch_put", tier="T1",
        )
        result = client.list_tier_writes(session_id="notitle-1")
        assert result["rows"] == [("scratch_put", "T1", None, None, None)]

    def test_limit_caps_the_page_but_total_stays_exact(self, client):
        # Review finding (reviewer [21898] == critic [21897], 2026-08-08):
        # list_tier_writes had no limit kwarg at all, so a caller could not
        # bound an unfiltered call even deliberately. Pin the cap
        # non-vacuously: over-insert past the default/requested limit and
        # assert the PAGE is capped while total reports the FULL count.
        for i in range(7):
            client.record_tier_write(
                session_id="cap-sess", ts=f"2026-07-20T00:00:{i:02d}Z",
                tool="memory_put", tier="T2", target_title=f"title-{i}",
            )

        capped = client.list_tier_writes(session_id="cap-sess", limit=3)
        assert len(capped["rows"]) == 3
        assert capped["total"] == 7, (
            "total must be the FULL filtered count, independent of limit — "
            "a caller asking for 3 rows must not see a total of 3"
        )
        # Most-recent-first ordering survives the cap.
        titles = [r[4] for r in capped["rows"]]
        assert titles == ["title-6", "title-5", "title-4"]

        uncapped = client.list_tier_writes(session_id="cap-sess", limit=100)
        assert len(uncapped["rows"]) == 7
        assert uncapped["total"] == 7

    def test_default_limit_matches_list_hook_failures(self, client):
        # No explicit limit kwarg -> default of 100, same as list_hook_failures.
        import inspect

        sig = inspect.signature(client.list_tier_writes)
        assert sig.parameters["limit"].default == 100


class TestNxAnswerStepsCapabilityProbe:
    """RDR-196 .p1d (nexus-nyry9.10): the ``GET /version`` capability probe
    gating ``steps[]`` on ``POST /v1/telemetry/nx_answer_runs/record``."""

    def test_probe_true_when_engine_advertises_support(self, client):
        _VERSION_RESPONSE.clear()
        _VERSION_RESPONSE.update({"nx_answer_steps_supported": True})
        assert client._supports_nx_answer_steps() is True

    def test_probe_false_when_field_absent(self, client):
        """An engine predating .p1c simply omits the field — never a KeyError,
        never a crash, just 'unsupported'."""
        _VERSION_RESPONSE.clear()
        _VERSION_RESPONSE.update({"app_version": "1.0-SNAPSHOT"})
        assert client._supports_nx_answer_steps() is False

    def test_probe_false_and_no_raise_on_explicit_false(self, client):
        _VERSION_RESPONSE.clear()
        _VERSION_RESPONSE.update({"nx_answer_steps_supported": False})
        assert client._supports_nx_answer_steps() is False

    def test_probe_cached_per_instance(self, client):
        """Two calls on the SAME store instance hit /version exactly once —
        'cache per store instance' per the bead's design note."""
        before = _VERSION_REQUEST_COUNT["n"]
        assert client._supports_nx_answer_steps() is True
        assert client._supports_nx_answer_steps() is True
        assert _VERSION_REQUEST_COUNT["n"] == before + 1

    def test_probe_false_on_transport_failure_never_raises(self):
        """nexus-moht0 vacuous-gate doctrine: FORCE the degradation branch —
        point at a port nothing listens on (not the fake server at all), so
        the probe hits a real connection failure, not a stubbed 404."""
        store = HttpTelemetryStore(
            base_url="http://127.0.0.1:1", tenant=DEFAULT_TENANT, _token=TOKEN,
        )
        try:
            assert store._supports_nx_answer_steps() is False
        finally:
            store.close()

    def test_record_includes_steps_when_supported(self, client):
        _VERSION_RESPONSE.clear()
        _VERSION_RESPONSE.update({"nx_answer_steps_supported": True})
        step = {
            "step_index": 0, "operator": "operator_generate", "source": "llm",
            "model": "claude-sonnet-5", "input_tokens": 100, "output_tokens": 50,
            "cost_usd": 0.02, "elapsed_ms": 1200, "ok": True, "bundled_steps": [],
        }
        client.record_nx_answer_run(
            question="q", plan_id=1, matched_confidence=0.8, step_count=1,
            final_text="answer", cost_usd=0.02, duration_ms=1200, steps=[step],
        )
        with _STORE_LOCK:
            row = _nx_answer_runs[-1]
        assert row["steps"] == [step]

    def test_record_omits_steps_and_warns_when_unsupported(self, client, caplog):
        """RDR-196 .p1d DO: 'probe says unsupported -> run-row-only payload
        + logged warning, never a 400'. FORCE the branch (nexus-moht0) by
        stubbing the engine to omit the capability field — not waiting for
        an ambient old-engine condition that never occurs in this suite."""
        import structlog

        _VERSION_RESPONSE.clear()
        _VERSION_RESPONSE.update({"app_version": "1.0-SNAPSHOT"})
        step = {
            "step_index": 0, "operator": "operator_generate", "source": "llm",
            "model": None, "input_tokens": None, "output_tokens": None,
            "cost_usd": None, "elapsed_ms": 500, "ok": True, "bundled_steps": [],
        }
        with structlog.testing.capture_logs() as captured:
            # No exception -> proves "never a 400": an old-engine fake that
            # does not recognize "steps" would 400 on an unexpected field
            # the same way the real engine's strict body parser would if
            # this method sent it anyway.
            client.record_nx_answer_run(
                question="q", plan_id=1, matched_confidence=0.8, step_count=1,
                final_text="answer", cost_usd=None, duration_ms=500, steps=[step],
            )
        with _STORE_LOCK:
            row = _nx_answer_runs[-1]
        assert row["steps"] is None, "run-row-only: 'steps' must not reach the wire"
        warnings = [
            e for e in captured
            if e.get("event") == "nx_answer_steps_unsupported_by_engine"
        ]
        assert warnings, f"expected a degradation warning, got: {captured}"
        assert warnings[0]["step_count"] == 1


class TestQueryNxAnswerRuns:
    """nexus-eho3u: the read half of nx_answer_runs —
    GET /v1/telemetry/nx_answer_runs/query."""

    def test_write_then_read_rows_and_aggregates(self, client):
        client.record_nx_answer_run(
            question="fallback", plan_id=None, matched_confidence=None,
            step_count=0, final_text="", cost_usd=0.0, duration_ms=2_000,
        )
        client.record_nx_answer_run(
            question="hit", plan_id=7, matched_confidence=0.9,
            step_count=1, final_text="answer", cost_usd=0.01, duration_ms=10_000,
        )

        result = client.query_nx_answer_runs()

        assert result["total"] == 2
        assert result["hit_count"] == 1
        assert result["fallback_count"] == 1
        assert result["avg_duration_ms"] == 6_000
        assert len(result["rows"]) == 2
        # Newest first.
        assert result["rows"][0]["question"] == "hit"
        assert result["rows"][0]["plan_id"] == 7
        assert result["rows"][1]["question"] == "fallback"
        assert result["rows"][1]["plan_id"] is None

    def test_plan_id_zero_is_the_ad_hoc_sentinel_not_a_hit(self, client):
        """nexus-eho3u review fix: plan_id=0 is the synthetic ad-hoc Match
        sentinel every SUCCESSFUL inline-planner run carries
        (core.py::_nx_answer_plan_miss's `Match(plan_id=0, name="ad-hoc",
        ...)`) — plans.id is BIGSERIAL, so 0 can never be a real plan. An
        earlier `plan_id is not None` predicate counted this as a HIT,
        inverting the plan-match-rate metric. Kill control: under that old
        predicate hit_count would read 2 (the real hit AND the sentinel)
        and fallback_count would read 1 — both wrong."""
        client.record_nx_answer_run(
            question="real hit", plan_id=11, matched_confidence=0.9,
            step_count=1, final_text="answer", cost_usd=0.001, duration_ms=1_000,
        )
        client.record_nx_answer_run(
            question="ad-hoc success (sentinel)", plan_id=0, matched_confidence=None,
            step_count=2, final_text="ad-hoc answer", cost_usd=0.002, duration_ms=2_000,
        )
        client.record_nx_answer_run(
            question="genuine fallback (planner error)", plan_id=None,
            matched_confidence=None, step_count=0, final_text="Planner error: x",
            cost_usd=0.0, duration_ms=3_000,
        )

        result = client.query_nx_answer_runs()

        assert result["total"] == 3
        assert result["hit_count"] == 1, "only the real matched plan (plan_id=11) counts as a hit"
        assert result["fallback_count"] == 2, (
            "plan_id=0 (ad-hoc sentinel) AND plan_id=None (genuine miss) both count as fallback"
        )
        sentinel_row = next(
            r for r in result["rows"] if r["question"] == "ad-hoc success (sentinel)"
        )
        assert sentinel_row["plan_id"] == 0

    def test_limit_caps_page_not_aggregates(self, client):
        for i in range(5):
            client.record_nx_answer_run(
                question=f"q{i}", plan_id=None, matched_confidence=None,
                step_count=0, final_text="", cost_usd=0.0, duration_ms=1_000,
            )

        result = client.query_nx_answer_runs(limit=1)
        assert len(result["rows"]) == 1
        assert result["total"] == 5

    def test_since_filters(self, client):
        client._post("/v1/telemetry/nx_answer_runs/record", {
            "question": "old", "created_at": "2020-01-01T00:00:00Z",
            "duration_ms": 1_000, "cost_usd": 0.0, "step_count": 0,
            "final_text": "",
        })
        client._post("/v1/telemetry/nx_answer_runs/record", {
            "question": "new", "created_at": "2099-01-01T00:00:00Z",
            "duration_ms": 1_000, "cost_usd": 0.0, "step_count": 0,
            "final_text": "",
        })

        recent = client.query_nx_answer_runs(since="2030-01-01T00:00:00Z")
        assert recent["total"] == 1
        assert recent["rows"][0]["question"] == "new"

        unbounded = client.query_nx_answer_runs()
        assert unbounded["total"] == 2

    def test_empty_returns_zeroed_structure(self, client):
        result = client.query_nx_answer_runs()
        assert result == {
            "rows": [], "total": 0, "oldest_created_at": "",
            "hit_count": 0, "fallback_count": 0,
            "avg_duration_ms": None, "avg_cost_usd": None,
            "latency_buckets": {
                "under_5s": 0, "5s_to_30s": 0, "30s_to_2min": 0,
                "2min_to_5min": 0, "over_5min": 0,
            },
        }

    def test_latency_buckets_sum_to_total(self, client):
        durations = [1_000, 6_000, 40_000, 150_000, 400_000]
        for i, d in enumerate(durations):
            client.record_nx_answer_run(
                question=f"bucket-{i}", plan_id=None, matched_confidence=None,
                step_count=0, final_text="", cost_usd=0.0, duration_ms=d,
            )

        result = client.query_nx_answer_runs()
        buckets = result["latency_buckets"]
        assert buckets == {
            "under_5s": 1, "5s_to_30s": 1, "30s_to_2min": 1,
            "2min_to_5min": 1, "over_5min": 1,
        }
        assert sum(buckets.values()) == result["total"]

    # ── RDR-196 .p1c-b (nexus-lme1s): include_steps ──────────────────────────

    def test_include_steps_true_returns_steps_per_row(self, client):
        step = {
            "step_index": 0, "operator": "operator_filter", "source": "sql",
            "model": None, "input_tokens": None, "output_tokens": None,
            "cost_usd": 0.0, "elapsed_ms": 12, "ok": True, "bundled_steps": [],
        }
        client.record_nx_answer_run(
            question="with steps", plan_id=1, matched_confidence=0.9,
            step_count=1, final_text="answer", cost_usd=0.0, duration_ms=1_000,
            steps=[step],
        )

        result = client.query_nx_answer_runs(include_steps=True)

        assert len(result["rows"]) == 1
        assert result["rows"][0]["steps"] == [step], (
            "steps must use the SAME field names the write side accepts "
            "(parseNxAnswerSteps), passed through verbatim"
        )

    def test_include_steps_false_omits_steps_key(self, client):
        """Default (False, and the plain 3-arg query_nx_answer_runs()) must be
        byte-for-byte the pre-existing wire shape — no 'steps' key at all,
        not even an empty one."""
        client.record_nx_answer_run(
            question="no steps requested", plan_id=None, matched_confidence=None,
            step_count=0, final_text="", cost_usd=0.0, duration_ms=1_000,
        )

        result = client.query_nx_answer_runs()

        assert len(result["rows"]) == 1
        assert "steps" not in result["rows"][0]

    def test_include_steps_true_run_with_no_steps_gets_empty_list(self, client):
        """A run recorded WITHOUT steps must still get 'steps': [] under
        include_steps=True, never an absent key or None — non-vacuity for
        the per-row shape, matching TelemetryRepository's
        getOrDefault(..., List.of()) fallback."""
        client.record_nx_answer_run(
            question="no steps written", plan_id=None, matched_confidence=None,
            step_count=0, final_text="", cost_usd=0.0, duration_ms=1_000,
        )

        result = client.query_nx_answer_runs(include_steps=True)

        assert len(result["rows"]) == 1
        assert result["rows"][0]["steps"] == []

    # ── RDR-196 .p1e (nexus-nyry9.11): read-side capability signal ────────

    def test_include_steps_true_carries_steps_supported_true(self, client):
        """.p1c critique fold (T2 [23099], recorded on .11): reuse the
        SAME capability probe the write side already gates on so a
        caller can tell 'the engine ignored include_steps' apart from
        'these rows genuinely have no steps'."""
        _VERSION_RESPONSE.clear()
        _VERSION_RESPONSE.update({"nx_answer_steps_supported": True})
        client.record_nx_answer_run(
            question="q", plan_id=None, matched_confidence=None,
            step_count=0, final_text="", cost_usd=0.0, duration_ms=1_000,
        )

        result = client.query_nx_answer_runs(include_steps=True)

        assert result["steps_supported"] is True

    def test_include_steps_true_carries_steps_supported_false_for_older_engine(
        self, client,
    ):
        _VERSION_RESPONSE.clear()
        _VERSION_RESPONSE.update({"app_version": "1.0-SNAPSHOT"})  # pre-.p1c: no field
        client.record_nx_answer_run(
            question="q", plan_id=None, matched_confidence=None,
            step_count=0, final_text="", cost_usd=0.0, duration_ms=1_000,
        )

        result = client.query_nx_answer_runs(include_steps=True)

        assert result["steps_supported"] is False

    def test_include_steps_false_omits_steps_supported_key(self, client):
        """The capability signal only makes sense when steps were
        actually requested -- must not appear (not even as None) on the
        default no-steps call, matching the existing 'no steps key at
        all' contract for the rows themselves."""
        client.record_nx_answer_run(
            question="q", plan_id=None, matched_confidence=None,
            step_count=0, final_text="", cost_usd=0.0, duration_ms=1_000,
        )

        result = client.query_nx_answer_runs()

        assert "steps_supported" not in result

    def test_null_cost_usd_reads_back_as_none_not_zero(self, client):
        """RDR-196 .p1c-b (nexus-lme1s) review fix: the fake server used to
        hardcode cost_usd null -> 0.0 on the write path, citing a
        known-limitation note that telemetry-007-3 (the real engine's
        cost_usd DROP NOT NULL) superseded. A run recorded with
        cost_usd=None must now read back as None through query, both on
        the row itself and in avg_cost_usd — never a fabricated 0.0
        indistinguishable from a genuine free call."""
        client.record_nx_answer_run(
            question="null cost run", plan_id=None, matched_confidence=None,
            step_count=0, final_text="", cost_usd=None, duration_ms=1_000,
        )
        client.record_nx_answer_run(
            question="known cost run", plan_id=1, matched_confidence=0.9,
            step_count=1, final_text="answer", cost_usd=0.02, duration_ms=1_000,
        )

        result = client.query_nx_answer_runs()

        null_row = next(r for r in result["rows"] if r["question"] == "null cost run")
        known_row = next(r for r in result["rows"] if r["question"] == "known cost run")
        assert null_row["cost_usd"] is None
        assert known_row["cost_usd"] == 0.02
        assert result["avg_cost_usd"] == 0.02, (
            "avg_cost_usd must ignore the null row (0.02 averaged over the "
            "1 non-null row, not 0.01 over both)"
        )


class TestTrimHookFailures:
    def test_trim_days_validation(self, client):
        with pytest.raises(ValueError, match="days must be >= 1"):
            client.trim_hook_failures(days=0)

    def test_trim_removes_old(self, client):
        # Old row (2020) + recent row; trim with a 3-year window deletes only the old.
        client.record_hook_failure(
            doc_id="d-old", collection="code__nexus", hook_name="h_old",
            error="boom", chain="single", occurred_at="2020-01-01T00:00:00+00:00",
        )
        client.record_hook_failure(
            doc_id="d-new", collection="code__nexus", hook_name="h_new",
            error="boom", chain="single",
            occurred_at=datetime.now(UTC).isoformat(),
        )
        deleted = client.trim_hook_failures(days=365 * 3)
        assert deleted == 1

    def test_dry_run_previews_then_matches_the_real_trim(self, client):
        """Same non-vacuity contract as search_telemetry's dry run — required
        so a doctor-level --dry-run cannot preview one trimmed table while
        silently mutating the other."""
        client.record_hook_failure(
            doc_id="dr-d-old", collection="code__nexus", hook_name="h_old",
            error="boom", chain="single", occurred_at="2020-01-01T00:00:00+00:00",
        )
        client.record_hook_failure(
            doc_id="dr-d-new", collection="code__nexus", hook_name="h_new",
            error="boom", chain="single",
            occurred_at=datetime.now(UTC).isoformat(),
        )

        preview = client.trim_hook_failures(days=365 * 3, dry_run=True)
        assert preview == 1
        assert client.trim_hook_failures(days=365 * 3, dry_run=True) == preview

        deleted = client.trim_hook_failures(days=365 * 3, dry_run=False)
        assert deleted == preview

        assert client.trim_hook_failures(days=365 * 3, dry_run=True) == 0


class TestRenameCollection:
    def test_rename_updates_search_telemetry(self, client):
        rows = [
            ("2025-01-01T00:00:00Z", "h1", "old-coll", 5, 5, None, None),
        ]
        client.log_search_batch(rows)
        result = client.rename_collection(old="old-coll", new="new-coll")
        assert isinstance(result, dict)
        assert "search_telemetry" in result
        assert "hook_failures" in result
        assert result["search_telemetry"] >= 1

    def test_rename_returns_int_counts(self, client):
        result = client.rename_collection(old="x", new="y")
        assert isinstance(result["search_telemetry"], int)
        assert isinstance(result["hook_failures"], int)


class TestImportTimestampFidelity:
    """HEADLINE: verify that import_* methods forward timestamps VERBATIM.

    The contract: the store sends ``timestamp=PAST_TS`` in the HTTP body;
    the fake server stores it verbatim. This verifies the PYTHON CLIENT sends
    the correct field. The Java service's actual TIMESTAMPTZ preservation is
    tested in TelemetryRepositoryTest.java.
    """

    def test_import_relevance_timestamp_verbatim(self, client):
        client.import_relevance_row(
            query="ts-fidelity-test",
            chunk_id="chunk-ts",
            collection="knowledge__nexus",
            action="store_put",
            session_id="sess-ts",
            timestamp=PAST_TS,
        )
        # IMPORT_LOG captures the payload sent to /v1/telemetry/import
        with _STORE_LOCK:
            payloads = [p for p in IMPORT_LOG if p.get("table") == "relevance_log"
                        and p.get("query") == "ts-fidelity-test"]
        assert len(payloads) == 1, "import request must reach the fake server"
        assert payloads[0]["timestamp"] == PAST_TS, (
            f"TIMESTAMP PRESERVATION: client must send PAST_TS={PAST_TS!r} verbatim; "
            f"got {payloads[0]['timestamp']!r}")

    def test_import_relevance_row_is_retrievable(self, client):
        client.import_relevance_row(
            query="import-retrieve",
            chunk_id="chunk-ir",
            collection="",
            action="store_put",
            session_id="",
            timestamp=PAST_TS,
        )
        rows = client.get_relevance_log(query="import-retrieve")
        assert len(rows) == 1
        assert rows[0]["timestamp"] == PAST_TS

    def test_import_search_row_forwarded(self, client):
        client.import_search_row(
            ts=PAST_TS,
            query_hash="tshash",
            collection="code__nexus",
            raw_count=10,
            kept_count=8,
            top_distance=0.25,
            threshold=0.3,
        )
        with _STORE_LOCK:
            payloads = [p for p in IMPORT_LOG if p.get("table") == "search_telemetry"]
        assert len(payloads) == 1
        assert payloads[0]["ts"] == PAST_TS, "search_telemetry ts must be forwarded verbatim"

    def test_import_tier_write_forwarded(self, client):
        client.import_tier_write(
            session_id="sess-tw",
            ts=PAST_TS,
            tool="memory_put",
            tier="T2",
            agent="developer",
            project="nexus",
            target_title="some-title",
        )
        with _STORE_LOCK:
            payloads = [p for p in IMPORT_LOG if p.get("table") == "tier_writes"]
        assert len(payloads) == 1
        assert payloads[0]["ts"] == PAST_TS

    def test_import_nx_answer_run_forwarded(self, client):
        client.import_nx_answer_run(
            question="how does X work",
            plan_id=None,
            matched_confidence=0.8,
            step_count=3,
            final_text="answer text",
            cost_usd=0.01,
            duration_ms=1500,
            created_at=PAST_TS,
        )
        with _STORE_LOCK:
            payloads = [p for p in IMPORT_LOG if p.get("table") == "nx_answer_runs"]
        assert len(payloads) == 1
        assert payloads[0]["created_at"] == PAST_TS

    def test_import_hook_failure_forwarded(self, client):
        client.import_hook_failure(
            doc_id="doc-123",
            collection="knowledge__nexus",
            hook_name="post_store",
            error="connection refused",
            occurred_at=PAST_TS,
            batch_doc_ids=None,
            is_batch=False,
            chain=None,
        )
        with _STORE_LOCK:
            payloads = [p for p in IMPORT_LOG if p.get("table") == "hook_failures"]
        assert len(payloads) == 1
        assert payloads[0]["occurred_at"] == PAST_TS


class TestFrecencyGreatestLeast:
    """Verify GREATEST no-clobber and LEAST embedded_at via the fake server."""

    def test_frecency_greatest_does_not_clobber_live_score(self, client):
        # Insert live-mutable frecency
        client.import_frecency_row(
            chunk_id="chunk-greatest",
            embedded_at="2024-01-01T00:00:00Z",
            ttl_days=30,
            frecency_score=0.95,
            miss_count=20,
            last_hit_at="2025-06-01T00:00:00Z",
        )
        # Re-import with stale (lower) values
        client.import_frecency_row(
            chunk_id="chunk-greatest",
            embedded_at="2023-01-01T00:00:00Z",
            ttl_days=30,
            frecency_score=0.50,
            miss_count=5,
            last_hit_at="2024-01-01T00:00:00Z",
        )
        row = _frecency.get("chunk-greatest")
        assert row is not None
        assert float(row["frecency_score"]) == pytest.approx(0.95), (
            "GREATEST: re-import with stale score=0.50 must not clobber live score=0.95")
        assert int(row["miss_count"]) == 20, (
            "GREATEST: re-import with stale miss_count=5 must not clobber live miss_count=20")

    def test_frecency_least_preserves_oldest_embedded_at(self, client):
        # Insert with a recent embedded_at
        client.import_frecency_row(
            chunk_id="chunk-least",
            embedded_at="2025-06-01T00:00:00Z",
            ttl_days=30,
            frecency_score=0.5,
            miss_count=1,
            last_hit_at=None,
        )
        # Re-import with an OLDER embedded_at — LEAST means older wins
        client.import_frecency_row(
            chunk_id="chunk-least",
            embedded_at="2023-01-01T00:00:00Z",
            ttl_days=30,
            frecency_score=0.3,
            miss_count=0,
            last_hit_at=None,
        )
        row = _frecency.get("chunk-least")
        assert row is not None
        assert row["embedded_at"] == "2023-01-01T00:00:00Z", (
            "LEAST: older embedded_at must win on conflict; "
            f"got {row['embedded_at']!r}")


class TestImportDoNothing:
    """Verify that importing the same event row twice results in DO NOTHING (not duplicate)."""

    def test_relevance_log_import_idempotent(self, client):
        kwargs = dict(
            query="idem-query",
            chunk_id="idem-chunk",
            collection="",
            action="store_put",
            session_id="sess",
            timestamp="2024-06-01T12:00:00Z",
        )
        client.import_relevance_row(**kwargs)
        client.import_relevance_row(**kwargs)
        # The fake server tracks by IMPORT_LOG (no dedup), but the REAL PG
        # service would return DO NOTHING. Here we just verify both calls succeed.
        rows = client.get_relevance_log(query="idem-query")
        # In the fake server there IS no dedup — idempotency is asserted by the
        # Java TelemetryRepositoryTest. Here we just verify the client makes 2
        # successful round-trips without error.
        assert len(rows) >= 1


# TestConfigErrors (test_missing_port_raises / test_missing_token_raises)
# moved to the shared parametrized suite in test_t2_store_config_contract.py
# (test-suite-compression P1b) — HttpMemoryStore and HttpTelemetryStore both
# adopt RefreshableHttpStoreMixin and raise the identical RuntimeError shape
# on unresolvable NX_SERVICE_PORT / NX_SERVICE_TOKEN.


class TestRetentionMarkers:
    def test_get_retention_markers_roundtrip(self, client):
        """nexus-24p05: the watermark's rollback-detector read."""
        _retention_markers["nexus.relevance_log"] = 7
        got = client.get_retention_markers(
            ["nexus.relevance_log", "nexus.search_telemetry"]
        )
        assert got == {"nexus.relevance_log": 7}  # never-swept relation absent

    def test_get_retention_markers_empty(self, client):
        assert client.get_retention_markers(["nexus.relevance_log"]) == {}


class TestGetRelevanceStatsAgainstRealEngine:
    """nexus-v0x32: proves ``GET /v1/telemetry/relevance/stats`` round-trips
    through the REAL engine substrate (not the fake server above). Every
    test in this suite already gets a live PG+JAR engine with a freshly
    minted per-test tenant via the autouse ``_pin_t2_substrate`` fixture
    (``tests/conftest.py``, itself built on ``tests/_engine_substrate.py``'s
    ``ensure_engine``/``mint_test_tenant``) — this test uses that ambient
    substrate directly via ``T2Database``, the same pattern
    ``tests/db/test_eho3u_nx_answer_runs_read.py`` and
    ``tests/db/test_telemetry_retention_marker.py`` use for their own
    real-engine round trips."""

    def test_relevance_stats_roundtrips_through_the_real_store(self, tmp_path):
        from nexus.db.t2 import T2Database

        db = T2Database(tmp_path / "memory.db")
        try:
            before = db.telemetry.get_relevance_stats()
            assert before == {"count": 0, "oldest": None, "newest": None}, (
                "a freshly minted per-test tenant must start with an empty "
                "relevance_log"
            )

            db.telemetry.import_relevance_row(
                query="v0x32-real-engine-q1",
                chunk_id="3f6fc7dab5e64271e7d7878af4c51d1d3752219f18c11819cb48ec2c61131a80",
                collection="knowledge__x",
                action="click",
                session_id="s",
                timestamp="2020-01-01T00:00:00Z",
            )
            db.telemetry.log_relevance(
                "v0x32-real-engine-q2",
                "b0f2f39241ba9ea53467b2896a06c230d0af376bc0c46514f3fff71b73005dfd",
                "click",
            )

            after = db.telemetry.get_relevance_stats()
            assert after["count"] == 2
            assert after["oldest"] == "2020-01-01T00:00:00Z"
            # log_relevance stamps "now" server-side; just prove it is the
            # NEWER of the two, not a fixed literal (avoids a flaky exact-ts
            # assertion against real wall-clock time).
            assert after["newest"] >= after["oldest"]
        finally:
            db.close()
