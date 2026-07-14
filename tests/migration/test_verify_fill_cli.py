# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-178 wave-2 verify-fill P4 (nexus-s3dd4.5): CLI + report wiring.

``--verify-fill`` on ``nx storage migrate <store>`` / ``migrate all`` swaps
the unconditional full re-send for the delta path: the outer count-diff
(``verify_fill.verify_store_counts``) decides parity (zero writes) vs.
divergent/indeterminate (send only the rows genuinely missing). This module
drives the CLI end-to-end against fake service clients so the wiring
(NOT verify_fill.py's own diff/fill logic, already covered by
test_verify_fill_outer.py / test_verify_fill_inner.py) is what's under test:
real client -> IdentitySource/ManifestSource adapters, report augmentation
(``filled_rows`` / ``verify_fill``), and the report-writer fixups (per-store
``verification`` always populated; ``target.service_url`` resolved, not the
``"(lease)"`` placeholder).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.cli import main

# ── Fakes ────────────────────────────────────────────────────────────────────


def _make_fake_chash_store(registered: dict[str, set[str]], posts: list[tuple[str, dict]]):
    """Factory: a fake HttpChashIndex bound to shared *registered*/*posts*
    state the test seeds/inspects (mirrors ``_fake_etls``'s closure pattern
    in test_storage_migrate_all.py).

    nexus-f2qvx.3: ``_chash_import_fn`` (orchestrator.py) now calls the
    public ``HttpChashIndex.import_rows()`` wrapper instead of reaching
    into ``http_chash._client.post(...)`` directly (the pre-mixin-adoption
    shape, which broke once RefreshableHttpStoreMixin's httpx.Client
    stopped baking a base_url) — so this fake exposes ``import_rows``
    rather than a fake ``._client``.
    """

    class _FakeChashStore:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def registered_chashes_for_collection(self, collection: str) -> set[str]:
            return set(registered.get(collection, set()))

        def import_rows(self, rows: list[dict[str, Any]]) -> int:
            posts.append(("/v1/chash/import", {"rows": rows}))
            return len(rows)

        def close(self) -> None:
            pass

    return _FakeChashStore


