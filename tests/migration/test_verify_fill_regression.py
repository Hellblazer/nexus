# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-178 wave-2 verify-fill P6 (nexus-s3dd4.7): fault-injection regression
+ composed acceptance.

This IS the load-bearing anti-regression suite the whole wave exists for —
the 2026-07-01 incident where a 270-row hole in a 138,327-row catalog
manifest was patched by re-sending ~158,000 rows because ``migrate`` had no
delta mode. Every scenario here follows the same shape: migrate a table
FULLY into a fake target, PUNCH a small hole (K of N rows), run verify-fill,
and assert (a) ``filled == K`` (never ``N``), (b) the import/upsert spy
received exactly K rows TOTAL across every call it made, (c) the post-fill
outer verdict reads as a pass (``parity`` / ``verified`` — the vocabulary
each surface exposes), and (d) a SECOND verify-fill pass is a TRUE no-op:
``filled == 0`` and the spy receives ZERO further rows.

Non-tautology requirement (worked out via sequential-thinking before
writing this module): a *static* canned identity-set/count fake cannot
actually prove (d) — replayed against the SAME canned "K missing" snapshot,
a second pass would blindly re-send the same K rows again and the test
would still pass, silently inverting the very regression it claims to
guard. Every fake below is instead STATEFUL: the import/upsert call
mutates the exact same dict/set the identity source and count source read
from, so a second pass genuinely observes post-fill convergence. This is
the only way "second pass filled == 0" is evidence of anything rather than
a tautology.

Four surfaces + one composed CLI case, per the bead:

1. ``TestChashRegressionFaultInjection`` — the chash relational path
   (:func:`nexus.migration.orchestrator.verify_fill_chash`), N=200/K=3.
2. ``TestVectorsLegFaultInjection`` — the vectors leg
   (:func:`nexus.migration.vector_etl.verify_fill_collections` against a
   stateful ``FakeVectorClient``, already self-mutating via
   ``upsert_chunks``/``existing_ids``), N=200/K=3.
3. ``TestCatalogDocumentChunksFaultInjection`` — the catalog
   ``document_chunks`` path (the ACTUAL 2026-07-01 incident shape, at
   fixture scale: K=3 of N=200 instead of 270-of-138k), via
   :func:`nexus.migration.orchestrator.verify_fill_catalog`.
4. ``TestTelemetryFaultInjection`` — the telemetry per-table delta path
   (:func:`nexus.migration.orchestrator.verify_fill_telemetry`), the
   residual disclosed at the original P6 landing (2026-07-02, see below):
   a MAPPED table (``hook_failures``, N=200/K=3), an UNMAPPED table
   (``frecency``, N=200/K=3 — pinning that an unmapped table still gets
   genuine delta fill, never a full re-send), and a multi-column
   conflict-key fidelity case (``search_telemetry``) proving the diff
   identifies a hole by its FULL conflict-key tuple, not a partial-column
   match.
5. ``TestComposedCliFaultInjection`` — one CLI-level case
   (``nx storage migrate chash --verify-fill``, driven twice through the
   same stateful fakes) proving the wiring end-to-end, not just the
   library functions.

Explicitly OUT of scope (residual scope on the parent bead nexus-s3dd4,
NOT this module): the ``tests/e2e/migration-rehearsal`` hole-punch
journey (cloud-gated, needs a deployed engine — separate follow-up). This
is now the ONLY residual — telemetry's fault-injection coverage (the gap
disclosed at the original P6 landing, 2026-07-02: P3b/nexus-s3dd4.14
wired the real per-table delta-fill path but the STATEFUL,
self-mutating-fake fault-injection style this module uses had not been
extended to it) is closed by ``TestTelemetryFaultInjection`` above.

Also out of scope BY DESIGN (r6b critic, 2026-07-02): breaker/502-burst
fault injection is NOT re-instantiated per surface here — the retry arc
is owned by test_rdr178_gap3_circuit_breaker.py (the 502-burst survival
regression) and test_verify_fill_wiring.py's breaker-give-up recovery
test, and every surface's inner fill (chash/catalog/telemetry) shares
the identical fill_missing + _etl_batch_with_breaker plumbing, so one
instance covers the shared path. Same practice as chash/catalog/vectors.

Also out of scope (P6 critic, 2026-07-02): the 300-row pagination
boundary of the identity-fetch surfaces. The fakes here answer presence
from in-memory dicts with no limit/offset walk, so an off-by-one at a
page boundary in the REAL clients (registered_chashes_for_collection,
existing_ids, chashes_for_collection) is invisible at any N — bumping
N past 300 would not make these tests exercise it. Real-client paging
belongs to the client's own tests + the cloud e2e journey above; the
2026-07-01 incident's page-count dimension (270 of 138k, many pages)
is therefore reproduced here in hole/table RATIO only.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import chromadb
import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.db.t2.telemetry_etl import conflict_key
from nexus.migration.orchestrator import (
    verify_fill_catalog,
    verify_fill_chash,
    verify_fill_telemetry,
)
from nexus.migration.vector_etl import migrate_collections, verify_fill_collections

