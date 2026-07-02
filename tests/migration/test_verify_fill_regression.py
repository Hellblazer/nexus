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

Three surfaces + one composed CLI case, per the bead:

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
4. ``TestComposedCliFaultInjection`` — one CLI-level case
   (``nx storage migrate chash --verify-fill``, driven twice through the
   same stateful fakes) proving the wiring end-to-end, not just the
   library functions.

Explicitly OUT of scope (residual scope on the parent bead nexus-s3dd4,
NOT this module): the telemetry surface (rides the .14/v0.1.18 engine
cut — no wired inner-fill surface yet, see
``verify_fill_generic_or_full``/``_telemetry_source_counts`` in
``orchestrator.py``) and the ``tests/e2e/migration-rehearsal`` hole-punch
journey (cloud-gated, needs a deployed engine — separate follow-up).
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
from nexus.migration.orchestrator import verify_fill_catalog, verify_fill_chash
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

_N = 200
_K = 3


class _FakeResponse:
    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
        return {"imported": 0}


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
    (``_client.post`` mutates ``registered`` on receipt — mirroring what
    the real service does), and the outer count source (``counts``,
    derived from the same dict). This is the load-bearing design choice
    that makes "second pass is a true no-op" a real assertion instead of a
    tautology against a static canned snapshot."""

    def __init__(self, registered: dict[str, set[str]]) -> None:
        self.registered: dict[str, set[str]] = {k: set(v) for k, v in registered.items()}
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self._client = self  # _chash_import_fn calls http_chash._client.post(...)

    def registered_chashes_for_collection(self, collection: str) -> set[str]:
        return set(self.registered.get(collection, set()))

    def post(self, url: str, json: dict[str, Any] | None = None) -> _FakeResponse:  # noqa: A002
        self.posts.append((url, json or {}))
        for row in (json or {}).get("rows", []):
            self.registered.setdefault(row["collection"], set()).add(row["chash"])
        return _FakeResponse()

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
# 4. composed CLI-level case
# ═══════════════════════════════════════════════════════════════════════════

_CLI_N = 50
_CLI_K = 3


class _StatefulCliHttpClient:
    def __init__(
        self, posts: list[tuple[str, dict[str, Any]]], registered: dict[str, set[str]],
    ) -> None:
        self._posts = posts
        self._registered = registered

    def post(self, url: str, json: dict[str, Any] | None = None) -> _FakeResponse:  # noqa: A002
        self._posts.append((url, json or {}))
        for row in (json or {}).get("rows", []):
            self._registered.setdefault(row["collection"], set()).add(row["chash"])
        return _FakeResponse()


def _make_stateful_fake_chash_store(
    registered: dict[str, set[str]], posts: list[tuple[str, dict[str, Any]]],
):
    """Factory mirroring test_verify_fill_cli.py's ``_make_fake_chash_store``
    closure pattern, but STATEFUL: ``post`` mutates the same ``registered``
    dict the identity surface reads from, so a second CLI invocation against
    the same closures observes genuine post-fill convergence."""

    class _FakeChashStore:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._client = _StatefulCliHttpClient(posts, registered)

        def registered_chashes_for_collection(self, collection: str) -> set[str]:
            return set(registered.get(collection, set()))

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