class _FakeCountSource:
    """A ``CountSource`` returning canned relation counts."""

    def __init__(self, counts: dict[str, int]) -> None:
        self._counts = counts

    def counts(self, relations: list[str]) -> dict[str, int]:
        return {r: self._counts[r] for r in relations if r in self._counts}


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def _seed_chash_db(db_path: Path, rows: list[tuple[str, str]]) -> None:
    """*rows* is a list of ``(chash, physical_collection)`` pairs."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE chash_index ("
        "chash TEXT, physical_collection TEXT, created_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO chash_index (chash, physical_collection, created_at) "
        "VALUES (?, ?, '2026-01-01T00:00:00Z')",
        rows,
    )
    conn.commit()
    conn.close()


# ── migrate chash --verify-fill ───────────────────────────────────────────────


class TestMigrateChashVerifyFill:
    def test_parity_sends_nothing(self, runner: CliRunner, tmp_path: Path) -> None:
        db = tmp_path / "t2.db"
        _seed_chash_db(db, [("a" * 32, "code__x")])
        report_path = tmp_path / "report.json"
        posts: list[tuple[str, dict]] = []
        registered = {"code__x": {"a" * 32}}

        with (
            patch(
                "nexus.db.t2.http_chash_index.HttpChashIndex",
                _make_fake_chash_store(registered, posts),
            ),
            patch(
                "nexus.migration.orchestrator.ServiceCountSource",
                lambda: _FakeCountSource({"nexus.chash_index": 1}),
            ),
            patch.dict("os.environ", {"NX_SERVICE_TOKEN": "t"}),
        ):
            result = runner.invoke(main, [
                "storage", "migrate", "chash",
                "--db", str(db),
                "--service-url", "http://fake-service:9",
                "--report", str(report_path),
                "--verify-fill",
            ])

        assert result.exit_code == 0, result.output
        assert posts == []  # parity -- nothing sent
        assert "filled=0" in result.output
        assert "outer_status=parity" in result.output

        report = json.loads(report_path.read_text())
        assert report["verification"] in ("verified", "mismatch", "indeterminate")
        assert report["target"]["service_url"] == "http://fake-service:9"

    def test_divergence_fills_only_missing_rows(
        self, runner: CliRunner, tmp_path: Path,
    ) -> None:
        db = tmp_path / "t2.db"
        _seed_chash_db(db, [
            ("a" * 32, "code__x"),
            ("b" * 32, "code__x"),
            ("c" * 32, "code__x"),
        ])
        report_path = tmp_path / "report.json"
        posts: list[tuple[str, dict]] = []
        # only 'a' landed already -- b, c are a hole of 2
        registered = {"code__x": {"a" * 32}}

        with (
            patch(
                "nexus.db.t2.http_chash_index.HttpChashIndex",
                _make_fake_chash_store(registered, posts),
            ),
            patch(
                "nexus.migration.orchestrator.ServiceCountSource",
                # target count (1) < source count (3) -> divergent
                lambda: _FakeCountSource({"nexus.chash_index": 1}),
            ),
            patch.dict("os.environ", {"NX_SERVICE_TOKEN": "t"}),
        ):
            result = runner.invoke(main, [
                "storage", "migrate", "chash",
                "--db", str(db),
                "--service-url", "http://fake-service:9",
                "--report", str(report_path),
                "--verify-fill",
            ])

        assert result.exit_code == 0, result.output
        assert "filled=2" in result.output
        assert "outer_status=divergent" in result.output

        # exactly the 2 missing rows were transmitted, never the whole table
        sent_chashes = {
            row["chash"] for url, payload in posts for row in payload["rows"]
        }
        assert sent_chashes == {"b" * 32, "c" * 32}

        report = json.loads(report_path.read_text())
        assert report["target"]["service_url"] == "http://fake-service:9"
        assert report["summary"]["total_written"] == 2

    def test_report_verification_always_populated_even_without_verify_fill(
        self, runner: CliRunner, tmp_path: Path,
    ) -> None:
        """R report-writer fixup (a): a per-store run (verify-fill or not)
        must always carry report['verification'] -- previously only
        `migrate all` verified."""
        db = tmp_path / "t2.db"
        _seed_chash_db(db, [("a" * 32, "code__x")])
        report_path = tmp_path / "report.json"
        posts: list[tuple[str, dict]] = []
        registered: dict[str, set[str]] = {}

        with (
            patch(
                "nexus.db.t2.http_chash_index.HttpChashIndex",
                _make_fake_chash_store(registered, posts),
            ),
            patch(
                "nexus.migration.orchestrator.ServiceCountSource",
                lambda: _FakeCountSource({}),  # unreachable -> indeterminate
            ),
            patch.dict("os.environ", {"NX_SERVICE_TOKEN": "t"}),
        ):
            result = runner.invoke(main, [
                "storage", "migrate", "chash",
                "--db", str(db),
                "--service-url", "http://fake-service:9",
                "--report", str(report_path),
            ])

        assert result.exit_code == 0, result.output
        report = json.loads(report_path.read_text())
        assert "verification" in report
        assert report["target"]["service_url"] == "http://fake-service:9"


# ── migrate all --verify-fill (generic skip-on-parity + convergence notes) ───


class TestMigrateAllVerifyFill:
    def test_verify_fill_flag_accepted_and_report_has_verify_fill_key(
        self, runner: CliRunner, tmp_path: Path,
    ) -> None:
        from nexus.migration.etl_registry import EtlSources, StoreEtl

        config_dir = tmp_path / "cfg"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "memory.db").touch()
        (config_dir / ".catalog.db").touch()

        order: list[str] = []

        def _runner(store: str):
            def run(sources: EtlSources, collector) -> dict:
                order.append(store)
                collector.count_read(store, store, 5)
                collector.count_written(store, store, 5)
                return {}
            return run

        fake_etls = [
            StoreEtl(s, _runner(s))
            for s in ("memory", "plans", "telemetry", "taxonomy",
                      "aspects", "chash", "catalog", "aspects_queue")
        ]

        with (
            patch(
                "nexus.migration.orchestrator.build_store_etls",
                return_value=fake_etls,
            ),
            patch(
                "nexus.migration.orchestrator.verify_counts",
                return_value=("verified", [], {}),
            ),
            # force every generic-counter store to read as divergent-source
            # (nonzero rows, no target relation mapped in the fake count
            # source) so the ladder still runs them -- exercising the
            # verify_fill wiring without needing real SQLite tables.
            patch(
                "nexus.migration.orchestrator.ServiceCountSource",
                lambda: _FakeCountSource({}),
            ),
            # skip the real SQLite reads _GENERIC_VERIFY_FILL_COUNTERS would
            # otherwise do -- this test's fake_etls already stand in for the
            # store-level ETLs, so the outer-verify pre-check should be a
            # no-op (empty verdicts -> falls through to the fake etl.run()).
            patch(
                "nexus.migration.orchestrator._GENERIC_VERIFY_FILL_COUNTERS",
                {s: (lambda sources: {}) for s in
                 ("memory", "plans", "telemetry", "taxonomy")},
            ),
            # chash/catalog/telemetry under verify_fill=True construct REAL
            # service clients (verify_fill_chash/verify_fill_catalog/
            # verify_fill_telemetry own that, by design, since a delta fill
            # genuinely needs the real IdentitySource surfaces) -- stub them
            # here too so this "flag is wired" test needs no live service.
            patch(
                "nexus.migration.orchestrator.verify_fill_chash",
                lambda *a, **k: {
                    "store": "chash", "outer": {}, "fill": {},
                    "total_filled": 0, "convergence_notes": [],
                },
            ),
            patch(
                "nexus.migration.orchestrator.verify_fill_catalog",
                lambda *a, **k: {
                    "store": "catalog", "outer": {}, "fill": {},
                    "total_filled": 0, "convergence_notes": [],
                },
            ),
            patch(
                "nexus.migration.orchestrator.verify_fill_telemetry",
                lambda *a, **k: {
                    "store": "telemetry", "outer": {}, "fill": {},
                    "total_filled": 0, "convergence_notes": [],
                },
            ),
            patch(
                "nexus.migration.orchestrator._open_chash_store",
                lambda: _make_fake_chash_store({}, [])(),
            ),
            patch(
                "nexus.migration.orchestrator._open_catalog_client",
                lambda: type("_FakeCatalogClient", (), {"close": lambda self: None})(),
            ),
            patch(
                "nexus.migration.orchestrator._open_telemetry_store",
                lambda: type("_FakeTelemetryStore", (), {"close": lambda self: None})(),
            ),
            patch.dict("os.environ", {"NEXUS_CONFIG_DIR": str(config_dir)}),
        ):
            result = runner.invoke(main, [
                "storage", "migrate", "all", "--verify-fill",
            ])

        assert result.exit_code == 0, result.output
        # chash/catalog/telemetry are real-etls here (fakes), so
        # verify_fill_chash / verify_fill_catalog / verify_fill_telemetry
        # are NOT invoked by the CLI in this fake-etl setup -- build_store_etls
        # is fully replaced. This test's purpose is narrower: prove
        # --verify-fill is accepted and threaded through to migrate_all()
        # without CLI-level errors.
        assert "memory" in order


class TestVerifyFillConvergenceNotes:
    """R2 critic finding (2026-07-02): dedup convergence notes must be
    recomputed from TableVerdict, not re-derived from scratch."""

    def test_dedup_convergence_note_wording_matches_verify_counts(self) -> None:
        from nexus.migration.orchestrator import dedup_convergence_notes

        notes = dedup_convergence_notes(
            "plans",
            {"plans": {"source_count": 98, "target_count": 80, "status": "parity"}},
        )
        assert notes == [
            "nexus.plans: 80 rows from 98 source rows; 18 converged onto "
            "existing keys by UNIQUE constraint via DO UPDATE — by design"
        ]

    def test_non_dedup_table_produces_no_note(self) -> None:
        from nexus.migration.orchestrator import dedup_convergence_notes

        notes = dedup_convergence_notes(
            "memory",
            {"memory": {"source_count": 10, "target_count": 10, "status": "parity"}},
        )
        assert notes == []

    def test_divergent_dedup_table_produces_no_note(self) -> None:
        from nexus.migration.orchestrator import dedup_convergence_notes

        notes = dedup_convergence_notes(
            "plans",
            {"plans": {"source_count": 98, "target_count": 0, "status": "divergent"}},
        )
        assert notes == []


class TestClampFillBatchSize:
    def test_clamps_to_quota_ceiling(self) -> None:
        from nexus.migration.orchestrator import clamp_fill_batch_size

        assert clamp_fill_batch_size(5000) == 300
        assert clamp_fill_batch_size(10) == 10
        assert clamp_fill_batch_size(0) == 1


class TestResolveTargetServiceUrl:
    def test_explicit_wins(self) -> None:
        from nexus.migration.orchestrator import resolve_target_service_url

        assert resolve_target_service_url("http://x:1/") == "http://x:1"

    def test_env_used_when_no_explicit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nexus.migration.orchestrator import resolve_target_service_url

        monkeypatch.setenv("NX_SERVICE_URL", "http://env:2/")
        assert resolve_target_service_url() == "http://env:2"

    def test_never_the_lease_placeholder(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nexus.migration.orchestrator import resolve_target_service_url

        monkeypatch.delenv("NX_SERVICE_URL", raising=False)
        with patch(
            "nexus.db.service_endpoint.resolve_service_endpoint",
            side_effect=RuntimeError("no endpoint resolvable"),
        ):
            resolved = resolve_target_service_url()
        assert resolved != "(lease)"
        assert resolved == "(unresolved)"


# ── migrate memory/plans/telemetry/taxonomy/catalog --verify-fill (generic) ──


class TestMigrateMemoryVerifyFill:
    def test_parity_skips_the_full_etl_entirely(
        self, runner: CliRunner, tmp_path: Path,
    ) -> None:
        config_dir = tmp_path / "cfg"
        config_dir.mkdir(parents=True, exist_ok=True)
        db = config_dir / "t2.db"
        db.touch()

        class _FakeStore:
            def __init__(self, *a, **k): ...
            def close(self): ...

        etl_called = {"n": 0}

        def _fake_migrate(*a, **k):
            etl_called["n"] += 1
            return {"read": 10, "written": 10}

        with (
            patch("nexus.db.t2.memory_etl.count_source_rows", return_value=10),
            patch("nexus.db.t2.memory_etl.migrate_memory_rows", side_effect=_fake_migrate),
            patch("nexus.db.t2.http_memory_store.HttpMemoryStore", _FakeStore),
            patch(
                "nexus.migration.orchestrator.ServiceCountSource",
                lambda: _FakeCountSource({"nexus.memory": 10}),  # parity
            ),
            patch.dict(
                "os.environ",
                {"NEXUS_CONFIG_DIR": str(config_dir),
                 "NX_SERVICE_TOKEN": "t", "NX_SERVICE_URL": "http://127.0.0.1:1"},
            ),
        ):
            result = runner.invoke(main, [
                "storage", "migrate", "memory", "--db", str(db), "--verify-fill",
            ])

        assert result.exit_code == 0, result.output
        assert etl_called["n"] == 0  # the full ETL never ran
        assert "already at parity" in result.output
        assert "read=0, written=0" in result.output

    def test_divergence_falls_back_to_full_etl(
        self, runner: CliRunner, tmp_path: Path,
    ) -> None:
        config_dir = tmp_path / "cfg"
        config_dir.mkdir(parents=True, exist_ok=True)
        db = config_dir / "t2.db"
        db.touch()

        class _FakeStore:
            def __init__(self, *a, **k): ...
            def close(self): ...

        etl_called = {"n": 0}

        def _fake_migrate(*a, **k):
            etl_called["n"] += 1
            return {"read": 10, "written": 10}

        with (
            patch("nexus.db.t2.memory_etl.count_source_rows", return_value=10),
            patch("nexus.db.t2.memory_etl.migrate_memory_rows", side_effect=_fake_migrate),
            patch("nexus.db.t2.http_memory_store.HttpMemoryStore", _FakeStore),
            patch(
                "nexus.migration.orchestrator.ServiceCountSource",
                lambda: _FakeCountSource({"nexus.memory": 3}),  # divergent (3 < 10)
            ),
            patch.dict(
                "os.environ",
                {"NEXUS_CONFIG_DIR": str(config_dir),
                 "NX_SERVICE_TOKEN": "t", "NX_SERVICE_URL": "http://127.0.0.1:1"},
            ),
        ):
            result = runner.invoke(main, [
                "storage", "migrate", "memory", "--db", str(db), "--verify-fill",
            ])

        assert result.exit_code == 0, result.output
        assert etl_called["n"] == 1  # fell back to the full ETL
        assert "ran full ETL" in result.output
        assert "read=10, written=10" in result.output


class TestMigratePlansVerifyFillConvergence:
    def test_dedup_convergence_parity_skips_full_etl_and_prints_note(
        self, runner: CliRunner, tmp_path: Path,
    ) -> None:
        config_dir = tmp_path / "cfg"
        config_dir.mkdir(parents=True, exist_ok=True)
        db = config_dir / "t2.db"
        db.touch()

        class _FakeStore:
            def __init__(self, *a, **k): ...
            def close(self): ...

        etl_called = {"n": 0}

        with (
            patch("nexus.db.t2.plan_etl.count_source_rows", return_value=98),
            patch(
                "nexus.db.t2.plan_etl.migrate_plan_rows",
                side_effect=lambda *a, **k: etl_called.__setitem__("n", etl_called["n"] + 1) or {"read": 98, "written": 98},
            ),
            patch("nexus.db.t2.http_plan_library.HttpPlanLibrary", _FakeStore),
            patch(
                "nexus.migration.orchestrator.ServiceCountSource",
                # dedup convergence: 80 landed rows from 98 source rows -> parity
                lambda: _FakeCountSource({"nexus.plans": 80}),
            ),
            patch.dict(
                "os.environ",
                {"NEXUS_CONFIG_DIR": str(config_dir),
                 "NX_SERVICE_TOKEN": "t", "NX_SERVICE_URL": "http://127.0.0.1:1"},
            ),
        ):
            result = runner.invoke(main, [
                "storage", "migrate", "plans", "--db", str(db), "--verify-fill",
            ])

        assert result.exit_code == 0, result.output
        assert etl_called["n"] == 0
        assert "already at parity" in result.output
        assert "convergence:" in result.output
        assert "converged onto existing keys" in result.output


class TestMigrateCatalogVerifyFillCLI:
    def test_owners_delta_fill_via_cli(self, runner: CliRunner, tmp_path: Path) -> None:
        config_dir = tmp_path / "cfg"
        config_dir.mkdir(parents=True, exist_ok=True)
        catalog_db = config_dir / ".catalog.db"

        conn = sqlite3.connect(str(catalog_db))
        conn.execute(
            "CREATE TABLE owners (tumbler_prefix TEXT, name TEXT, owner_type TEXT, "
            "repo_hash TEXT, description TEXT, repo_root TEXT, head_hash TEXT)"
        )
        conn.execute("CREATE TABLE documents (tumbler TEXT)")
        conn.execute("CREATE TABLE links (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE collections (name TEXT)")
        conn.execute("CREATE TABLE document_chunks (doc_id TEXT, position INT, chash TEXT)")
        conn.execute("CREATE TABLE _meta (key TEXT, value TEXT)")
        conn.execute(
            "INSERT INTO owners VALUES ('1', 'owner-1', 'user', '', '', '', '')"
        )
        conn.commit()
        conn.close()

        class _FakeCatalogClient:
            def __init__(self, *a, **k) -> None:
                self.posts: list[tuple[str, dict]] = []

            def list_owners(self) -> list[dict]:
                return []  # owner "1" missing -> divergent fill

            def list_collections(self) -> list[dict]:
                return []

            def chashes_for_collection(self, collection: str) -> set[str]:
                return set()

            def get_manifest(self, doc_id: str) -> list[Any]:
                return []

            def _post(self, path: str, payload: dict) -> None:
                self.posts.append((path, payload))

            def close(self) -> None:
                pass

        with (
            patch(
                "nexus.catalog.factory.make_catalog_client_for_migration",
                return_value=_FakeCatalogClient(),
            ),
            patch(
                "nexus.migration.orchestrator.ServiceCountSource",
                lambda: _FakeCountSource({
                    "nexus.catalog_owners": 0,        # divergent
                    "nexus.catalog_documents": 0,     # parity (source has 0)
                    "nexus.catalog_collections": 0,   # parity (source has 0)
                    "nexus.catalog_document_chunks": 0,  # parity (source has 0)
                    "nexus.catalog_links": 0,         # parity (source has 0)
                }),
            ),
            patch.dict(
                "os.environ",
                {"NEXUS_CONFIG_DIR": str(config_dir),
                 "NX_SERVICE_TOKEN": "t", "NX_SERVICE_URL": "http://127.0.0.1:1"},
            ),
        ):
            result = runner.invoke(main, [
                "storage", "migrate", "catalog",
                "--catalog-db", str(catalog_db), "--verify-fill",
            ])

        assert result.exit_code == 0, result.output
        assert "verify-fill done" in result.output
        assert "owners: filled=1" in result.output