# Reuse the locked copy-not-move ETL fakes + seeding helpers (single source of
# truth; mirrors tests/migration/test_e2e_oracle.py's own precedent for
# cross-file test-fake reuse — a drift in FakeVectorClient's surface trips
# both suites).
from tests.migration.test_vector_etl import (  # noqa: PLC2701 — shared test fakes
    FakeVectorClient,
    _coll,
    _seed_source,
)

# Reuse the locked 6-table telemetry seeding helper (single source of truth;
# mirrors test_verify_fill_wiring.py's own precedent for cross-file test-fake
# reuse — a drift in the schema trips both suites).
from tests.db.test_telemetry_etl import _seed_full_telemetry_db  # noqa: PLC2701 — shared test fixture

_N = 200
_K = 3


# ═══════════════════════════════════════════════════════════════════════════
# 1. chash relational path
# ═══════════════════════════════════════════════════════════════════════════


def _seed_chash_db(db_path: Path, rows: list[tuple[str, str]]) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE chash_index (chash TEXT, physical_collection TEXT, created_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO chash_index VALUES (?, ?, '2026-01-01T00:00:00Z')", rows,
    )
    conn.commit()
    conn.close()


class _StatefulChashTarget:
    """Single stateful object serving THREE roles against the SAME
    ``registered`` dict: the chash identity source
    (``registered_chashes_for_collection``), the import target
    (``import_rows`` mutates ``registered`` on receipt — mirroring what
    the real service does), and the outer count source (``counts``,
    derived from the same dict). This is the load-bearing design choice
    that makes "second pass is a true no-op" a real assertion instead of a
    tautology against a static canned snapshot.

    nexus-f2qvx.3: ``_chash_import_fn`` (orchestrator.py) used to call
    ``http_chash._client.post("/v1/chash/import", ...)`` directly — a
    pre-mixin-adoption reach-through into the store's internal transport
    that no longer works post-adoption (RefreshableHttpStoreMixin's
    httpx.Client has no baked base_url). It now calls the public
    ``HttpChashIndex.import_rows()`` wrapper instead, so this fake exposes
    ``import_rows`` rather than ``_client.post``.
    """

    def __init__(self, registered: dict[str, set[str]]) -> None:
        self.registered: dict[str, set[str]] = {k: set(v) for k, v in registered.items()}
        self.posts: list[tuple[str, dict[str, Any]]] = []

    def registered_chashes_for_collection(self, collection: str) -> set[str]:
        return set(self.registered.get(collection, set()))

    def import_rows(self, rows: list[dict[str, Any]]) -> int:
        self.posts.append(("/v1/chash/import", {"rows": rows}))
        for row in rows:
            self.registered.setdefault(row["collection"], set()).add(row["chash"])
        return len(rows)

    def close(self) -> None:
        pass

    # CountSource surface
    def counts(self, relations: list[str]) -> dict[str, int]:
        total = sum(len(s) for s in self.registered.values())
        return {r: total for r in relations}

    @property
    def total_rows_sent(self) -> int:
        return sum(len(payload.get("rows", [])) for _url, payload in self.posts)


class TestChashRegressionFaultInjection:
    def test_hole_of_k_filled_not_full_resend_then_second_pass_noop(
        self, tmp_path: Path,
    ) -> None:
        all_ids = [f"{i:032d}" for i in range(_N)]
        db = tmp_path / "t2.db"
        _seed_chash_db(db, [(chash, "code__x") for chash in all_ids])

        holed = set(all_ids[50 : 50 + _K])  # noqa: E203 — black-formatted slice
        target = _StatefulChashTarget({"code__x": set(all_ids) - holed})

        # ── pass 1: divergent -> fill exactly the hole ──────────────────
        result1 = verify_fill_chash(db, target, count_source=target)

        assert result1["outer"]["chash_index"]["status"] == "divergent"
        fill1 = result1["fill"]["code__x"]
        assert fill1["status"] == "filled"
        assert fill1["missing"] == _K
        assert fill1["filled"] == _K  # (a): K, never N
        assert result1["total_filled"] == _K

        # (b): the spy received EXACTLY K rows total, not the 200-row table
        assert target.total_rows_sent == _K
        sent_chashes = {
            row["chash"] for _url, payload in target.posts for row in payload["rows"]
        }
        assert sent_chashes == holed

        # (c): the hole is now genuinely closed in the (stateful) target
        assert target.registered["code__x"] == set(all_ids)

        # ── pass 2: true no-op ───────────────────────────────────────────
        result2 = verify_fill_chash(db, target, count_source=target)

        assert result2["outer"]["chash_index"]["status"] == "parity"  # (c)
        assert result2["fill"] == {}  # inner loop skipped entirely on parity
        assert result2["total_filled"] == 0  # (d)
        # (d): the spy received ZERO further rows — posts list unchanged
        assert target.total_rows_sent == _K
        assert len(target.posts) == 1  # still just the one batch from pass 1


