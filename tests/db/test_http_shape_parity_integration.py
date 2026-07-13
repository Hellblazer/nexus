# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-w2q0s phase 2: return-shape parity against the REAL Java service.

The fake-server shape tests (test_t2_return_shape_parity.py) defend the
client-vs-SQLite boundary, but the fakes and the clients could drift from the
real Java handlers TOGETHER — the mechanism that actually produced the
search_cmd ``distance`` KeyError incident. This suite runs the same
operations through the SQLite/Chroma implementation AND the Http* client
against a LIVE self-provisioned service, asserting dict key-set parity with
the same frozen allowlists.

Marked ``-m integration``: skips automatically when the shaded jar or PG
binaries are absent (same fixtures as test_http_memory_store_integration —
imported, not duplicated).
"""
from __future__ import annotations

import chromadb
import pytest

from nexus.db.t1 import T1Database
from nexus.db.t2 import T2Database

# Reused module-scoped fixtures: hermetic PG + shaded-jar service.
from tests.db._service_fixture import mint_session
from tests.db.test_http_memory_store_integration import (  # noqa: F401, PLC2701 — pytest resolves imported fixtures by name
    pg_instance,
    service,
)

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def sqlite_db(tmp_path_factory):
    return T2Database(
        tmp_path_factory.mktemp("shape") / "t2.db", run_migrations=True,
    )


@pytest.fixture()
def _token_env(service, monkeypatch):
    _base_url, token, _ = service
    monkeypatch.setenv("NX_SERVICE_TOKEN", token)


def _keys(row: dict) -> set[str]:
    return set(row.keys())


def _assert_parity(local_row: dict, http_row: dict, allow: frozenset[str], what: str):
    lk, rk = _keys(local_row), _keys(http_row)
    unexplained = (lk ^ rk) - allow
    assert unexplained == set(), (
        f"{what} live-service shape divergence beyond the frozen allowlist "
        f"{sorted(allow)}: only-local={lk - rk} only-http={rk - lk}"
    )


# ── memory ────────────────────────────────────────────────────────────────────

_MEMORY_ALLOW: frozenset[str] = frozenset()


def test_memory_get_and_search_shape_parity_live(service, sqlite_db, _token_env):
    from nexus.db.t2.http_memory_store import HttpMemoryStore

    base_url, _token, _ = service
    http = HttpMemoryStore(base_url=base_url, tenant="default")
    try:
        for s in (sqlite_db.memory, http):
            s.put("shape-live", "e1", "live parity probe content", tags="a,b", ttl=30)
        _assert_parity(
            sqlite_db.memory.get(project="shape-live", title="e1"),
            http.get(project="shape-live", title="e1"),
            _MEMORY_ALLOW, "memory.get",
        )
        l_rows = sqlite_db.memory.search("parity", project="shape-live")
        r_rows = http.search("parity", project="shape-live")
        assert l_rows and r_rows
        _assert_parity(l_rows[0], r_rows[0], _MEMORY_ALLOW, "memory.search")
    finally:
        http.close()


# ── scratch (T1) ──────────────────────────────────────────────────────────────

#: Same frozen divergences as the fake-layer test: Chroma search rows carry a
#: cosine ``distance`` (no Postgres-FTS equivalent); service rows carry ``ts``.
_SCRATCH_ALLOW = frozenset({"distance", "ts"})


def test_scratch_search_shape_parity_live(service, _token_env):
    from nexus.db.http_scratch_store import HttpScratchStore

    base_url, token, _ = service
    t1 = T1Database(session_id="shape-live", client=chromadb.EphemeralClient())
    # Phase E require-minted: an unminted X-Nexus-T1-Session 401s — mint like
    # the MCP session lifespan does (mirrors test_http_scratch_store_integration).
    session_token = mint_session(base_url, token, "shape-live")
    http = HttpScratchStore(
        base_url=base_url, tenant="default", session_id="shape-live",
        _token=token, _session_token=session_token,
    )
    try:
        t1.put(content="live scratch parity probe", tags="t")
        http.put(content="live scratch parity probe", tags="t")
        l_rows = t1.search("parity")
        r_rows = http.search("parity")
        assert l_rows and r_rows
        _assert_parity(l_rows[0], r_rows[0], _SCRATCH_ALLOW, "scratch.search")
    finally:
        http.close()


# ── telemetry ─────────────────────────────────────────────────────────────────

_TELEMETRY_ALLOW: frozenset[str] = frozenset()


def test_telemetry_consents_and_relevance_shape_parity_live(
    service, sqlite_db, _token_env,
):
    from nexus.db.t2.http_telemetry_store import HttpTelemetryStore

    base_url, _token, _ = service
    http = HttpTelemetryStore(base_url=base_url, tenant="default")
    try:
        for s in (sqlite_db.telemetry, http):
            s.record_consent(
                scope="remediate:chash-poison",
                ts="2026-07-13T00:00:00Z", granted=True,
            )
        l_rows = sqlite_db.telemetry.list_consents()
        r_rows = http.list_consents()
        assert l_rows and r_rows
        _assert_parity(l_rows[0], r_rows[0], _TELEMETRY_ALLOW, "telemetry.list_consents")

        for s in (sqlite_db.telemetry, http):
            s.log_relevance(
                "shape probe", "chunk-d1", "click",
                session_id="shape-live", collection="knowledge__shape",
            )
        l_rows = sqlite_db.telemetry.get_relevance_log(limit=1)
        r_rows = http.get_relevance_log(limit=1)
        assert l_rows and r_rows
        _assert_parity(
            l_rows[0], r_rows[0], _TELEMETRY_ALLOW, "telemetry.get_relevance_log",
        )
    finally:
        http.close()


# ── taxonomy ──────────────────────────────────────────────────────────────────

_TAXONOMY_ALLOW: frozenset[str] = frozenset()


def test_taxonomy_topics_shape_parity_live(service, sqlite_db, _token_env):
    from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore

    base_url, _token, _ = service
    http = HttpTaxonomyStore(base_url=base_url, tenant="default")
    try:
        topic = dict(
            src_id=1, label="shape-parity-topic", parent_id=None,
            collection="knowledge__shape", centroid_hash="abc",
            doc_count=1, created_at="2026-07-13T00:00:00Z",
            review_status="pending", terms="shape,parity",
        )
        for s in (sqlite_db.taxonomy, http):
            s.import_topic(**topic)
        l_rows = sqlite_db.taxonomy.get_topics()
        r_rows = http.get_topics()
        assert l_rows and r_rows
        _assert_parity(l_rows[0], r_rows[0], _TAXONOMY_ALLOW, "taxonomy.get_topics")
    finally:
        http.close()
