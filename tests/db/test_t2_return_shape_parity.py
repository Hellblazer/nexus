# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-w2q0s: return-SHAPE (dict key-set) parity between store pairs.

The static parity tripwire (test_http_t2_store_parity.py) guards method
signatures only; the search_cmd ``r['distance']`` KeyError incident proved a
key present in one backend's rows and absent in the other survives it. These
tests run the SAME operations through both implementations of a pair —
SQLite/Chroma vs the Http* client against its faithful fake server — and
assert the returned dict key-sets match, modulo an EXPLICIT, frozen
divergence allowlist. A new divergence fails loudly; shrinking an allowlist
entry is a welcome breaking change (delete the entry when the shapes align).

The fake servers mirror the real Java handlers field-for-field (their module
docstrings carry that contract), so this is client+contract-level parity;
the live-service leg belongs to ``-m integration``.
"""
from __future__ import annotations

import socket
import threading
from http.server import HTTPServer

import pytest

from nexus.db.t1 import T1Database
from nexus.db.t2 import T2Database
from nexus.db.http_scratch_store import HttpScratchStore
from nexus.db.t2.http_memory_store import HttpMemoryStore

from tests.db import test_http_memory_store as fake_mem
from tests.db import test_http_scratch_store as fake_scratch
from tests.conftest import make_vector_test_client


def _serve(handler_cls) -> tuple[HTTPServer, str]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = HTTPServer(("127.0.0.1", port), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{port}"


# ── Memory pair: SQLite MemoryStore vs HttpMemoryStore ───────────────────────

#: Frozen, deliberate shape divergences for memory rows. EMPTY = full parity
#: is the contract; any key appearing on one side only is a regression.
_MEMORY_ROW_DIVERGENCES: frozenset[str] = frozenset()


@pytest.fixture()
def memory_pair(tmp_path):
    sqlite_store = T2Database(tmp_path / "t2.db", run_migrations=True).memory
    with fake_mem._STORE_LOCK:
        fake_mem._STORE.clear()
        fake_mem._ID_SEQ[0] = 1
    server, url = _serve(fake_mem._FakeMemoryHandler)
    http_store = HttpMemoryStore(base_url=url, _token=fake_mem.TOKEN)
    yield sqlite_store, http_store
    http_store.close()
    server.shutdown()


def _row_keys(row: dict) -> set[str]:
    return set(row.keys())


def test_memory_get_row_shape_parity(memory_pair):
    sqlite_store, http_store = memory_pair
    for s in (sqlite_store, http_store):
        s.put("shape", "e1", "parity probe content", tags="a,b", ttl=30)
    lk = _row_keys(sqlite_store.get(project="shape", title="e1"))
    rk = _row_keys(http_store.get(project="shape", title="e1"))
    assert lk - rk - _MEMORY_ROW_DIVERGENCES == set(), (
        f"keys only in SQLite get(): {lk - rk}"
    )
    assert rk - lk - _MEMORY_ROW_DIVERGENCES == set(), (
        f"keys only in Http get(): {rk - lk}"
    )


def test_memory_search_row_shape_parity(memory_pair):
    sqlite_store, http_store = memory_pair
    for s in (sqlite_store, http_store):
        s.put("shape", "e2", "searchable parity probe", ttl=30)
    l_rows = sqlite_store.search("parity", project="shape")
    r_rows = http_store.search("parity", project="shape")
    assert l_rows and r_rows, "both sides must return the seeded row"
    lk, rk = _row_keys(l_rows[0]), _row_keys(r_rows[0])
    assert (lk ^ rk) - _MEMORY_ROW_DIVERGENCES == set(), (
        f"search row shapes diverge: only-sqlite={lk - rk} only-http={rk - lk}"
    )


# ── Scratch pair: Chroma T1Database vs HttpScratchStore ──────────────────────

#: The search_cmd KeyError incident, frozen: Chroma search rows carry a
#: cosine ``distance``; the Postgres FTS path has no comparable score and
#: never returns one. Callers must .get() it. Delete this entry if/when the
#: service grows a score field — until then any OTHER divergence fails.
_SCRATCH_SEARCH_DIVERGENCES: frozenset[str] = frozenset({"distance"})
#: Found by this test's first run (2026-07-13): service rows carry the row
#: timestamp ``ts``; the Chroma path never surfaces one. ADDITIVE (extra key,
#: not a missing one), so lower-risk than the distance class — frozen here so
#: any FURTHER drift still fails.
_SCRATCH_ROW_DIVERGENCES: frozenset[str] = frozenset({"ts"})


@pytest.fixture()
def scratch_pair():
    t1 = T1Database(session_id="shape-parity", client=make_vector_test_client())
    with fake_scratch._STORE_LOCK:
        fake_scratch._STORE.clear()
    server, url = _serve(fake_scratch._FakeScratchHandler)
    http = HttpScratchStore(
        base_url=url, _token=fake_scratch.TOKEN, session_id="shape-parity",
    )
    yield t1, http
    http.close()
    server.shutdown()


def test_scratch_search_row_shape_parity(scratch_pair):
    t1, http = scratch_pair
    t1.put(content="scratch parity probe", tags="t")
    http.put(content="scratch parity probe", tags="t")
    l_rows = t1.search("parity")
    r_rows = http.search("parity")
    assert l_rows and r_rows, "both sides must return the seeded row"
    lk, rk = set(l_rows[0].keys()), set(r_rows[0].keys())
    unexplained = (lk ^ rk) - _SCRATCH_SEARCH_DIVERGENCES - _SCRATCH_ROW_DIVERGENCES
    assert unexplained == set(), (
        f"NEW scratch search-shape divergence (beyond the frozen 'distance' "
        f"allowlist): only-chroma={lk - rk} only-http={rk - lk}"
    )