# ═══════════════════════════════════════════════════════════════════════════
# 2. vectors leg
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture()
def source_client():
    c = chromadb.EphemeralClient()
    # EphemeralClient shares one in-process backend — clear leftovers.
    for col in c.list_collections():
        c.delete_collection(col.name)
    return c


class TestVectorsLegFaultInjection:
    def test_hole_of_k_filled_not_full_resend_then_second_pass_noop(
        self, source_client,
    ) -> None:
        name = _coll("vf-regression")
        ids = _seed_source(source_client, name, _N)
        fake = FakeVectorClient()
        migrate_collections(source_client, fake, leg="local")  # fully populate target

        holed = ids[50 : 50 + _K]  # noqa: E203
        for missing_id in holed:
            fake.store[name].pop(missing_id, None)
        fake.upsert_calls.clear()

        # ── pass 1: partial delta -> fill exactly the hole ──────────────
        report1 = verify_fill_collections(source_client, fake, leg="local")

        result1 = report1.results[0]
        assert result1.status == "filled"
        assert result1.source_count == _N
        assert result1.missing_count == _K
        assert result1.filled_count == _K  # (a): K, never N
        assert result1.written_count == _K
        assert report1.ok is True
        assert report1.total_written == _K

        # (b): the spy received EXACTLY K ids total, not the 200-row collection
        sent_ids = {i for _coll_name, batch in fake.upsert_calls for i in batch}
        assert sent_ids == set(holed)
        assert sum(len(batch) for _c, batch in fake.upsert_calls) == _K

        # (c): the target genuinely holds all N now
        assert fake.count(name) == _N

        # ── pass 2: true no-op ───────────────────────────────────────────
        fake.upsert_calls.clear()
        report2 = verify_fill_collections(source_client, fake, leg="local")

        result2 = report2.results[0]
        assert result2.status == "verified"  # (c) surface's pass vocabulary
        assert result2.missing_count == 0
        assert result2.filled_count == 0  # (d)
        assert report2.ok is True
        assert report2.total_written == 0
        assert fake.upsert_calls == []  # (d): zero further rows sent


# ═══════════════════════════════════════════════════════════════════════════
# 3. catalog document_chunks path (the actual 2026-07-01 incident shape)
# ═══════════════════════════════════════════════════════════════════════════

_DOC_TUMBLER = "1.1"
_DOC_COLLECTION = "code__x"


def _seed_catalog_db(catalog_db: Path, *, n_chunks: int) -> None:
    conn = sqlite3.connect(str(catalog_db))
    conn.execute(
        "CREATE TABLE owners (tumbler_prefix TEXT, name TEXT, owner_type TEXT, "
        "repo_hash TEXT, description TEXT, repo_root TEXT, head_hash TEXT)"
    )
    conn.execute(
        "CREATE TABLE documents (tumbler TEXT, title TEXT, author TEXT, year INT, "
        "content_type TEXT, file_path TEXT, corpus TEXT, physical_collection TEXT, "
        "chunk_count INT, head_hash TEXT, indexed_at TEXT, metadata TEXT, "
        "source_mtime REAL, alias_of TEXT, source_uri TEXT, bib_year INT, "
        "bib_authors TEXT, bib_venue TEXT, bib_citation_count INT, "
        "bib_semantic_scholar_id TEXT, bib_openalex_id TEXT, bib_doi TEXT, "
        "bib_enriched_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE links (id INTEGER PRIMARY KEY, from_tumbler TEXT, "
        "to_tumbler TEXT, link_type TEXT, from_span TEXT, to_span TEXT, "
        "created_by TEXT, created_at TEXT, metadata TEXT)"
    )
    conn.execute(
        "CREATE TABLE collections (name TEXT, content_type TEXT, owner_id TEXT, "
        "embedding_model TEXT, model_version TEXT, display_name TEXT, "
        "legacy_grandfathered INT, superseded_by TEXT, superseded_at TEXT, "
        "created_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE document_chunks (doc_id TEXT, position INT, chash TEXT, "
        "chunk_index INT, line_start INT, line_end INT, char_start INT, char_end INT)"
    )
    conn.execute("CREATE TABLE _meta (key TEXT, value TEXT)")
    conn.execute(
        "INSERT INTO owners VALUES ('1', 'owner-1', 'user', '', '', '', '')"
    )
    conn.execute(
        "INSERT INTO documents VALUES (?, 'doc', '', 0, '', '', '', ?, ?, '', '', "
        "NULL, 0, '', '', 0, '', '', 0, '', '', '', '')",
        (_DOC_TUMBLER, _DOC_COLLECTION, n_chunks),
    )
    conn.execute(
        "INSERT INTO collections VALUES (?, 'code', '1', 'voyage', 'v1', '', 0, "
        "'', '', '')",
        (_DOC_COLLECTION,),
    )
    conn.executemany(
        "INSERT INTO document_chunks VALUES (?, ?, ?, 0, 0, 0, 0, 0)",
        [(_DOC_TUMBLER, i, f"{i:032d}") for i in range(n_chunks)],
    )
    conn.commit()
    conn.close()


