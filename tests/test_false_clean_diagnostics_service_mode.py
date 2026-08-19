# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Diagnostics must not report a clean bill of health they never checked.

nexus-ingey + nexus-k0luu. One bug class, three call sites: a maintenance or
diagnostic verb reads the LOCAL SQLite unconditionally, so on a migrated
(service-mode) box it inspects a frozen file nothing writes any more and prints
success. The user asked "am I okay?" and was told yes by a check that never ran.

Hal's directive, and the reason these are bugs rather than cosmetic gaps: no
silent fallbacks for data-correctness problems — FAIL LOUD.

The three sites, and what each printed BEFORE:

  nx aspects gc            "examined 0 row(s) ... deleted 0 orphan(s)"
                           HttpDocumentAspectsStore.delete_orphans returned
                           (0, 0), which is indistinguishable downstream from
                           "examined everything, found nothing wrong".
  nx doctor --trim         "Trimmed N rows" after trimming the FROZEN SQLite,
                           while the live PG audit tables grew untrimmed. The
                           engine had exposed the trim endpoints the whole time.
  nx doctor --check-aspect-queue
                           reported the frozen SQLite queue as current. Worse
                           than useless: `nx aspects requeue-failed` points
                           operators at this check for backlog visibility.

Every test below asserts on the SERVICE-mode branch specifically. The sqlite
branches (and the SQLite telemetry/queue/highlights stores behind them) were
DELETED in nexus-i711w Stage 2 sub-stage A — the service branch is now the
only branch, which makes the never-opens-sqlite assertions here the tripwire
that the frozen-file read path stays dead.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from click.testing import CliRunner


