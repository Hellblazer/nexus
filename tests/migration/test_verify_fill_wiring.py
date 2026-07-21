# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-178 wave-2 verify-fill P4 (nexus-s3dd4.5): orchestrator-level wiring
of REAL client -> IdentitySource/ManifestSource adapters.

test_verify_fill_cli.py drives the CLI seam end-to-end; this module tests
:func:`~nexus.migration.orchestrator.verify_fill_catalog` /
:func:`~nexus.migration.orchestrator.verify_fill_telemetry` directly against
fake service clients — the catalog delta path (owners/collections/
document_chunks fill + the documents/links full-ETL fallback), the
telemetry per-table delta path (mapped-vs-unmapped tables, the
mixed-fleet-404 full-ETL fallback), and the R3 breaker-give-up
partial-progress recovery, none of which the CLI test exercises directly.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx

from nexus.migration.orchestrator import (
    EtlSources,
    _telemetry_source_counts,
    verify_fill_catalog,
    verify_fill_generic_or_full,
    verify_fill_telemetry,
)
from nexus.db.t2.telemetry_etl import count_source_rows
from nexus.migration.migration_report import IssueCollector
from nexus.retry import EtlCircuitBreaker

# Reuse the locked telemetry-table seeding helper (single source of truth for
# the 6-table schema; mirrors test_verify_fill_regression.py's own precedent
# for cross-file test-fake reuse).
from tests.db.test_telemetry_etl import _seed_full_telemetry_db  # noqa: PLC2701 — shared test fixture

# ── Fakes ────────────────────────────────────────────────────────────────────


class _FakeCountSource:
    def __init__(self, counts: dict[str, int]) -> None:
        self._counts = counts

    def counts(self, relations: list[str]) -> dict[str, int]:
        return {r: self._counts[r] for r in relations if r in self._counts}


# (dead chash fixtures removed — RDR-187/nexus-piwya.10)


class TestBreakerGiveupRecovery:
    """R3 review note 1, re-vehicled (RDR-187/nexus-piwya.10): the original
    vehicle was verify_fill_chash, retired with the chash ETL store. The
    property under test is GENERIC — _try_fill must catch fill_missing's
    breaker give-up (which propagates WITHOUT a partial FillResult),
    re-probe the identity source, and record a partial-progress issue
    rather than crashing the command — and its surviving call sites are the
    catalog + telemetry legs, so the catalog owners leg carries it now."""

    def test_breaker_giveup_records_partial_progress_not_a_crash(
        self, tmp_path: Path,
    ) -> None:
        catalog_db = tmp_path / ".catalog.db"
        TestVerifyFillCatalog._seed_catalog_db(TestVerifyFillCatalog(), catalog_db)

        class _OutageClient:
            def list_owners(self) -> list[dict]:
                return []  # owner "1" missing -> divergent -> fill runs

            def list_collections(self) -> list[dict]:
                return [{"name": "code__x"}]

            def chashes_for_collection(self, collection: str) -> set[str]:
                return {"a" * 32}

            def get_manifest(self, doc_id: str) -> list:
                return []

            def _post(self, path: str, payload: dict) -> None:
                raise ConnectionError("simulated sustained outage")

            def close(self) -> None:
                pass

        collector = IssueCollector()
        with patch("nexus.retry.time.sleep", return_value=None):
            result = verify_fill_catalog(
                catalog_db, _OutageClient(),
                count_source=_FakeCountSource({
                    "nexus.catalog_owners": 0,           # divergent -> fill
                    "nexus.catalog_documents": 1,        # parity
                    "nexus.catalog_collections": 1,      # parity
                    "nexus.catalog_document_chunks": 1,  # parity
                    "nexus.catalog_links": 0,            # parity
                }),
                breaker=EtlCircuitBreaker(trip_threshold=1, max_trips=0),
                collector=collector,
            )

        fill = result["fill"]["owners"]
        assert fill["status"] == "indeterminate"
        assert fill["filled"] == 0  # nothing landed (post always failed)
        # the failure is recorded (gates total_failed), never silent
        issues = collector.issues_for("catalog", "owners")
        assert any(i.action == "failed" for i in issues)