class _StatefulCatalogChunkTarget:
    """Single stateful object for the ``document_chunks`` fill surface:
    ``chashes_for_collection`` (collection-level chash pre-filter),
    ``get_manifest`` (precise per-doc ``(position, chash)`` reconciliation
    — BOTH derived from the SAME ``manifest_by_doc`` dict), ``_post``
    (import target — mutates that same dict), and ``counts`` (outer count
    source, also derived from it). owners/documents/collections/links are
    pinned at PARITY via static counts so ONLY document_chunks triggers the
    delta path (never the full-ETL fallback).

    Tracking the FULL ``(doc_id, position) -> chash`` manifest (not just a
    flat chash set) matters here: ``fill_missing_document_chunks`` treats
    ANY row whose chash is present in the collection-level set as merely an
    AMBIGUOUS "candidate" (RDR-108 D1) and always cross-checks it against
    ``get_manifest`` — so a fake whose ``get_manifest`` doesn't mirror the
    same underlying state as the chash pre-filter would wrongly report
    every already-landed row as still missing (verified by running this
    fixture with a manifest-blind stub first: it inflated ``missing`` from
    3 to 200)."""

    def __init__(
        self, manifest_by_doc: dict[str, dict[int, str]], collection_for_doc: dict[str, str],
    ) -> None:
        self.manifest_by_doc: dict[str, dict[int, str]] = {
            doc: dict(m) for doc, m in manifest_by_doc.items()
        }
        self._collection_for_doc = collection_for_doc
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.manifest_calls: list[str] = []
        self._static = {
            "nexus.catalog_owners": 1,
            "nexus.catalog_documents": 1,
            "nexus.catalog_collections": 1,
            "nexus.catalog_links": 0,
        }

    def close(self) -> None:
        pass

    def chashes_for_collection(self, collection: str) -> set[str]:
        chashes: set[str] = set()
        for doc_id, coll in self._collection_for_doc.items():
            if coll == collection:
                chashes.update(self.manifest_by_doc.get(doc_id, {}).values())
        return chashes

    def get_manifest(self, doc_id: str) -> list[Any]:
        from nexus.catalog.catalog_writes import ManifestRow  # noqa: PLC0415 — real-shape fake, scoped

        self.manifest_calls.append(doc_id)
        return [
            ManifestRow(position=pos, chash=chash)
            for pos, chash in sorted(self.manifest_by_doc.get(doc_id, {}).items())
        ]

    def _post(self, path: str, payload: dict[str, Any]) -> None:
        self.posts.append((path, payload))
        if path == "/import/chunk":
            doc_id = payload["doc_id"]
            for row in payload["rows"]:
                self.manifest_by_doc.setdefault(doc_id, {})[row["position"]] = row["chash"]

    def counts(self, relations: list[str]) -> dict[str, int]:
        result: dict[str, int] = {}
        for r in relations:
            if r == "nexus.catalog_document_chunks":
                result[r] = sum(len(m) for m in self.manifest_by_doc.values())
            else:
                result[r] = self._static.get(r, 0)
        return result

    @property
    def total_chunk_rows_sent(self) -> int:
        return sum(
            len(payload["rows"])
            for path, payload in self.posts
            if path == "/import/chunk"
        )