@pytest.fixture()
def service_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point every store this module touches at the service backend."""
    for store in ("DOCUMENT_ASPECTS", "TELEMETRY", "ASPECT_QUEUE"):
        monkeypatch.setenv(f"NX_STORAGE_BACKEND_{store}", "service")


# ── nx aspects gc (nexus-ingey a) ───────────────────────────────────────────


class TestAspectsGcRefusesInsteadOfFalseClean:
    def test_gc_refuses_loud_in_service_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, service_mode: None,
    ) -> None:
        """The headline: it must NOT print an orphan count it did not compute."""
        from nexus.commands.aspects import aspects_group

        cat_dir = tmp_path / "catalog"
        cat_dir.mkdir()
        (cat_dir / ".catalog.db").write_bytes(b"")  # exists, so the guard is what fires
        monkeypatch.setattr("nexus.config.catalog_path", lambda: cat_dir)
        monkeypatch.setattr("nexus.config.default_db_path", lambda: tmp_path / "memory.db")

        result = CliRunner().invoke(aspects_group, ["gc"])

        assert result.exit_code != 0, result.output
        combined = (result.output or "") + (result.stderr or "")
        # The false-clean phrasing must be gone entirely.
        assert "orphan(s)" not in combined, (
            f"still printing an orphan count it never computed: {combined!r}"
        )
        assert "retired" in combined
        assert "Traceback" not in combined

    def test_refusal_states_what_IS_guaranteed_and_what_is_not(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, service_mode: None,
    ) -> None:
        """Refusing without explanation would trade one unusable answer for
        another. The message must say BOTH halves of the truth:

        covered      document-delete orphans cannot accumulate — doc_id is
                     FK-bound ON DELETE CASCADE (fk-001-catalog-cross-store).
        NOT covered  the source_uri-keyed sweep this verb performs; that
                     changeset deliberately leaves source_uri unconstrained.

        Claiming only the first half would be a NEW false-clean.
        """
        from nexus.commands.aspects import aspects_group

        cat_dir = tmp_path / "catalog"
        cat_dir.mkdir()
        (cat_dir / ".catalog.db").write_bytes(b"")
        monkeypatch.setattr("nexus.config.catalog_path", lambda: cat_dir)
        monkeypatch.setattr("nexus.config.default_db_path", lambda: tmp_path / "memory.db")

        result = CliRunner().invoke(aspects_group, ["gc"])
        combined = (result.output or "") + (result.stderr or "")
        assert "CASCADE" in combined, "must state what the FK DOES guarantee"
        assert "source_uri" in combined, "must state what is NOT certified clean"
        assert "nexus-ingey" in combined

    def test_store_refuses_rather_than_returning_a_zero_tuple(self) -> None:
        """Defense in depth: the CLI guard fixes one caller, but (0, 0) is
        ambiguous for ANY caller. The store withholds the value instead."""
        from nexus.db.t2.http_document_aspects_store import HttpDocumentAspectsStore

        store = HttpDocumentAspectsStore.__new__(HttpDocumentAspectsStore)
        with pytest.raises(NotImplementedError, match="nexus-ingey"):
            store.delete_orphans(Path("/tmp/x.db"), dry_run=True)


# ── nx doctor --trim (nexus-ingey b) ────────────────────────────────────────


from contextlib import contextmanager


@contextmanager
def _engine_version_probe(release_version: str | None):
    """Stub the nexus-5uoxu dry-run engine-version probe: a resolvable
    endpoint whose /version reports *release_version* (None -> the GET
    raises, the unprobeable arm)."""
    from unittest.mock import MagicMock

    resp = MagicMock()
    resp.json.return_value = {"release_version": release_version}
    resp.raise_for_status.return_value = None
    with patch(
        "nexus.db.service_endpoint.resolve_service_endpoint_with_evidence_gate",
        return_value=("http://127.0.0.1:1", "tk"),
    ), patch(
        "httpx.get",
        side_effect=(RuntimeError("probe down") if release_version is None else None),
        return_value=resp,
    ):
        yield


class TestTrimDryRunEngineGate:
    """nexus-5uoxu belt-and-braces: an engine below the dryRun floor
    silently drops the field and DELETES what the flag claims to preview —
    the client must refuse, fail-closed, before any store call."""

    def _run(self, dry_run: bool, version: str | None):
        from nexus.commands import doctor as doctor_mod

        with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore") as http_store, \
             _engine_version_probe(version):
            http_store.return_value.trim_search_telemetry.return_value = 0
            http_store.return_value.trim_hook_failures.return_value = 0
            runner = CliRunner()
            with runner.isolation():
                doctor_mod._run_trim_telemetry(days=30, dry_run=dry_run)
            return http_store

    def test_below_floor_engine_refuses_the_preview(self, service_mode: None) -> None:
        import click

        with pytest.raises(click.exceptions.Exit):
            self._run(dry_run=True, version="0.1.80")

    def test_unprobeable_engine_refuses_fail_closed(self, service_mode: None) -> None:
        import click

        with pytest.raises(click.exceptions.Exit):
            self._run(dry_run=True, version=None)

    def test_at_floor_engine_previews(self, service_mode: None) -> None:
        store = self._run(dry_run=True, version="0.1.81")
        store.return_value.trim_search_telemetry.assert_called_once_with(
            days=30, dry_run=True)

    def test_refusal_never_reaches_a_store(self, service_mode: None) -> None:
        import click

        from nexus.commands import doctor as doctor_mod

        with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore") as http_store, \
             _engine_version_probe("0.1.71"):
            runner = CliRunner()
            with runner.isolation():
                with pytest.raises(click.exceptions.Exit):
                    doctor_mod._run_trim_telemetry(days=30, dry_run=True)
        http_store.return_value.trim_search_telemetry.assert_not_called()
        http_store.return_value.trim_hook_failures.assert_not_called()

    def test_real_trim_never_probes_or_refuses(self, service_mode: None) -> None:
        """dry_run=False is the pre-existing contract — no probe, no gate."""
        from nexus.commands import doctor as doctor_mod

        with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore") as http_store, \
             patch("httpx.get", side_effect=AssertionError("real trim must not probe /version")):
            http_store.return_value.trim_search_telemetry.return_value = 1
            http_store.return_value.trim_hook_failures.return_value = 1
            runner = CliRunner()
            with runner.isolation():
                doctor_mod._run_trim_telemetry(days=30, dry_run=False)
        http_store.return_value.trim_search_telemetry.assert_called_once_with(
            days=30, dry_run=False)


class TestTrimTelemetryRoutes:
    def test_trim_routes_to_the_service_and_never_opens_sqlite(
        self, service_mode: None,
    ) -> None:
        """It must trim the LIVE tables. The engine has exposed
        POST /v1/telemetry/{search,hook_failures}/trim all along; only this call
        site never routed to it."""
        from nexus.commands import doctor as doctor_mod

        # nexus-i711w Stage 2 sub-stage A2: the SQLite Telemetry class is
        # deleted, so "never opens the frozen SQLite" is asserted at the
        # sqlite3.connect seam (same shape as the aspect-queue tests below).
        with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore") as http_store, \
             patch("sqlite3.connect") as sqlite_connect:
            http_store.return_value.trim_search_telemetry.return_value = 7
            http_store.return_value.trim_hook_failures.return_value = 3
            doctor_mod._run_trim_telemetry(days=30)

        http_store.return_value.trim_search_telemetry.assert_called_once_with(
            days=30, dry_run=False)
        http_store.return_value.trim_hook_failures.assert_called_once_with(
            days=30, dry_run=False)
        sqlite_connect.assert_not_called(), "must not open the frozen SQLite"

    def test_dry_run_previews_both_tables_without_deleting(
        self, service_mode: None,
    ) -> None:
        """``dry_run=True`` must reach BOTH stores — a partial preview
        (search_telemetry previewed, hook_failures for-real deleted, or vice
        versa) would be a worse footgun than the missing feature."""
        from nexus.commands import doctor as doctor_mod

        with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore") as http_store, \
             _engine_version_probe("0.1.81"):
            http_store.return_value.trim_search_telemetry.return_value = 2
            http_store.return_value.trim_hook_failures.return_value = 1
            runner = CliRunner()
            with runner.isolation() as (out, _err, _):
                doctor_mod._run_trim_telemetry(days=30, dry_run=True)
                printed = out.getvalue().decode()

        http_store.return_value.trim_search_telemetry.assert_called_once_with(
            days=30, dry_run=True)
        http_store.return_value.trim_hook_failures.assert_called_once_with(
            days=30, dry_run=True)
        assert "Would trim 2 search_telemetry" in printed, printed
        assert "Would trim 1 hook_failures" in printed, printed
        assert "Trimmed" not in printed, (
            "dry-run output must never read like a completed deletion"
        )

    def test_missing_local_file_does_not_report_nothing_to_trim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, service_mode: None,
    ) -> None:
        """The second false-clean at this site: a service box has no local T2
        file, so the `db_path.exists()` early return printed 'nothing to trim'
        while the real tables were elsewhere and untrimmed."""
        from nexus.commands import doctor as doctor_mod

        monkeypatch.setattr("nexus.config.default_db_path", lambda: tmp_path / "absent.db")
        runner = CliRunner()
        with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore") as http_store:
            http_store.return_value.trim_search_telemetry.return_value = 0
            http_store.return_value.trim_hook_failures.return_value = 0
            with runner.isolation() as (out, _err, _):
                doctor_mod._run_trim_telemetry(days=30)
                printed = out.getvalue().decode()

        assert "nothing to trim" not in printed.lower(), printed
        http_store.return_value.trim_search_telemetry.assert_called_once()


# ── nx doctor --check-aspect-queue (nexus-k0luu) ────────────────────────────


class TestAspectQueueCheckRoutes:
    def test_check_reports_the_live_queue_not_the_frozen_file(
        self, service_mode: None,
    ) -> None:
        from nexus.commands import doctor as doctor_mod

        with patch("nexus.db.t2.http_aspect_queue.HttpAspectQueue") as q, \
             patch("sqlite3.connect") as sqlite_connect:
            q.return_value.pending_count.return_value = 42
            q.return_value.list_failed.return_value = []
            runner = CliRunner()
            with runner.isolation() as (out, _err, _):
                doctor_mod._run_check_aspect_queue()
                printed = out.getvalue().decode()

        assert "42 pending" in printed, printed
        assert "service backend" in printed
        sqlite_connect.assert_not_called(), (
            "service mode must not read the frozen SQLite queue"
        )

    def test_unreachable_service_reports_UNKNOWN_not_zero(
        self, service_mode: None,
    ) -> None:
        """The trap when adding a service branch: falling back to sqlite on a
        transport error would reproduce the exact defect being fixed. Depth
        must read UNKNOWN, never 0."""
        from nexus.commands import doctor as doctor_mod

        with patch("nexus.db.t2.http_aspect_queue.HttpAspectQueue") as q, \
             patch("sqlite3.connect") as sqlite_connect:
            q.side_effect = httpx.ConnectError("refused")
            runner = CliRunner()
            with runner.isolation() as (out, err, _):
                doctor_mod._run_check_aspect_queue()
                printed = out.getvalue().decode() + err.getvalue().decode()

        assert "UNKNOWN" in printed, printed
        assert "0 pending" not in printed
        sqlite_connect.assert_not_called()

    def test_failed_rows_declare_the_missing_last_error(
        self, service_mode: None,
    ) -> None:
        """The service list endpoint does not carry last_error (AspectRepository
        .listFailed does not select LAST_ERROR). Omitting it silently would make
        a failing queue look error-free — the same class again — so the absence
        is stated."""
        from nexus.commands import doctor as doctor_mod
        # nexus-i711w Stage 2 sub-stage A2: QueueRow rehomed to records when
        # the SQLite aspect_extraction_queue module was deleted.
        from nexus.db.t2.records import QueueRow

        row = QueueRow(
            collection="knowledge__x", source_path="/p.pdf",
            content_hash="h", content="", retry_count=3, doc_id="1.1",
        )
        with patch("nexus.db.t2.http_aspect_queue.HttpAspectQueue") as q:
            q.return_value.pending_count.return_value = 0
            q.return_value.list_failed.return_value = [row]
            runner = CliRunner()
            with runner.isolation() as (out, _err, _):
                doctor_mod._run_check_aspect_queue()
                printed = out.getvalue().decode()

        assert "knowledge__x" in printed
        assert "retries 3" in printed
        assert "last_error is not carried" in printed, printed


# ── console twin (nexus-k0luu, second surface) ──────────────────────────────


class TestConsoleHealthTwin:
    def test_console_reports_the_live_queue_not_the_frozen_file(
        self, service_mode: None,
    ) -> None:
        from nexus.console.routes import health as health_mod

        with patch("nexus.db.t2.http_aspect_queue.HttpAspectQueue") as q, \
             patch("sqlite3.connect") as sqlite_connect:
            q.return_value.pending_count.return_value = 5
            q.return_value.list_failed.return_value = [object(), object()]
            data = health_mod._collect_aspect_queue_data()

        assert data["backend"] == "service"
        assert data["pending"] == 5
        assert data["failed_count"] == 2
        sqlite_connect.assert_not_called()

    def test_unreachable_queue_is_not_rendered_as_absent_or_empty(
        self, service_mode: None,
    ) -> None:
        """'I could not reach the queue' must not render identically to 'the
        queue is empty' — nor to 'no queue exists'. Either would be the same
        false-clean in a different shape."""
        from nexus.console.routes import health as health_mod

        with patch("nexus.db.t2.http_aspect_queue.HttpAspectQueue") as q:
            q.side_effect = httpx.ConnectError("refused")
            data = health_mod._collect_aspect_queue_data()

        assert data["present"] is True, "must not claim the queue does not exist"
        assert data["unavailable"] is True
        assert data.get("total") is None and data.get("pending") is None, (
            f"must not report a count it could not obtain: {data}"
        )

    def test_catalog_age_is_marked_frozen_in_service_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, service_mode: None,
    ) -> None:
        """age_seconds is the LOCAL cache's mtime. Post-migration it grows
        forever and reads as a stale catalog while PG is current, so the field
        must declare its scope."""
        from nexus.console.routes import health as health_mod

        cfg = tmp_path / "cfg"
        (cfg / "catalog").mkdir(parents=True)
        (cfg / "catalog" / ".catalog.db").write_bytes(b"")
        # collect_health_data imports nexus_config_dir function-locally, so patch
        # the source module rather than an attribute the module does not hold.
        monkeypatch.setattr("nexus.config.nexus_config_dir", lambda: cfg)
        monkeypatch.setenv("NX_STORAGE_BACKEND_CATALOG", "service")

        with patch("nexus.db.t2.http_aspect_queue.HttpAspectQueue") as q:
            q.return_value.pending_count.return_value = 0
            q.return_value.list_failed.return_value = []
            data = health_mod._collect_health_data()

        assert data["catalog"]["scope"] == "local-cache-frozen", data["catalog"]


# ── Endpoint-unresolvable, not just transport-down (review 2026-07-25) ──────
#
# The first cut of these two fixes caught only httpx.HTTPError. Constructing an
# Http* store RESOLVES the endpoint, and an unresolvable one raises
# ServiceEndpointUnresolvableError — which subclasses RuntimeError and NOT
# httpx.HTTPError. So a missing supervisor lease or an absent NX_SERVICE_TOKEN
# escaped as a traceback out of `nx doctor`, in the commit whose entire purpose
# was to stop doctor misreporting health.
#
# The console twin written in the SAME commit caught (httpx.HTTPError,
# RuntimeError) correctly, which is why these tests assert on the exception
# TYPE rather than on any particular transport failure: the two call sites must
# not be able to drift apart again.


class TestEndpointUnresolvableDegradesCleanly:
    def test_unresolvable_error_is_a_runtime_not_an_httpx_error(self) -> None:
        """Pins the premise. If this ever becomes an httpx.HTTPError subclass,
        the narrower except clauses would have been sufficient after all and
        these guards can be revisited — but until then they are load-bearing."""
        import httpx

        from nexus.db.service_endpoint import ServiceEndpointUnresolvableError

        assert issubclass(ServiceEndpointUnresolvableError, RuntimeError)
        assert not issubclass(ServiceEndpointUnresolvableError, httpx.HTTPError)

    def test_aspect_queue_check_survives_unresolvable_endpoint(
        self, service_mode: None,
    ) -> None:
        from nexus.commands import doctor as doctor_mod
        from nexus.db.service_endpoint import ServiceEndpointUnresolvableError

        with patch("nexus.db.t2.http_aspect_queue.HttpAspectQueue") as q:
            q.side_effect = ServiceEndpointUnresolvableError("no lease, no token")
            runner = CliRunner()
            with runner.isolation() as (out, err, _):
                doctor_mod._run_check_aspect_queue()
                printed = out.getvalue().decode() + err.getvalue().decode()

        assert "UNKNOWN" in printed, printed
        assert "0 pending" not in printed

    def test_trim_reports_UNKNOWN_and_exits_nonzero_on_unresolvable_endpoint(
        self, service_mode: None,
    ) -> None:
        """Trim had NO handling at all. Reporting nothing-trimmed would be the
        false-clean being fixed, so it must say UNKNOWN and exit non-zero —
        a scripted caller must not read a failed trim as a completed one."""
        import click

        from nexus.commands import doctor as doctor_mod
        from nexus.db.service_endpoint import ServiceEndpointUnresolvableError

        with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore") as store:
            store.side_effect = ServiceEndpointUnresolvableError("no lease, no token")
            runner = CliRunner()
            with runner.isolation() as (out, err, _):
                with pytest.raises(click.exceptions.Exit) as exc:
                    doctor_mod._run_trim_telemetry(days=30)
                printed = out.getvalue().decode() + err.getvalue().decode()

        assert exc.value.exit_code == 2
        assert "UNKNOWN" in printed, printed
        assert "Trimmed" not in printed

    def test_trim_survives_a_transport_error_mid_call(
        self, service_mode: None,
    ) -> None:
        """Not only construction: a blip between the two trim calls must not
        leave a partial trim reported as a whole one."""
        import click

        from nexus.commands import doctor as doctor_mod

        with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore") as store:
            store.return_value.trim_search_telemetry.return_value = 5
            store.return_value.trim_hook_failures.side_effect = httpx.ConnectError("boom")
            runner = CliRunner()
            with runner.isolation() as (out, err, _):
                with pytest.raises(click.exceptions.Exit):
                    doctor_mod._run_trim_telemetry(days=30)
                printed = out.getvalue().decode() + err.getvalue().decode()

        assert "UNKNOWN" in printed
        assert "search_telemetry" not in printed, (
            f"must not report the partial success as a completed trim: {printed}"
        )