class TestVerifyFillCatalog:
    def _seed_catalog_db(self, catalog_db: Path) -> None:
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
            "INSERT INTO documents VALUES ('1.1', 'doc', '', 0, '', '', '', "
            "'code__x', 1, '', '', NULL, 0, '', '', 0, '', '', 0, '', '', '', '')"
        )
        conn.execute(
            "INSERT INTO collections VALUES ('code__x', 'code', '1', 'voyage', "
            "'v1', '', 0, '', '', '')"
        )
        conn.execute(
            "INSERT INTO document_chunks VALUES ('1.1', 0, ?, 0, 0, 0, 0, 0)",
            ("a" * 32,),
        )
        conn.commit()
        conn.close()

    def test_documents_divergent_falls_back_to_full_etl(self, tmp_path: Path) -> None:
        catalog_db = tmp_path / ".catalog.db"
        self._seed_catalog_db(catalog_db)

        full_etl_called = {"n": 0}

        def _fake_migrate_catalog(db_path, client, *, collector=None, breaker=None):
            full_etl_called["n"] += 1
            return {
                "owners": {"read": 1, "written": 1},
                "documents": {"read": 1, "written": 1},
                "collections": {"read": 1, "written": 1},
                "document_chunks": {"read": 1, "written": 1},
                "links": {"read": 0, "written": 0},
            }

        class _FakeClient:
            def close(self) -> None:
                pass

        with patch(
            "nexus.db.t2.catalog_etl.migrate_catalog",
            side_effect=_fake_migrate_catalog,
        ):
            result = verify_fill_catalog(
                catalog_db, _FakeClient(),
                # documents relation short -> triggers full fallback
                count_source=_FakeCountSource({
                    "nexus.catalog_owners": 1,
                    "nexus.catalog_documents": 0,
                    "nexus.catalog_collections": 1,
                    "nexus.catalog_document_chunks": 1,
                    "nexus.catalog_links": 0,
                }),
            )

        assert full_etl_called["n"] == 1
        assert result["fallback"] == "full_etl"
        assert result["total_filled"] == 4

    def test_owners_collections_chunks_delta_fill_when_docs_links_parity(
        self, tmp_path: Path,
    ) -> None:
        catalog_db = tmp_path / ".catalog.db"
        self._seed_catalog_db(catalog_db)

        class _FakeCatalogClient:
            def __init__(self) -> None:
                self.posts: list[tuple[str, dict]] = []

            def list_owners(self) -> list[dict]:
                return []  # owner "1" missing -> divergent fill

            def list_collections(self) -> list[dict]:
                return [{"name": "code__x"}]  # already present -> parity, no fill

            def chashes_for_collection(self, collection: str) -> set[str]:
                return set()  # chunk chash missing -> definite miss

            def get_manifest(self, doc_id: str) -> list[Any]:
                return []

            def _post(self, path: str, payload: dict) -> None:
                self.posts.append((path, payload))

            def close(self) -> None:
                pass

        client = _FakeCatalogClient()

        with patch(
            "nexus.db.t2.catalog_etl.migrate_catalog",
        ) as full_etl:
            result = verify_fill_catalog(
                catalog_db, client,
                count_source=_FakeCountSource({
                    "nexus.catalog_owners": 0,       # divergent
                    "nexus.catalog_documents": 1,    # parity
                    "nexus.catalog_collections": 1,  # parity
                    "nexus.catalog_document_chunks": 0,  # divergent
                    "nexus.catalog_links": 0,        # parity (source has 0 links)
                }),
            )

        full_etl.assert_not_called()
        assert "owners" in result["fill"]
        assert result["fill"]["owners"]["filled"] == 1
        assert "collections" not in result["fill"]  # was parity, no fill call
        assert "document_chunks" in result["fill"]
        assert result["fill"]["document_chunks"]["filled"] == 1
        assert result["total_filled"] == 2
        # real client methods were exercised, not a full re-send
        assert any(p[0] == "/import/owner" for p in client.posts)
        assert any(p[0] == "/import/chunk" for p in client.posts)