class TestCatalogDocumentChunksFaultInjection:
    def test_hole_of_k_filled_not_full_resend_then_second_pass_noop(
        self, tmp_path: Path,
    ) -> None:
        catalog_db = tmp_path / ".catalog.db"
        _seed_catalog_db(catalog_db, n_chunks=_N)

        holed_positions = set(range(50, 50 + _K))
        all_chashes = {f"{i:032d}" for i in range(_N)}
        holed = {f"{i:032d}" for i in holed_positions}
        target = _StatefulCatalogChunkTarget(
            manifest_by_doc={
                _DOC_TUMBLER: {
                    i: f"{i:032d}" for i in range(_N) if i not in holed_positions
                },
            },
            collection_for_doc={_DOC_TUMBLER: _DOC_COLLECTION},
        )

        # ── pass 1: only document_chunks diverges -> fill exactly the hole ──
        result1 = verify_fill_catalog(catalog_db, target, count_source=target)

        assert "fallback" not in result1  # never the full-ETL escape hatch
        assert result1["outer"]["owners"]["status"] == "parity"
        assert result1["outer"]["documents"]["status"] == "parity"
        assert result1["outer"]["collections"]["status"] == "parity"
        assert result1["outer"]["links"]["status"] == "parity"
        assert result1["outer"]["document_chunks"]["status"] == "divergent"

        chunk_fill1 = result1["fill"]["document_chunks"]
        assert chunk_fill1["missing"] == _K
        assert chunk_fill1["filled"] == _K  # (a): K, never N=200
        assert chunk_fill1["indeterminate"] == 0
        assert result1["total_filled"] == _K
        assert "owners" not in result1["fill"]  # parity tables never fill-called
        assert "collections" not in result1["fill"]

        # (b): the spy received EXACTLY K rows total, not the 200-row table
        assert target.total_chunk_rows_sent == _K
        sent_chashes = {
            row["chash"]
            for path, payload in target.posts
            if path == "/import/chunk"
            for row in payload["rows"]
        }
        assert sent_chashes == holed
        # the 197 already-landed rows are AMBIGUOUS candidates (their chash
        # IS present in the collection-level set, per RDR-108 D1) -- exactly
        # one manifest_for("1.1") call resolves all of them at once; the 3
        # definitely-missing rows never needed it.
        assert target.manifest_calls == [_DOC_TUMBLER]

        # (c): the hole is now genuinely closed in the (stateful) target
        assert target.chashes_for_collection(_DOC_COLLECTION) == all_chashes
        assert set(target.manifest_by_doc[_DOC_TUMBLER].keys()) == set(range(_N))

        # ── pass 2: true no-op ───────────────────────────────────────────
        result2 = verify_fill_catalog(catalog_db, target, count_source=target)

        assert result2["outer"]["document_chunks"]["status"] == "parity"  # (c)
        assert "document_chunks" not in result2["fill"]  # fill call skipped entirely
        assert result2["total_filled"] == 0  # (d)
        assert target.total_chunk_rows_sent == _K  # (d): no further rows sent
        assert len(target.posts) == 1  # still just the one batch from pass 1


# ═══════════════════════════════════════════════════════════════════════════
# 4. telemetry (P6 residual close-out, nexus-s3dd4.7 comment 2026-07-02 14:29)
# ═══════════════════════════════════════════════════════════════════════════


class _StatefulTelemetryTarget:
    """Single stateful object serving THREE roles against the SAME
    ``present_by_table`` dict: the per-table identity source
    (``probe_ids``), the import target (``import_rows_batch`` mutates the
    same dict on receipt, keyed via the REAL ``conflict_key`` so a fault
    scenario's expectations and the fake's own state can never drift
    apart), and — via ``_LiveTelemetryCountSource`` below — the outer
    count source for the two ``_VERIFY_TABLES``-mapped tables
    (``hook_failures``, ``nx_answer_runs``). Same non-tautology discipline
    as ``_StatefulChashTarget``/``_StatefulCatalogChunkTarget`` above: a
    second pass genuinely re-probes post-fill state rather than replaying
    a canned snapshot."""

    base_url = "http://corpus-fake-service:0"  # watermark identity key (nexus-te885.10)

    def get_retention_markers(self, relations):
        """Marker-era engine, never swept: success + absent = baseline 0
        (nexus-24p05; the orchestrator normalizes absent-on-success to 0)."""
        return {}

    def __init__(self, present_by_table: dict[str, set[tuple[Any, ...]]]) -> None:
        self.present_by_table: dict[str, set[tuple[Any, ...]]] = {
            t: set(s) for t, s in present_by_table.items()
        }
        self.probe_calls: list[tuple[str, list[list[Any]]]] = []
        self.import_calls: list[tuple[str, list[dict[str, Any]]]] = []

    def probe_ids(self, table: str, keys: list[list[Any]]) -> list[list[Any]]:
        self.probe_calls.append((table, [list(k) for k in keys]))
        present = self.present_by_table.get(table, set())
        return [list(k) for k in keys if tuple(k) in present]

    def import_rows_batch(self, table: str, rows: list[dict[str, Any]]) -> int:
        self.import_calls.append((table, list(rows)))
        for row in rows:
            self.present_by_table.setdefault(table, set()).add(conflict_key(table, row))
        return len(rows)

    def close(self) -> None:
        pass

    @property
    def total_rows_sent(self) -> int:
        return sum(len(rows) for _t, rows in self.import_calls)


