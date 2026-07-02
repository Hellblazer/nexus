# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-178 wave-2 verify-fill P4 (nexus-s3dd4.5): orchestrator-level wiring
of REAL client -> IdentitySource/ManifestSource adapters.

test_verify_fill_cli.py drives the CLI seam end-to-end; this module tests
:func:`nexus.migration.orchestrator.verify_fill_chash` /
:func:`~nexus.migration.orchestrator.verify_fill_catalog` directly against
fake service clients — the catalog delta path (owners/collections/
document_chunks fill + the documents/links full-ETL fallback) and the R3
breaker-give-up partial-progress recovery, neither of which the CLI test
exercises directly.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import patch

from nexus.migration.orchestrator import (
    EtlSources,
    _telemetry_source_counts,
    verify_fill_catalog,
    verify_fill_chash,
    verify_fill_generic_or_full,
)
from nexus.db.t2.telemetry_etl import count_source_rows
from nexus.migration.migration_report import IssueCollector
from nexus.retry import EtlCircuitBreaker

# ── Fakes ────────────────────────────────────────────────────────────────────


class _FakeCountSource:
    def __init__(self, counts: dict[str, int]) -> None:
        self._counts = counts

    def counts(self, relations: list[str]) -> dict[str, int]:
        return {r: self._counts[r] for r in relations if r in self._counts}


class _FakeResponse:
    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
        return {"imported": 0}


class _FakeChashClient:
    def __init__(self, registered: dict[str, set[str]]) -> None:
        self._registered = registered
        self.posts: list[tuple[str, dict]] = []
        self._client = self

    def post(self, url: str, json: dict[str, Any] | None = None) -> _FakeResponse:  # noqa: A002
        self.posts.append((url, json))
        return _FakeResponse()

    def registered_chashes_for_collection(self, collection: str) -> set[str]:
        return set(self._registered.get(collection, set()))

    def close(self) -> None:
        pass


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


class TestVerifyFillChashBreakerRecovery:
    def test_breaker_giveup_records_partial_progress_not_a_crash(
        self, tmp_path: Path,
    ) -> None:
        """R3 review note 1: fill_missing propagates a breaker give-up
        WITHOUT a partial FillResult -- verify_fill_chash must catch it,
        re-probe the identity source, and record a partial-progress issue
        rather than crashing the whole command."""
        db = tmp_path / "t2.db"
        _seed_chash_db(db, [
            ("a" * 32, "code__x"), ("b" * 32, "code__x"), ("c" * 32, "code__x"),
        ])
        client = _FakeChashClient({"code__x": set()})

        call_count = {"n": 0}
        orig_post = client.post

        def _flaky_post(url, json=None):  # noqa: A002
            call_count["n"] += 1
            # first batch call always raises a retryable transport error;
            # every OTHER (never happens here, batch_size=200 => one batch)
            raise ConnectionError("simulated sustained outage")

        client.post = _flaky_post
        collector = IssueCollector()

        with patch("nexus.retry.time.sleep", return_value=None):
            result = verify_fill_chash(
                db, client,
                count_source=_FakeCountSource({"nexus.chash_index": 0}),
                breaker=EtlCircuitBreaker(trip_threshold=1, max_trips=0),
                collector=collector,
            )

        assert call_count["n"] > 0  # the flaky post really was invoked
        fill = result["fill"]["code__x"]
        assert fill["status"] == "indeterminate"
        assert fill["filled"] == 0  # nothing landed (post always failed)
        # the failure is recorded (gates total_failed), never silent
        issues = collector.issues_for("chash", "chash_index")
        assert any(i.action == "failed" for i in issues)
        client.post = orig_post  # restore (unused after this point)


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