class TestTelemetryPartialCoverageGuard:
    """NOTE (P3b, nexus-s3dd4.14): ``verify_fill_generic_or_full`` itself is
    UNCHANGED — this class still validates its own general contract (an
    unmapped table is never silently treated as a store-level pass) and
    remains a true regression for any caller that DOES go through this
    generic path (``nx storage migrate telemetry --verify-fill``'s
    single-store CLI command still does, see ``verify_fill_telemetry``'s
    "Scope boundary" docstring paragraph). ``migrate_all`` no longer routes
    the ``telemetry`` store through this function — it calls
    :func:`~nexus.migration.orchestrator.verify_fill_telemetry` directly,
    which enforces the EQUIVALENT guarantee (no unmapped table is ever
    silently skipped) at PER-TABLE granularity instead of an
    all-or-nothing store-level full-ETL fallback — see
    ``TestVerifyFillTelemetry`` below for that surface's own regression."""

    def test_unmapped_telemetry_tables_force_full_etl_even_when_mapped_pair_is_parity(
        self,
    ) -> None:
        """R4 substantive-critic HIGH (2026-07-02): only hook_failures +
        nx_answer_runs have a _VERIFY_TABLES mapping; the other 4 telemetry
        tables land ``indeterminate`` and indeterminate is never a pass —
        so a --verify-fill telemetry run must NEVER skip the full ETL on
        2/6-table parity. A hole in e.g. frecency would otherwise be
        undetectable."""
        source_counts = {
            "hook_failures": 3,      # mapped, parity below
            "nx_answer_runs": 7,     # mapped, parity below
            "relevance_log": 100,    # unmapped -> indeterminate
            "search_telemetry": 50,  # unmapped -> indeterminate
            "tier_writes": 25,       # unmapped -> indeterminate
            "frecency": 10,          # unmapped -> indeterminate (drifted or not: unknowable)
        }
        ran = {"full": 0}

        def run_full() -> str:
            ran["full"] += 1
            return "full-etl-ran"

        verdicts, full_result, _notes = verify_fill_generic_or_full(
            "telemetry", source_counts, run_full,
            count_source=_FakeCountSource({
                "nexus.hook_failures": 3,
                "nexus.nx_answer_runs": 7,
            }),
        )

        assert ran["full"] == 1
        assert full_result == "full-etl-ran"
        assert verdicts["hook_failures"]["status"] == "parity"
        assert verdicts["frecency"]["status"] == "indeterminate"

    def test_telemetry_source_counts_passes_all_six_tables_through(
        self, tmp_path: Path,
    ) -> None:
        db = tmp_path / "memory.db"
        conn = sqlite3.connect(str(db))
        conn.close()
        expected = set(count_source_rows(db).keys())
        got = set(_telemetry_source_counts(
            EtlSources(sqlite_path=db, catalog_db_path=None),  # type: ignore[arg-type]
        ).keys())
        assert got == expected
        assert {"relevance_log", "search_telemetry", "tier_writes", "frecency"} <= got


# ── verify_fill_telemetry (P3b, nexus-s3dd4.14) ───────────────────────────────


class _FakeTelemetryClient:
    """Fake ``HttpTelemetryStore`` for ``verify_fill_telemetry`` wiring tests.

    ``present_by_table`` maps table -> set of conflict-key TUPLES already
    "landed" in the target; ``probe_ids`` answers membership from it. NOT a
    stateful/self-mutating fake (test_verify_fill_regression.py's P6 fault-
    injection style) — telemetry fault-injection is explicitly out of P6's
    scope (see that module's docstring); these tests validate the WIRING
    (which table gets probed, which rows get sent, the 404 fallback), not a
    second convergence pass.
    """

    def __init__(
        self,
        present_by_table: dict[str, set[tuple]] | None = None,
        *,
        force_404: bool = False,
    ) -> None:
        self._present_by_table = present_by_table or {}
        self._force_404 = force_404
        self.probe_calls: list[tuple[str, list[list]]] = []
        self.import_calls: list[tuple[str, list[dict]]] = []

    def probe_ids(self, table: str, keys: list[list]) -> list[list]:
        self.probe_calls.append((table, [list(k) for k in keys]))
        if self._force_404:
            request = httpx.Request("POST", "http://fake/v1/telemetry/ids/probe")
            response = httpx.Response(404, request=request, json={"error": "not found"})
            raise httpx.HTTPStatusError("404 Not Found", request=request, response=response)
        present = self._present_by_table.get(table, set())
        return [list(k) for k in keys if tuple(k) in present]

    def import_rows_batch(self, table: str, rows: list[dict]) -> int:
        self.import_calls.append((table, list(rows)))
        return len(rows)

    def close(self) -> None:
        pass


_DAYS_AGO_CACHE: dict[int, str] = {}


def _days_ago_iso(days: int) -> str:
    """Memoized so seed and present-set tuples share the EXACT string (the
    conflict key includes the timestamp verbatim)."""
    from datetime import UTC, datetime, timedelta
    if days not in _DAYS_AGO_CACHE:
        _DAYS_AGO_CACHE[days] = (
            datetime.now(UTC) - timedelta(days=days)
        ).isoformat()
    return _DAYS_AGO_CACHE[days]