#: relation (as reported by ``_VERIFY_TABLES``) -> telemetry table name,
#: the inverse of the mapping ``verify_fill_telemetry`` uses internally.
_TELEMETRY_RELATION_TO_TABLE = {
    "nexus.hook_failures": "hook_failures",
    "nexus.nx_answer_runs": "nx_answer_runs",
}


class _LiveTelemetryCountSource:
    """Derives the two mapped-relation counts LIVE from the SAME
    ``present_by_table`` dict the stateful target mutates on import — a
    static/canned count would make a second pass indistinguishable from a
    re-send bug (module docstring's non-tautology requirement)."""

    def __init__(self, target: _StatefulTelemetryTarget) -> None:
        self._target = target

    def counts(self, relations: list[str]) -> dict[str, int]:
        return {
            r: len(
                self._target.present_by_table.get(
                    _TELEMETRY_RELATION_TO_TABLE.get(r, ""), set(),
                )
            )
            for r in relations
        }


class TestTelemetryFaultInjection:
    """P6 residual close-out (nexus-s3dd4.7 comment 2026-07-02 14:29): the
    original P6 landing extended the stateful K-of-N fault-injection style
    to chash/vectors/catalog but explicitly disclosed telemetry as
    out-of-scope. This class closes that gap for
    :func:`nexus.migration.orchestrator.verify_fill_telemetry`."""

    def test_hole_of_k_in_mapped_table_filled_not_full_resend_then_second_pass_noop(
        self, tmp_path: Path,
    ) -> None:
        """hook_failures IS ``_VERIFY_TABLES``-mapped (``nexus.hook_failures``)
        — the outer count-diff goes divergent, and the inner probe/fill
        loop must send exactly the K-row hole, never the N-row table."""
        db = tmp_path / "t2.db"
        hooks = [
            {
                "doc_id": f"d{i:04d}", "hook_name": "h1",
                "occurred_at": "2024-01-01T00:00:00+00:00",  # already-canonical: no _normalize_timestamp drift
            }
            for i in range(_N)
        ]
        _seed_full_telemetry_db(db, hooks=hooks)

        all_keys = {conflict_key("hook_failures", r) for r in hooks}
        holed_rows = hooks[50 : 50 + _K]  # noqa: E203
        holed_keys = {conflict_key("hook_failures", r) for r in holed_rows}
        target = _StatefulTelemetryTarget({"hook_failures": all_keys - holed_keys})
        count_source = _LiveTelemetryCountSource(target)

        # ── pass 1: divergent -> fill exactly the hole ──────────────────
        result1 = verify_fill_telemetry(db, target, count_source=count_source)

        assert "fallback" not in result1  # never the store-wide full-ETL escape hatch
        assert result1["outer"]["hook_failures"]["status"] == "divergent"
        fill1 = result1["fill"]["hook_failures"]
        assert fill1["missing"] == _K
        assert fill1["filled"] == _K  # (a): K, never N
        assert result1["total_filled"] == _K

        # (b): the spy received EXACTLY K rows total, not the 200-row table
        assert target.total_rows_sent == _K
        sent_keys = {
            conflict_key("hook_failures", row)
            for _t, rows in target.import_calls
            for row in rows
        }
        assert sent_keys == holed_keys

        # (c): the hole is now genuinely closed in the (stateful) target
        assert target.present_by_table["hook_failures"] == all_keys

        # ── pass 2: true no-op ───────────────────────────────────────────
        result2 = verify_fill_telemetry(db, target, count_source=count_source)

        assert result2["outer"]["hook_failures"]["status"] == "parity"  # (c)
        assert "hook_failures" not in result2["fill"]  # skip-on-parity, zero probe at all
        assert result2["total_filled"] == 0  # (d)
        assert target.total_rows_sent == _K  # (d): no further rows sent
        assert len(target.import_calls) == 1  # still just the one batch from pass 1

    def test_hole_of_k_in_unmapped_table_filled_not_full_resend_then_second_pass_noop(
        self, tmp_path: Path,
    ) -> None:
        """frecency has NO ``_VERIFY_TABLES`` relation — its outer verdict
        is ALWAYS ``indeterminate``, by design (nexus-s3dd4.14 design
        decision 1). This pins that "always indeterminate" does NOT mean
        "always full re-send": ``probe_ids`` still delta-fills exactly the
        K-row hole out of N, every pass, on the strength of the identity
        probe alone."""
        db = tmp_path / "t2.db"
        frecency_rows = [{"chunk_id": f"fc{i:04d}"} for i in range(_N)]
        _seed_full_telemetry_db(db, frecency=frecency_rows)

        all_keys = {conflict_key("frecency", r) for r in frecency_rows}
        holed_rows = frecency_rows[50 : 50 + _K]  # noqa: E203
        holed_keys = {conflict_key("frecency", r) for r in holed_rows}
        target = _StatefulTelemetryTarget({"frecency": all_keys - holed_keys})
        count_source = _LiveTelemetryCountSource(target)

        # ── pass 1: indeterminate outer, but the probe finds exactly K missing ──
        result1 = verify_fill_telemetry(db, target, count_source=count_source)

        assert "fallback" not in result1
        assert result1["outer"]["frecency"]["status"] == "indeterminate"  # unmapped, always
        fill1 = result1["fill"]["frecency"]
        assert fill1["missing"] == _K
        assert fill1["filled"] == _K  # (a): K, never N=200
        assert result1["total_filled"] == _K

        # (b): the spy received EXACTLY K rows total, not the 200-row table
        assert target.total_rows_sent == _K
        sent_keys = {
            conflict_key("frecency", row)
            for _t, rows in target.import_calls
            for row in rows
        }
        assert sent_keys == holed_keys

        # (c): the hole is now genuinely closed in the (stateful) target
        assert target.present_by_table["frecency"] == all_keys

        # ── pass 2: true no-op (still probed every pass -- unmapped never
        # reaches outer "parity" -- but the identity diff finds nothing left) ──
        result2 = verify_fill_telemetry(db, target, count_source=count_source)

        fill2 = result2["fill"]["frecency"]
        assert fill2["missing"] == 0  # (c)
        assert fill2["filled"] == 0  # (d)
        assert result2["total_filled"] == 0
        assert target.total_rows_sent == _K  # (d): no further rows sent
        assert len(target.import_calls) == 1  # still just the one batch from pass 1

    def test_multi_column_conflict_key_identifies_holed_row_by_full_tuple(
        self, tmp_path: Path,
    ) -> None:
        """search_telemetry's conflict key is the 3-column tuple
        ``(ts, query_hash, collection)``. Four rows share pairwise columns
        with each other (same ``query_hash``+``collection`` as the holed
        row except for ``ts``; same ``ts`` except for ``query_hash``; same
        ``ts``+``query_hash`` except for ``collection``) -- if the fill
        diffed on any PARTIAL sub-tuple instead of the full key, it would
        either wrongly re-send an already-present row or wrongly skip the
        genuinely-missing one. Asserting exact row IDENTITY of what was
        sent is the load-bearing check here, not just a count."""
        db = tmp_path / "t2.db"
        present_row = {"ts": "2024-04-01T00:00:00Z", "query_hash": "qh1", "collection": "code__x"}
        holed_row = {"ts": "2024-04-02T00:00:00Z", "query_hash": "qh1", "collection": "code__x"}
        same_ts_diff_hash = {"ts": "2024-04-01T00:00:00Z", "query_hash": "qh2", "collection": "code__x"}
        same_ts_hash_diff_coll = {"ts": "2024-04-01T00:00:00Z", "query_hash": "qh1", "collection": "code__y"}
        rows = [present_row, holed_row, same_ts_diff_hash, same_ts_hash_diff_coll]
        _seed_full_telemetry_db(db, search=rows)

        present_keys = {
            conflict_key("search_telemetry", r)
            for r in (present_row, same_ts_diff_hash, same_ts_hash_diff_coll)
        }
        holed_key = conflict_key("search_telemetry", holed_row)
        target = _StatefulTelemetryTarget({"search_telemetry": present_keys})
        count_source = _LiveTelemetryCountSource(target)

        # ── pass 1: exactly the one holed (ts, query_hash, collection) tuple ──
        result1 = verify_fill_telemetry(db, target, count_source=count_source)

        assert result1["outer"]["search_telemetry"]["status"] == "indeterminate"  # unmapped
        fill1 = result1["fill"]["search_telemetry"]
        assert fill1["missing"] == 1
        assert fill1["filled"] == 1
        assert result1["total_filled"] == 1

        sent_keys = {
            conflict_key("search_telemetry", row)
            for _t, rows_sent in target.import_calls
            for row in rows_sent
        }
        assert sent_keys == {holed_key}  # full-tuple identity, not a partial-column match
        assert target.total_rows_sent == 1

        # ── pass 2: true no-op ───────────────────────────────────────────
        result2 = verify_fill_telemetry(db, target, count_source=count_source)

        fill2 = result2["fill"]["search_telemetry"]
        assert fill2["missing"] == 0
        assert fill2["filled"] == 0
        assert result2["total_filled"] == 0
        assert target.total_rows_sent == 1
        assert len(target.import_calls) == 1  # still just the one batch from pass 1


