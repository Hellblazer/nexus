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

import datetime as _dt
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

import nexus.daemon.aspect_worker_daemon as awd
from click.testing import CliRunner

from nexus.db.t2.http_aspect_queue import AspectTupleCounts

#: The ``aspects`` tuple subspace holds nothing: the queue is table-only.
_NO_TUPLES = AspectTupleCounts(available=0, claimed=0, dead=0)


@pytest.fixture()
def service_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point every store this module touches at the service backend."""
    for store in ("DOCUMENT_ASPECTS", "TELEMETRY", "ASPECT_QUEUE"):
        monkeypatch.setenv(f"NX_STORAGE_BACKEND_{store}", "service")


#: What the engine's ``GET /v1/status`` says once the retired aspect queue table is gone.
_TABLE_GONE = {"aspect_queue_table_present": False, "aspect_queue_rows_retained": 0}


@pytest.fixture(autouse=True)
def _engine_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """The depth checks also ask the engine about the retired queue table (nexus-hbnml);
    unless a test says otherwise the engine answers that it is gone, so no test here reaches
    for a live endpoint."""
    monkeypatch.setattr("nexus.db.http_engine_status.fetch_engine_status", lambda **kw: dict(_TABLE_GONE))


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
            # nexus-gjv9b review fold-in, critique Significant 4: the sweep
            # grew from two live tables to four.
            http_store.return_value.trim_capability_census.return_value = 5
            http_store.return_value.trim_routing_events.return_value = 9
            doctor_mod._run_trim_telemetry(days=30)

        http_store.return_value.trim_search_telemetry.assert_called_once_with(
            days=30, dry_run=False)
        http_store.return_value.trim_hook_failures.assert_called_once_with(
            days=30, dry_run=False)
        http_store.return_value.trim_capability_census.assert_called_once_with(
            days=30, dry_run=False)
        # routing_events falls back to its own shorter 7d default when the
        # caller leaves `days` at the shared default (nexus-gjv9b review
        # fold-in round 3, critique Significant 3).
        http_store.return_value.trim_routing_events.assert_called_once_with(
            days=7, dry_run=False)
        sqlite_connect.assert_not_called(), "must not open the frozen SQLite"

    def test_dry_run_previews_both_tables_without_deleting(
        self, service_mode: None,
    ) -> None:
        """``dry_run=True`` must reach ALL FOUR stores (nexus-gjv9b review
        fold-in, critique Significant 4, grew this from two) — a partial
        preview (one table previewed, another for-real deleted) would be a
        worse footgun than the missing feature."""
        from nexus.commands import doctor as doctor_mod

        with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore") as http_store, \
             _engine_version_probe("0.1.81"):
            http_store.return_value.trim_search_telemetry.return_value = 2
            http_store.return_value.trim_hook_failures.return_value = 1
            http_store.return_value.trim_capability_census.return_value = 4
            http_store.return_value.trim_routing_events.return_value = 6
            runner = CliRunner()
            with runner.isolation() as (out, _err, _):
                doctor_mod._run_trim_telemetry(days=30, dry_run=True)
                printed = out.getvalue().decode()

        http_store.return_value.trim_search_telemetry.assert_called_once_with(
            days=30, dry_run=True)
        http_store.return_value.trim_hook_failures.assert_called_once_with(
            days=30, dry_run=True)
        http_store.return_value.trim_capability_census.assert_called_once_with(
            days=30, dry_run=True)
        http_store.return_value.trim_routing_events.assert_called_once_with(
            days=7, dry_run=True)
        assert "Would trim 2 search_telemetry" in printed, printed
        assert "Would trim 1 hook_failures" in printed, printed
        assert "Would trim 4 capability_census" in printed, printed
        assert "Would trim 6 routing_events" in printed, printed
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
            q.return_value.tuple_counts.return_value = AspectTupleCounts(42, 0, 0)
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

    def test_zero_dead_tasks_does_not_signal_failure(
        self, service_mode: None,
    ) -> None:
        """A healthy queue (pending rows only, no failures) must stay a
        clean, non-raising report -- the fix must not turn every backlog
        into a failure, only a genuine failed-row count."""
        import click

        from nexus.commands import doctor as doctor_mod

        with patch("nexus.db.t2.http_aspect_queue.HttpAspectQueue") as q:
            q.return_value.tuple_counts.return_value = AspectTupleCounts(7, 0, 0)
            runner = CliRunner()
            with runner.isolation() as (out, err, _):
                exit_code = None
                try:
                    doctor_mod._run_check_aspect_queue()
                except click.exceptions.Exit as exc:
                    exit_code = exc.exit_code
                printed = out.getvalue().decode() + err.getvalue().decode()

        assert exit_code is None
        assert "7 pending" in printed
        assert "FAIL" not in printed


class TestRetiredQueueTableIsNeverACleanZero:
    """nexus-hbnml: a table the engine kept because it held rows the start-up drain could not
    move is invisible to the tuple-queue depth lines, so ``--check-aspect-queue`` and the
    console must say so, with a remedy, and never print a clean report while rows remain."""

    @staticmethod
    def _run(status, monkeypatch) -> tuple[str, int | None]:  # noqa: ANN001
        import click

        from nexus.commands import doctor as doctor_mod

        monkeypatch.setattr("nexus.db.http_engine_status.fetch_engine_status", lambda **kw: status)
        with patch("nexus.db.t2.http_aspect_queue.HttpAspectQueue") as q:
            q.return_value.tuple_counts.return_value = AspectTupleCounts(0, 0, 0)
            runner = CliRunner()
            with runner.isolation() as (out, err, _):
                exit_code = None
                try:
                    doctor_mod._run_check_aspect_queue()
                except click.exceptions.Exit as exc:
                    exit_code = exc.exit_code
                return out.getvalue().decode() + err.getvalue().decode(), exit_code

    def test_retained_rows_fail_the_check_with_a_remedy(self, service_mode: None, monkeypatch) -> None:
        printed, exit_code = self._run({
            "aspect_queue_table_present": True, "aspect_queue_rows_retained": 3,
            "aspect_queue_rows_retained_reasons": {"body_over_cap": 2, "unknown_status": 1},
        }, monkeypatch)
        assert exit_code == 1, printed
        assert "3 row(s) are stranded" in printed and "body_over_cap=2" in printed
        assert "nx enrich aspects" in printed and "migration-runbook.md#the-retired-aspect-queue-tuples-rung-and-the-aspect-queue-table" in printed
        assert "0 pending, 0 failed" in printed, "premise: the tuple depth alone reads clean"

    def test_an_uncounted_table_warns_and_is_not_clean(self, service_mode: None, monkeypatch) -> None:
        printed, exit_code = self._run(
            {"aspect_queue_table_present": True, "aspect_queue_rows_retained": -1}, monkeypatch)
        assert exit_code is None
        assert "could not count" in printed and "nx enrich aspects" in printed

    def test_an_unreadable_status_is_unknown_not_clean(self, service_mode: None, monkeypatch) -> None:
        printed, exit_code = self._run(None, monkeypatch)
        assert exit_code is None
        assert "retired aspect_extraction_queue table: UNKNOWN" in printed

    def test_an_engine_without_the_field_is_unknown_not_clean(self, service_mode: None, monkeypatch) -> None:
        printed, _ = self._run({"version": "x"}, monkeypatch)
        assert "UNKNOWN" in printed

    def test_a_present_empty_table_and_a_gone_table_are_clean(self, service_mode: None, monkeypatch) -> None:
        printed, exit_code = self._run(
            {"aspect_queue_table_present": True, "aspect_queue_rows_retained": 0}, monkeypatch)
        assert exit_code is None and "present and empty" in printed and "FAIL" not in printed
        printed, exit_code = self._run(dict(_TABLE_GONE), monkeypatch)
        assert exit_code is None and "retired aspect_extraction_queue" not in printed

    def test_the_console_card_carries_the_state_and_the_remedy(self, service_mode: None, monkeypatch) -> None:
        from nexus.console.routes import health as health_mod

        monkeypatch.setattr("nexus.db.http_engine_status.fetch_engine_status", lambda **kw: {
            "aspect_queue_table_present": True, "aspect_queue_rows_retained": 4,
            "aspect_queue_rows_retained_reasons": {"error": 4},
        })
        with patch("nexus.db.t2.http_aspect_queue.HttpAspectQueue") as q:
            q.return_value.tuple_counts.return_value = AspectTupleCounts(0, 0, 0)
            data = health_mod._collect_aspect_queue_data()
        assert data["total"] == 0, "premise: the tuple depth reads zero"
        card = data["retired_table"]
        assert card["state"] == "retained" and card["rows"] == 4 and card["reasons"] == {"error": 4}
        assert "nx enrich aspects" in card["remedy"]

    @pytest.mark.t2_tenant
    def test_the_console_card_renders_stranded_rows_as_a_warning(self, monkeypatch) -> None:  # noqa: ANN001
        from fastapi.testclient import TestClient

        from nexus.console.app import create_app

        def card(state: str) -> dict:
            return {"present": True, "backend": "service", "total": 0, "pending": 0, "failed_count": 0,
                    "oldest_pending": None,
                    "retired_table": {"state": state, "rows": 4, "reasons": {"error": 4}, "remedy": "THE-REMEDY"}}

        client = TestClient(create_app())
        monkeypatch.setattr("nexus.console.routes.health._collect_aspect_queue_data", lambda: card("retained"))
        html = client.get("/health/refresh").text
        assert "4 row(s) stranded" in html and "THE-REMEDY" in html and "error=4" in html
        monkeypatch.setattr("nexus.console.routes.health._collect_aspect_queue_data", lambda: card("gone"))
        html = client.get("/health/refresh").text
        assert "stranded" not in html and "THE-REMEDY" not in html, "a gone table adds nothing to the card"


class TestAspectQueueIdleWarning:
    """The spawner declines to start the aspect worker without `claude` on PATH
    and logs it once per process; doctor is the durable signal. A WARNING, not
    a failure, and silent when nothing is pending (a virgin box stays clean)."""

    @staticmethod
    def _run(pending: int, claude_on_path: bool, monkeypatch) -> tuple[str, int | None]:
        import click

        import nexus.daemon.aspect_worker_daemon as awd
        from nexus.commands import doctor as doctor_mod

        monkeypatch.setattr(awd, "_claude_available", lambda: claude_on_path)
        with patch("nexus.db.t2.http_aspect_queue.HttpAspectQueue") as q:
            q.return_value.tuple_counts.return_value = AspectTupleCounts(pending, 0, 0)
            runner = CliRunner()
            with runner.isolation() as (out, err, _):
                exit_code = None
                try:
                    doctor_mod._run_check_aspect_queue()
                except click.exceptions.Exit as exc:
                    exit_code = exc.exit_code
                return out.getvalue().decode() + err.getvalue().decode(), exit_code

    def test_pending_rows_and_no_claude_warns_without_failing(
        self, service_mode: None, monkeypatch,
    ) -> None:
        printed, exit_code = self._run(5, False, monkeypatch)
        assert "aspect extraction is idle: 5 pending, `claude` not on PATH" in printed, printed
        assert exit_code is None, "a warning, not a failure"
        assert "✗" not in printed and "FAIL:" not in printed, printed

    def test_nothing_pending_does_not_warn_even_without_claude(
        self, service_mode: None, monkeypatch,
    ) -> None:
        printed, exit_code = self._run(0, False, monkeypatch)
        assert "idle" not in printed and "WARN" not in printed, printed
        assert exit_code is None

    def test_pending_rows_with_claude_and_a_running_worker_do_not_warn(
        self, service_mode: None, monkeypatch,
    ) -> None:
        monkeypatch.setattr(awd, "aspect_worker_running", lambda *a, **k: True)
        printed, exit_code = self._run(5, True, monkeypatch)
        assert "idle" not in printed and "WARN" not in printed, printed
        assert exit_code is None

    def test_pending_rows_with_claude_and_no_worker_warn_with_the_remedy(
        self, service_mode: None, monkeypatch,
    ) -> None:
        """A worker leaves after an hour without an engine (nexus-wl9pk) and the
        next store or index starts one; a backlog from before the outage sits
        until then, so doctor says so and names the remedy."""
        monkeypatch.setattr(awd, "aspect_worker_running", lambda *a, **k: False)
        printed, exit_code = self._run(5, True, monkeypatch)
        assert "aspect extraction is idle: 5 pending and no aspect worker is running" in printed, printed
        assert "nx daemon aspect-worker start" in printed, printed
        assert "this machine's worker leases only" in printed, (
            "the WARN must say it cannot see a worker on another machine against the same cloud tenant"
        )
        assert exit_code is None, "a warning, not a failure"
        assert "FAIL:" not in printed, printed

    def test_nothing_pending_and_no_worker_does_not_warn(
        self, service_mode: None, monkeypatch,
    ) -> None:
        monkeypatch.setattr(awd, "aspect_worker_running", lambda *a, **k: False)
        printed, exit_code = self._run(0, True, monkeypatch)
        assert "idle" not in printed and "WARN" not in printed, printed

    def test_a_lease_that_cannot_be_read_does_not_warn(
        self, service_mode: None, monkeypatch,
    ) -> None:
        def _boom(*a, **k):
            raise OSError("lease directory unreadable")

        monkeypatch.setattr(awd, "aspect_worker_running", _boom)
        printed, exit_code = self._run(5, True, monkeypatch)
        assert "WARN" not in printed, printed
        assert exit_code is None


# ── The queue's depth, read from the aspects tuple census (RDR-235 F18) ─────
#
# available is pending, dead is failed, and claimed (in flight) is neither. The table side
# left with the table (nexus-hbnml), so the census is the only input.


def _run_doctor_check(q, *, tuples) -> tuple[str, int | None]:
    import click

    from nexus.commands import doctor as doctor_mod

    if isinstance(tuples, BaseException):
        q.return_value.tuple_counts.side_effect = tuples
    else:
        q.return_value.tuple_counts.return_value = tuples
    runner = CliRunner()
    with runner.isolation() as (out, err, _):
        exit_code = None
        try:
            doctor_mod._run_check_aspect_queue()
        except click.exceptions.Exit as exc:
            exit_code = exc.exit_code
        return out.getvalue().decode() + err.getvalue().decode(), exit_code


class TestAspectQueueDoctorCountsBothSides:
    def test_available_is_pending_dead_is_failed_and_claimed_is_neither(
        self, service_mode: None,
    ) -> None:
        with patch("nexus.db.t2.http_aspect_queue.HttpAspectQueue") as q:
            printed, exit_code = _run_doctor_check(q, tuples=AspectTupleCounts(4, 5, 2))
        assert "4 pending, 2 failed" in printed, printed
        assert exit_code == 1, "a nonzero dead count still fails the check"
        assert "FAIL: 2 failed" in printed, printed

    def test_claimed_alone_is_not_pending(self, service_mode: None) -> None:
        with patch("nexus.db.t2.http_aspect_queue.HttpAspectQueue") as q:
            printed, exit_code = _run_doctor_check(q, tuples=AspectTupleCounts(0, 9, 0))
        assert "0 pending, 0 failed" in printed, printed
        assert exit_code is None

    def test_dead_tuple_tasks_alone_fail_the_check(self, service_mode: None) -> None:
        with patch("nexus.db.t2.http_aspect_queue.HttpAspectQueue") as q:
            printed, exit_code = _run_doctor_check(q, tuples=AspectTupleCounts(0, 0, 2))
        assert exit_code == 1, printed
        assert "2 failed" in printed, printed
        assert "nx tuple stats aspects" in printed, "dead tasks need a pointer to where they are"

    def test_available_tuple_tasks_are_pending_and_not_a_failure(
        self, service_mode: None,
    ) -> None:
        with patch("nexus.db.t2.http_aspect_queue.HttpAspectQueue") as q:
            printed, exit_code = _run_doctor_check(q, tuples=AspectTupleCounts(1000, 0, 0))
        assert "1000 pending, 0 failed" in printed, printed
        assert exit_code is None, "a routine bulk backlog is not a failure"

    def test_unreadable_tuple_side_is_UNKNOWN_never_zero(self, service_mode: None) -> None:
        with patch("nexus.db.t2.http_aspect_queue.HttpAspectQueue") as q:
            printed, exit_code = _run_doctor_check(q, tuples=httpx.ConnectError("refused"))
        assert "UNKNOWN" in printed, printed
        # No figure at all: a count with a guessed zero would be the false-clean.
        assert "pending" not in printed and "failed" not in printed, printed
        assert exit_code is None, "an unreadable queue is UNKNOWN, not a failure"

    def test_idle_warning_counts_the_tuple_side(
        self, service_mode: None, monkeypatch,
    ) -> None:
        import nexus.daemon.aspect_worker_daemon as awd

        monkeypatch.setattr(awd, "_claude_available", lambda: False)
        with patch("nexus.db.t2.http_aspect_queue.HttpAspectQueue") as q:
            printed, exit_code = _run_doctor_check(q, tuples=AspectTupleCounts(6, 0, 0))
        assert "aspect extraction is idle: 6 pending" in printed, printed
        assert exit_code is None

    def test_an_absent_subspace_is_zero(self, service_mode: None) -> None:
        """tuple_counts() answers zeros for a subspace that does not exist yet: nothing is
        queued there, so zero is true and is not UNKNOWN."""
        with patch("nexus.db.t2.http_aspect_queue.HttpAspectQueue") as q:
            printed, exit_code = _run_doctor_check(q, tuples=_NO_TUPLES)
        assert "0 pending, 0 failed" in printed, printed
        assert "UNKNOWN" not in printed
        assert exit_code is None


# ── console twin (nexus-k0luu, second surface) ──────────────────────────────


class TestConsoleHealthTwin:
    def test_console_reports_the_live_queue_not_the_frozen_file(
        self, service_mode: None,
    ) -> None:
        from nexus.console.routes import health as health_mod

        with patch("nexus.db.t2.http_aspect_queue.HttpAspectQueue") as q, \
             patch("sqlite3.connect") as sqlite_connect:
            q.return_value.tuple_counts.return_value = AspectTupleCounts(5, 0, 2)
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

    @pytest.mark.t2_tenant
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
        # Redirect via env, NOT monkeypatch.setattr("nexus.config.
        # nexus_config_dir", ...): nexus_config_dir() reads NEXUS_CONFIG_DIR
        # at call time, so setenv reaches every consumer — including modules
        # FIRST-imported during this test. The setattr form leaked: the
        # health run below first-imports nexus.gc_purge_marker (deferred
        # import in _check_gc_audit_non_empty_after_purge), whose module-level
        # `from nexus.config import nexus_config_dir` captured the patch
        # lambda BY VALUE; teardown restored nexus.config but not that
        # binding, pinning every later purge-marker read/write in the process
        # to this test's tmp dir (broke TestGcPurgeMarker on CI shard 3,
        # v7.11.0 release PR #1467).
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg))
        monkeypatch.setenv("NX_STORAGE_BACKEND_CATALOG", "service")

        with patch("nexus.db.t2.http_aspect_queue.HttpAspectQueue") as q:
            q.return_value.tuple_counts.return_value = _NO_TUPLES
            data = health_mod._collect_health_data()

        assert data["catalog"]["scope"] == "local-cache-frozen", data["catalog"]


class TestConsoleCountsBothSides:
    def test_pending_is_available_failed_is_dead_claimed_is_neither(
        self, service_mode: None,
    ) -> None:
        from nexus.console.routes import health as health_mod

        with patch("nexus.db.t2.http_aspect_queue.HttpAspectQueue") as q:
            q.return_value.tuple_counts.return_value = AspectTupleCounts(4, 5, 2)
            data = health_mod._collect_aspect_queue_data()

        assert data["pending"] == 4
        assert data["failed_count"] == 2, "the card keys its warn on failed_count"
        assert data["total"] == 6, "total = pending + failed; claimed is in flight"
        assert data["backend"] == "service"

    def test_unreadable_tuple_side_is_unavailable_not_a_smaller_count(
        self, service_mode: None,
    ) -> None:
        from nexus.console.routes import health as health_mod

        with patch("nexus.db.t2.http_aspect_queue.HttpAspectQueue") as q:
            q.return_value.tuple_counts.side_effect = httpx.ConnectError("refused")
            data = health_mod._collect_aspect_queue_data()

        assert data["present"] is True and data["unavailable"] is True
        assert data.get("total") is None and data.get("pending") is None, data


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

    @pytest.mark.t2_tenant
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

    @pytest.mark.t2_tenant
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


# ── nx doctor --check-index-failures (nexus-nukn3) ──────────────────────────


class TestIndexFailuresCheckRoutes:
    """Non-vacuity: this check must fire loudly on a seeded failure row --
    the exact nexus-fylxo trap named on this bead (a durable failure queue
    whose reader never raises reproduces the aspect-queue check's original
    defect for a second queue). Written FAIL-FIRST rather than retrofitted.

    Fold-in round 1 (T2 critique-nexus-nukn3-410720f6a [24569], Critical
    finding): the check now scopes its gate to the LATEST run and exempts
    a stale one, rather than gating on the all-time total.

    Fold-in round 2 (T2 critique-nexus-nukn3-37262c4a1 [24596], Critical
    finding): the gate is now scoped to the latest UNACKNOWLEDGED run --
    a durable ``nx index failures --acknowledge`` adjudication is excluded
    from the gate so a recurring failure for an acknowledged file does not
    gate forever just because every re-index mints a fresh run_id. These
    tests assert the resulting THREE-query shape: an unscoped all-time
    call, an unscoped unacknowledged-only call (finds the latest
    unacknowledged run's id), and a run_id + unacknowledged_only scoped
    call for the exact gating count."""

    @staticmethod
    def _scoped_mock(
        all_time: dict,
        unacked_all_time: dict | None = None,
        by_run_id_unacked: dict | None = None,
    ):
        """A list_index_failures side_effect routed by (run_id,
        unacknowledged_only): the plain unscoped call returns *all_time*;
        the unscoped unacknowledged_only call returns *unacked_all_time*
        (defaults to *all_time* when every failure is unacknowledged, the
        common test shape); a run_id + unacknowledged_only scoped call
        returns *by_run_id_unacked*."""
        def _side_effect(*, run_id: str = "", days: int = 0, limit: int = 100,
                          unacknowledged_only: bool = False):
            if run_id:
                assert unacknowledged_only, (
                    "the run-scoped gate call must always pass "
                    "unacknowledged_only=True"
                )
                assert by_run_id_unacked is not None, (
                    f"unexpected scoped call with run_id={run_id}"
                )
                return by_run_id_unacked
            if unacknowledged_only:
                return unacked_all_time if unacked_all_time is not None else all_time
            return all_time
        return _side_effect

    def test_zero_failures_does_not_signal_failure(
        self, service_mode: None,
    ) -> None:
        import click

        from nexus.commands import doctor as doctor_mod

        with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore") as store:
            store.return_value.list_index_failures.side_effect = self._scoped_mock(
                {"rows": [], "total": 0, "oldest_occurred_at": ""},
            )
            runner = CliRunner()
            with runner.isolation() as (out, err, _):
                exit_code = None
                try:
                    doctor_mod._run_check_index_failures()
                except click.exceptions.Exit as exc:
                    exit_code = exc.exit_code
                printed = out.getvalue().decode() + err.getvalue().decode()

        assert exit_code is None
        assert "0 recorded failure" in printed
        assert "FAIL" not in printed

    def test_fresh_latest_run_failure_signals_failure_loudly(
        self, service_mode: None,
    ) -> None:
        """THE motivating case: a genuine, RECENT backlog must raise Exit(1)
        with a ✗/FAIL: marker, never read as healthy (nexus-fylxo class)."""
        import click

        from nexus.commands import doctor as doctor_mod

        now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
        row = {
            "run_id": "run-1", "file_path": "/repo/broken.pdf",
            "error_class": "UnextractableContentError",
            "error": "produced empty output",
            "occurred_at": now_iso,
        }
        with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore") as store:
            store.return_value.list_index_failures.side_effect = self._scoped_mock(
                all_time={"rows": [row], "total": 1, "oldest_occurred_at": now_iso},
                by_run_id_unacked={"rows": [row], "total": 1, "oldest_occurred_at": now_iso},
            )
            runner = CliRunner()
            with runner.isolation() as (out, err, _):
                exit_code = None
                try:
                    doctor_mod._run_check_index_failures()
                except click.exceptions.Exit as exc:
                    exit_code = exc.exit_code
                printed = out.getvalue().decode() + err.getvalue().decode()

        assert exit_code == 1
        assert "✗" in printed or "FAIL:" in printed, printed
        assert "/repo/broken.pdf" in printed
        assert "UnextractableContentError" in printed

    def test_stale_older_run_failure_alone_does_not_signal_failure(
        self, service_mode: None,
    ) -> None:
        """THE Critical fix's non-vacuity proof: a backlog whose only
        failure is older than the staleness window must NOT gate the
        sweep -- only a fresh one does. Before this fold-in, this exact
        scenario (one permanent failure, however old) failed the default
        sweep forever."""
        import click

        from nexus.commands import doctor as doctor_mod

        old_iso = (
            _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=60)
        ).isoformat()
        row = {
            "run_id": "run-old", "file_path": "/repo/ancient.pdf",
            "error_class": "UnextractableContentError",
            "error": "encrypted PDF", "occurred_at": old_iso,
        }
        with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore") as store:
            store.return_value.list_index_failures.side_effect = self._scoped_mock(
                {"rows": [row], "total": 1, "oldest_occurred_at": old_iso},
            )
            runner = CliRunner()
            with runner.isolation() as (out, err, _):
                exit_code = None
                try:
                    doctor_mod._run_check_index_failures()
                except click.exceptions.Exit as exc:
                    exit_code = exc.exit_code
                printed = out.getvalue().decode() + err.getvalue().decode()

        assert exit_code is None, printed
        assert "FAIL" not in printed
        assert "1 recorded failure" in printed, printed
        assert "no longer gates" in printed

    def test_all_failures_acknowledged_does_not_signal_failure(
        self, service_mode: None,
    ) -> None:
        """Fold-in round 2's motivating case: a FRESH, recurring failure
        that is fully covered by a durable acknowledgment must not gate,
        even though the all-time total is nonzero and the failure recurred
        just now (the exact shape a stale-only fix cannot self-heal)."""
        import click

        from nexus.commands import doctor as doctor_mod

        now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
        row = {
            "run_id": "run-9", "file_path": "/repo/known-encrypted.pdf",
            "error_class": "UnextractableContentError",
            "error": "encrypted", "occurred_at": now_iso,
        }
        with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore") as store:
            store.return_value.list_index_failures.side_effect = self._scoped_mock(
                all_time={"rows": [row], "total": 2, "oldest_occurred_at": now_iso},
                unacked_all_time={"rows": [], "total": 0, "oldest_occurred_at": ""},
            )
            runner = CliRunner()
            with runner.isolation() as (out, err, _):
                exit_code = None
                try:
                    doctor_mod._run_check_index_failures()
                except click.exceptions.Exit as exc:
                    exit_code = exc.exit_code
                printed = out.getvalue().decode() + err.getvalue().decode()

        assert exit_code is None, printed
        assert "FAIL" not in printed
        assert "2 recorded failure" in printed, printed
        assert "acknowledged" in printed

    def test_unreachable_service_reports_UNKNOWN_not_zero(
        self, service_mode: None,
    ) -> None:
        from nexus.commands import doctor as doctor_mod

        with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore") as store:
            store.side_effect = httpx.ConnectError("refused")
            runner = CliRunner()
            with runner.isolation() as (out, err, _):
                doctor_mod._run_check_index_failures()
                printed = out.getvalue().decode() + err.getvalue().decode()

        assert "UNKNOWN" in printed, printed
        assert "0 recorded failure" not in printed

    def test_active_acknowledgments_are_named_in_the_footnote(
        self, service_mode: None,
    ) -> None:
        """Round-4 fold-in (critique [24621] item 1): the doctor footnote
        must name the ACTIVE acknowledgment count and point at the list
        surface, alongside the pre-existing "N are acknowledged" line."""
        import click

        from nexus.commands import doctor as doctor_mod

        now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
        row = {
            "run_id": "run-9", "file_path": "/repo/known-encrypted.pdf",
            "error_class": "UnextractableContentError",
            "error": "encrypted", "occurred_at": now_iso,
        }
        with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore") as store:
            store.return_value.list_index_failures.side_effect = self._scoped_mock(
                all_time={"rows": [row], "total": 2, "oldest_occurred_at": now_iso},
                unacked_all_time={"rows": [], "total": 0, "oldest_occurred_at": ""},
            )
            store.return_value.list_index_failure_acknowledgments.return_value = {
                "rows": [{
                    "file_path": "/repo/known-encrypted.pdf",
                    "error_class": "UnextractableContentError",
                    "reason": "known limitation", "created_at": now_iso,
                }],
                "total": 1,
            }
            runner = CliRunner()
            with runner.isolation() as (out, err, _):
                exit_code = None
                try:
                    doctor_mod._run_check_index_failures()
                except click.exceptions.Exit as exc:
                    exit_code = exc.exit_code
                printed = out.getvalue().decode() + err.getvalue().decode()

        assert exit_code is None, printed
        assert "1 active acknowledgment(s)" in printed, printed
        assert "nx index failures --acks" in printed, printed

    def test_ack_lookup_failure_does_not_break_the_footnote(
        self, service_mode: None,
    ) -> None:
        """The footnote's own lookup is advisory: a transport error there
        must never turn the (already-clean) all-time summary into a hard
        failure, mirroring the outer store-construction posture."""
        import click

        from nexus.commands import doctor as doctor_mod

        now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
        row = {
            "run_id": "run-9", "file_path": "/repo/known-encrypted.pdf",
            "error_class": "UnextractableContentError",
            "error": "encrypted", "occurred_at": now_iso,
        }
        with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore") as store:
            store.return_value.list_index_failures.side_effect = self._scoped_mock(
                all_time={"rows": [row], "total": 2, "oldest_occurred_at": now_iso},
                unacked_all_time={"rows": [], "total": 0, "oldest_occurred_at": ""},
            )
            store.return_value.list_index_failure_acknowledgments.side_effect = (
                httpx.ConnectError("refused")
            )
            runner = CliRunner()
            with runner.isolation() as (out, err, _):
                exit_code = None
                try:
                    doctor_mod._run_check_index_failures()
                except click.exceptions.Exit as exc:
                    exit_code = exc.exit_code
                printed = out.getvalue().decode() + err.getvalue().decode()

        assert exit_code is None, printed
        assert "FAIL" not in printed
        assert "acknowledged" in printed

    def test_pre_emptive_acknowledgment_is_named_with_zero_failures(
        self, service_mode: None,
    ) -> None:
        """Round-5 fold-in (code-review [24635] item 3): an acknowledgment
        created AHEAD of any failure ever being recorded for that file/class
        (an operator pre-emptively exempting a known-bad corpus) must still
        be named -- the old placement inside the post-return branch never
        reached this, since total_all_time == 0 returns before the ack
        lookup ran at all."""
        import click

        from nexus.commands import doctor as doctor_mod

        with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore") as store:
            store.return_value.list_index_failures.side_effect = self._scoped_mock(
                {"rows": [], "total": 0, "oldest_occurred_at": ""},
            )
            store.return_value.list_index_failure_acknowledgments.return_value = {
                "rows": [{
                    "file_path": "", "error_class": "ScannedPdfNoOcrError",
                    "reason": "known systemic issue",
                    "created_at": "2026-09-05T00:00:00Z",
                }],
                "total": 1,
            }
            runner = CliRunner()
            with runner.isolation() as (out, err, _):
                exit_code = None
                try:
                    doctor_mod._run_check_index_failures()
                except click.exceptions.Exit as exc:
                    exit_code = exc.exit_code
                printed = out.getvalue().decode() + err.getvalue().decode()

        assert exit_code is None, printed
        assert "0 recorded failure" in printed, printed
        assert "1 active acknowledgment(s)" in printed, printed
        assert "nx index failures --acks" in printed, printed

    def test_surviving_acknowledgment_is_named_when_no_current_failure_is_covered(
        self, service_mode: None,
    ) -> None:
        """Round-5 fold-in (code-review [24635] item 3): an acknowledgment
        whose own failure has aged out or been cleared, while an UNRELATED
        fresh failure exists, must still be named -- total_all_time equals
        total_unacknowledged here (nothing CURRENT is covered), so the old
        placement inside `if total_all_time > total_unacknowledged` never
        reached the ack lookup in this shape either."""
        import click

        from nexus.commands import doctor as doctor_mod

        now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
        unrelated_row = {
            "run_id": "run-1", "file_path": "/repo/unrelated.pdf",
            "error_class": "UnextractableContentError",
            "error": "boom", "occurred_at": now_iso,
        }
        with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore") as store:
            store.return_value.list_index_failures.side_effect = self._scoped_mock(
                all_time={"rows": [unrelated_row], "total": 1, "oldest_occurred_at": now_iso},
                by_run_id_unacked={"rows": [unrelated_row], "total": 1, "oldest_occurred_at": now_iso},
            )
            store.return_value.list_index_failure_acknowledgments.return_value = {
                "rows": [{
                    "file_path": "/repo/long-since-fixed.pdf",
                    "error_class": "UnextractableContentError",
                    "reason": "fixed upstream", "created_at": now_iso,
                }],
                "total": 1,
            }
            runner = CliRunner()
            with runner.isolation() as (out, err, _):
                exit_code = None
                try:
                    doctor_mod._run_check_index_failures()
                except click.exceptions.Exit as exc:
                    exit_code = exc.exit_code
                printed = out.getvalue().decode() + err.getvalue().decode()

        # The unrelated failure still gates (it is genuinely unacknowledged).
        assert exit_code == 1, printed
        assert "1 active acknowledgment(s)" in printed, printed
        assert "nx index failures --acks" in printed, printed