class TestVerifyFillTelemetry:
    def test_mixed_parity_divergent_and_unmapped_tables_in_one_run(
        self, tmp_path: Path,
    ) -> None:
        """The core P3b scenario: a mapped-parity table skips its probe
        entirely; a mapped-divergent table and all FOUR unmapped tables
        (relevance_log/search_telemetry/tier_writes/frecency — no
        _VERIFY_TABLES relation, always outer-indeterminate) are each probed
        and filled with EXACTLY their missing rows, never a full re-send."""
        db = tmp_path / "t2.db"
        _seed_full_telemetry_db(
            db,
            hooks=[  # mapped, PG count == source count -> parity, no probe
                {"doc_id": "d1", "hook_name": "h1", "occurred_at": "2024-01-01T00:00:00Z"},
                {"doc_id": "d2", "hook_name": "h1", "occurred_at": "2024-01-02T00:00:00Z"},
                {"doc_id": "d3", "hook_name": "h2", "occurred_at": "2024-01-03T00:00:00Z"},
            ],
            nx=[  # mapped, PG count < source count -> divergent, probe+fill
                {"question": f"q{i}", "created_at": f"2024-02-0{i}T00:00:00Z"}
                for i in range(1, 6)  # 5 source rows
            ],
            relevance=[  # unmapped -> always indeterminate -> probe+fill.
                # nexus-24p05: timestamps must sit INSIDE the 90-day retention
                # horizon or the fresh-window scope excludes them (the sweep's
                # domain). now-relative, monotonic — not a randomness source.
                *[
                    {"query": f"q{i}", "chunk_id": f"c{i}", "action": "store_put",
                     "timestamp": _days_ago_iso(i)}
                    for i in range(1, 5)  # 4 fresh source rows
                ],
                # One EXPIRED-side row with a matching target hole: verify-fill
                # must NEVER re-import it (the resurrect exposure 24p05 closed).
                {"query": "qold", "chunk_id": "cold", "action": "store_put",
                 "timestamp": _days_ago_iso(400)},
            ],
            search=[  # unmapped, but everything already present -> 0 missing
                {"ts": "2024-04-01T00:00:00Z", "query_hash": "h1", "collection": "code__x"},
                {"ts": "2024-04-02T00:00:00Z", "query_hash": "h2", "collection": "code__x"},
            ],
            tier=[],  # unmapped, empty source -> trivial parity, no probe
            frecency=[{"chunk_id": "fc1"}],  # unmapped, missing -> probe+fill
        )

        # target already has: 2 of 5 nx_answer_runs; 1 of 4 relevance_log;
        # BOTH search_telemetry rows; 0 of 1 frecency.
        present = {
            "nx_answer_runs": {("q1", "2024-02-01T00:00:00Z"), ("q2", "2024-02-02T00:00:00Z")},
            "relevance_log": {("q1", "c1", "store_put", "", _days_ago_iso(1))},
            "search_telemetry": {
                ("2024-04-01T00:00:00Z", "h1", "code__x"),
                ("2024-04-02T00:00:00Z", "h2", "code__x"),
            },
            "frecency": set(),
        }
        client = _FakeTelemetryClient(present)
        count_source = _FakeCountSource({
            "nexus.hook_failures": 3,     # matches source -> parity
            "nexus.nx_answer_runs": 2,    # short -> divergent
        })

        result = verify_fill_telemetry(db, client, count_source=count_source)

        # hook_failures: mapped parity -> zero probe calls, zero import calls
        assert all(t != "hook_failures" for t, _ in client.probe_calls)
        assert all(t != "hook_failures" for t, _ in client.import_calls)
        assert "hook_failures" not in result["fill"]

        # tier_writes: empty source -> zero probe calls at all
        assert all(t != "tier_writes" for t, _ in client.probe_calls)
        assert result["fill"]["tier_writes"] == {
            "source_count": 0, "target_count": None, "missing": 0,
            "filled": 0, "status": "parity",
        }

        # nx_answer_runs: mapped divergent -> exactly the 3 missing rows sent
        assert result["fill"]["nx_answer_runs"]["missing"] == 3
        assert result["fill"]["nx_answer_runs"]["filled"] == 3
        nx_sent = {
            (r["question"], r["created_at"])
            for t, rows in client.import_calls if t == "nx_answer_runs"
            for r in rows
        }
        assert nx_sent == {
            ("q3", "2024-02-03T00:00:00Z"),
            ("q4", "2024-02-04T00:00:00Z"),
            ("q5", "2024-02-05T00:00:00Z"),
        }

        # relevance_log: UNMAPPED (no _VERIFY_TABLES relation) but STILL
        # probed + delta-filled, never a full re-send over the whole table.
        assert result["fill"]["relevance_log"]["missing"] == 3
        assert result["fill"]["relevance_log"]["filled"] == 3
        relevance_sent = {
            r["chunk_id"]
            for t, rows in client.import_calls if t == "relevance_log"
            for r in rows
        }
        assert relevance_sent == {"c2", "c3", "c4"}
        assert "cold" not in relevance_sent, (
            "nexus-24p05: an expired-side source row (outside the retention "
            "horizon) must never be re-imported - that is the sweep's domain"
        )

        # search_telemetry: unmapped, probed, everything already present ->
        # zero rows sent (not a full re-send of the 2 source rows either).
        assert result["fill"]["search_telemetry"]["missing"] == 0
        assert all(t != "search_telemetry" for t, _ in client.import_calls)

        # frecency: unmapped, single missing row -> filled
        assert result["fill"]["frecency"]["missing"] == 1
        assert result["fill"]["frecency"]["filled"] == 1

        assert result["total_filled"] == 3 + 3 + 0 + 1  # nx + relevance + search + frecency
        assert "fallback" not in result

    def test_pre_v0_1_18_engine_404_falls_back_to_full_etl_never_crashes(
        self, tmp_path: Path,
    ) -> None:
        """R1 note 2 (mixed-fleet gate): a pre-v0.1.18 engine 404s
        /v1/telemetry/ids/probe. This must NOT crash migrate-all and must
        NOT be treated as "nothing present" (which would blindly re-send
        every row as "missing") -- it falls back to the unchanged full
        telemetry ETL for the WHOLE store."""
        db = tmp_path / "t2.db"
        _seed_full_telemetry_db(db, hooks=[
            {"doc_id": "d1", "hook_name": "h1", "occurred_at": "2024-01-01T00:00:00Z"},
        ])
        client = _FakeTelemetryClient(force_404=True)
        count_source = _FakeCountSource({"nexus.hook_failures": 0})  # divergent -> needs fill

        full_etl_called = {"n": 0}

        def _fake_migrate_telemetry_rows(sqlite_path, store, *, collector=None, breaker=None):
            full_etl_called["n"] += 1
            return {t: {"read": 0, "written": 0} for t in (
                "relevance_log", "search_telemetry", "tier_writes",
                "nx_answer_runs", "hook_failures", "frecency",
            )} | {"hook_failures": {"read": 1, "written": 1}}

        with patch(
            "nexus.db.t2.telemetry_etl.migrate_telemetry_rows",
            side_effect=_fake_migrate_telemetry_rows,
        ):
            result = verify_fill_telemetry(db, client, count_source=count_source)

        assert full_etl_called["n"] == 1
        assert result["fallback"] == "full_etl"
        assert result["total_filled"] == 1
        # never a blind "everything missing" send through import_rows_batch
        assert client.import_calls == []

    def test_non_404_probe_failure_is_indeterminate_for_that_table_only(
        self, tmp_path: Path,
    ) -> None:
        """A transient (non-404) probe failure must NOT trigger the
        store-wide full-ETL fallback -- only a confirmed capability gap
        (404) does that. A 5xx/transport error reports indeterminate for
        that table alone, same as chash's/catalog's real IdentitySource
        wiring."""
        db = tmp_path / "t2.db"
        _seed_full_telemetry_db(db, hooks=[
            {"doc_id": "d1", "hook_name": "h1", "occurred_at": "2024-01-01T00:00:00Z"},
        ])

        class _FlakyClient(_FakeTelemetryClient):
            def probe_ids(self, table, keys):
                self.probe_calls.append((table, [list(k) for k in keys]))
                raise ConnectionError("simulated transient outage")

        client = _FlakyClient()
        count_source = _FakeCountSource({"nexus.hook_failures": 0})

        result = verify_fill_telemetry(db, client, count_source=count_source)

        assert "fallback" not in result  # NOT a store-wide full-ETL fallback
        assert result["fill"]["hook_failures"]["status"] == "indeterminate"
        assert result["fill"]["hook_failures"]["filled"] == 0
        assert client.import_calls == []