# ═══════════════════════════════════════════════════════════════════════════
# 5. composed CLI-level case
# ═══════════════════════════════════════════════════════════════════════════

_CLI_N = 50
_CLI_K = 3


def _make_stateful_fake_chash_store(
    registered: dict[str, set[str]], posts: list[tuple[str, dict[str, Any]]],
):
    """Factory mirroring test_verify_fill_cli.py's ``_make_fake_chash_store``
    closure pattern, but STATEFUL: ``import_rows`` mutates the same
    ``registered`` dict the identity surface reads from, so a second CLI
    invocation against the same closures observes genuine post-fill
    convergence.

    nexus-f2qvx.3: ``_chash_import_fn`` (orchestrator.py) now calls the
    public ``HttpChashIndex.import_rows()`` wrapper instead of reaching
    into ``http_chash._client.post(...)`` directly — see
    ``_StatefulChashTarget``'s docstring above for the full rationale.
    """

    class _FakeChashStore:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def registered_chashes_for_collection(self, collection: str) -> set[str]:
            return set(registered.get(collection, set()))

        def import_rows(self, rows: list[dict[str, Any]]) -> int:
            posts.append(("/v1/chash/import", {"rows": rows}))
            for row in rows:
                registered.setdefault(row["collection"], set()).add(row["chash"])
            return len(rows)

        def close(self) -> None:
            pass

    return _FakeChashStore


