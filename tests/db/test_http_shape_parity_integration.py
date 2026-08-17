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

import pytest

from nexus.db.t1 import T1Database

# Reused module-scoped fixtures: hermetic PG + shaded-jar service.
from tests.db._service_fixture import mint_session
from tests.db.test_http_memory_store_integration import (  # noqa: F401, PLC2701 — pytest resolves imported fixtures by name
    pg_instance,
    service,
)
from tests.conftest import make_vector_test_client

pytestmark = pytest.mark.integration


#: THE FROZEN ORACLE (nexus-1hufi). These parity tests compared the HTTP
#: store's row keys against the LOCAL SQLite store's. RDR-158 P4 deleted the
#: SQLite side, so the comparison lost its reference implementation and the
#: old ``sqlite_db`` fixture — which pinned ``NX_STORAGE_BACKEND=sqlite`` —
#: now raises ``StorageModeFlagError`` and ERRORs three tests.
#:
#: The contract is preserved by materialising the oracle instead of running
#: it. Each tuple below is the deleted store's own column list, recovered
#: verbatim from git history, NOT a snapshot of what the service returns
#: today — that distinction is the whole point. Freezing today's output
#: would bless any shape regression already present; freezing the deleted
#: reference keeps the original assertion ("the HTTP row has exactly the
#: keys the local row had") enforceable with the local row gone.
#:
#: Provenance, re-derivable:
#:   memory      _COLUMNS,       src/nexus/db/t2/memory_store.py     @ dbf67ed1^
#:   consents    list_consents,  src/nexus/db/t2/telemetry.py        @ 514253aa^
#:   relevance   get_relevance_log SELECT, same file/commit
#:   topics      _TOPIC_COLUMNS, src/nexus/db/t2/catalog_taxonomy.py @ f24bdb85^
#:
#: A key added to the service without being added here FAILS, which is the
#: behaviour the pair had before P4. Changing a tuple is a deliberate
#: contract change and belongs in the commit that changes the shape.
_ORACLE_MEMORY_ROW: frozenset[str] = frozenset({
    "id", "project", "title", "session", "agent", "content",
    "tags", "timestamp", "ttl", "access_count", "last_accessed",
})
_ORACLE_CONSENT_ROW: frozenset[str] = frozenset({"scope", "ts", "granted"})
_ORACLE_RELEVANCE_ROW: frozenset[str] = frozenset({
    "id", "query", "chunk_id", "collection", "action", "session_id", "timestamp",
})
_ORACLE_TOPIC_ROW: frozenset[str] = frozenset({
    "id", "label", "parent_id", "collection", "centroid_hash",
    "doc_count", "created_at", "review_status", "terms",
})


def _assert_shape(http_row: dict, oracle: frozenset[str], allow: frozenset[str], what: str):
    """Assert an HTTP row carries exactly the deleted local store's keys."""
    rk = _keys(http_row)
    unexplained = (rk ^ oracle) - allow
    assert unexplained == set(), (
        f"{what} live-service shape divergence from the frozen pre-P4 oracle "
        f"beyond the allowlist {sorted(allow)}: "
        f"only-oracle={oracle - rk} only-http={rk - oracle}"
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


def test_memory_get_and_search_shape_parity_live(service, _token_env):
    from nexus.db.t2.http_memory_store import HttpMemoryStore

    base_url, _token, _ = service
    http = HttpMemoryStore(base_url=base_url, tenant="default")
    try:
        http.put("shape-live", "e1", "live parity probe content", tags="a,b", ttl=30)
        _assert_shape(
            http.get(project="shape-live", title="e1"),
            _ORACLE_MEMORY_ROW, _MEMORY_ALLOW, "memory.get",
        )
        r_rows = http.search("parity", project="shape-live")
        assert r_rows
        _assert_shape(r_rows[0], _ORACLE_MEMORY_ROW, _MEMORY_ALLOW, "memory.search")
    finally:
        http.close()


# ── scratch (T1) ──────────────────────────────────────────────────────────────

#: Same frozen divergences as the fake-layer test: Chroma search rows carry a
#: cosine ``distance`` (no Postgres-FTS equivalent); service rows carry ``ts``.
_SCRATCH_ALLOW = frozenset({"distance", "ts"})


def test_scratch_search_shape_parity_live(service, _token_env):
    from nexus.db.http_scratch_store import HttpScratchStore

    base_url, token, _ = service
    t1 = T1Database(session_id="shape-live", client=make_vector_test_client())
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


def test_telemetry_consents_and_relevance_shape_parity_live(service, _token_env):
    from nexus.db.t2.http_telemetry_store import HttpTelemetryStore

    base_url, _token, _ = service
    http = HttpTelemetryStore(base_url=base_url, tenant="default")
    try:
        http.record_consent(
            scope="remediate:chash-poison",
            ts="2026-07-13T00:00:00Z", granted=True,
        )
        r_rows = http.list_consents()
        assert r_rows
        _assert_shape(
            r_rows[0], _ORACLE_CONSENT_ROW, _TELEMETRY_ALLOW,
            "telemetry.list_consents",
        )

        from tests._t2_fixture_ops import canonical_chunk_id

        http.log_relevance(
            "shape probe", canonical_chunk_id("chunk-d1"), "click",
            session_id="shape-live", collection="knowledge__shape",
        )
        r_rows = http.get_relevance_log(limit=1)
        assert r_rows
        _assert_shape(
            r_rows[0], _ORACLE_RELEVANCE_ROW, _TELEMETRY_ALLOW,
            "telemetry.get_relevance_log",
        )
    finally:
        http.close()


# ── taxonomy ──────────────────────────────────────────────────────────────────

_TAXONOMY_ALLOW: frozenset[str] = frozenset()


def test_taxonomy_topics_shape_parity_live(service, _token_env):
    from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore

    base_url, _token, _ = service
    http = HttpTaxonomyStore(base_url=base_url, tenant="default")
    try:
        # Seeded through the HTTP store's own real write path. The local
        # CatalogTaxonomy had no import_topic (that verb exists only on the
        # HTTP twin for cross-store migration), and an earlier version of
        # this test called import_topic on "both" sides — passing only
        # because the un-pinned sqlite_db was secretly service-backed too.
        # That hazard is gone with the local side; the frozen oracle above
        # is what keeps the assertion honest now.
        http.import_topic(
            src_id=1, label="shape-parity-topic", parent_id=None,
            collection="knowledge__shape", centroid_hash="abc",
            doc_count=1, created_at="2026-07-13T00:00:00Z",
            review_status="pending", terms="shape,parity",
        )
        r_rows = http.get_topics()
        assert r_rows
        _assert_shape(
            r_rows[0], _ORACLE_TOPIC_ROW, _TAXONOMY_ALLOW, "taxonomy.get_topics",
        )
    finally:
        http.close()