class _LiveCliCountSource:
    """A ``CountSource`` deriving the target count LIVE from the same
    ``registered`` dict the stateful chash store mutates — a static/canned
    count would make pass 2 indistinguishable from a re-send bug (see
    module docstring)."""

    def __init__(self, registered: dict[str, set[str]]) -> None:
        self._registered = registered

    def counts(self, relations: list[str]) -> dict[str, int]:
        total = sum(len(s) for s in self._registered.values())
        return {r: total for r in relations}


def _seed_chash_db_cli(db_path: Path, rows: list[tuple[str, str]]) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE chash_index (chash TEXT, physical_collection TEXT, created_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO chash_index (chash, physical_collection, created_at) "
        "VALUES (?, ?, '2026-01-01T00:00:00Z')",
        rows,
    )
    conn.commit()
    conn.close()


class TestComposedCliFaultInjection:
    def test_migrate_chash_verify_fill_twice_hole_then_noop(
        self, tmp_path: Path,
    ) -> None:
        from unittest.mock import patch  # noqa: PLC0415 — scoped to this test

        runner = CliRunner()
        all_ids = [f"{i:032d}" for i in range(_CLI_N)]
        db = tmp_path / "t2.db"
        _seed_chash_db_cli(db, [(chash, "code__x") for chash in all_ids])
        report_path = tmp_path / "report.json"

        holed = set(all_ids[10 : 10 + _CLI_K])  # noqa: E203
        registered: dict[str, set[str]] = {"code__x": set(all_ids) - holed}
        posts: list[tuple[str, dict[str, Any]]] = []

        common_patches = (
            patch(
                "nexus.db.t2.http_chash_index.HttpChashIndex",
                _make_stateful_fake_chash_store(registered, posts),
            ),
            patch(
                "nexus.migration.orchestrator.ServiceCountSource",
                lambda: _LiveCliCountSource(registered),
            ),
            patch.dict("os.environ", {"NX_SERVICE_TOKEN": "t"}),
        )

        cli_args = [
            "storage", "migrate", "chash",
            "--db", str(db),
            "--service-url", "http://fake-service:9",
            "--report", str(report_path),
            "--verify-fill",
        ]

        # ── pass 1: divergent -> fill exactly the hole ──────────────────
        with common_patches[0], common_patches[1], common_patches[2]:
            result1 = runner.invoke(main, cli_args)

        assert result1.exit_code == 0, result1.output
        assert "filled=3" in result1.output  # (a): K=3, never N=50
        assert "outer_status=divergent" in result1.output

        sent_chashes = {
            row["chash"] for _url, payload in posts for row in payload["rows"]
        }
        assert sent_chashes == holed  # (b): exactly the hole
        assert sum(len(payload["rows"]) for _u, payload in posts) == _CLI_K

        report1 = json.loads(report_path.read_text())
        assert report1["summary"]["total_written"] == _CLI_K

        # ── pass 2: true no-op ───────────────────────────────────────────
        with common_patches[0], common_patches[1], common_patches[2]:
            result2 = runner.invoke(main, cli_args)

        assert result2.exit_code == 0, result2.output
        assert "filled=0" in result2.output  # (d)
        assert "outer_status=parity" in result2.output  # (c)

        # (d): the spy received ZERO further rows across the second pass
        assert len(posts) == 1  # still just the one batch from pass 1
        assert sum(len(payload["rows"]) for _u, payload in posts) == _CLI_K

        report2 = json.loads(report_path.read_text())
        assert report2["summary"]["total_written"] == 0
