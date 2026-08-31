# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for ``nx catalog purge-trash`` (nexus-3ck2g, client half).

Mirrors ``tests/test_catalog_reconcile_stale.py``'s fake-based style — no
real catalog / engine substrate. The engine's own ``POST
/v1/catalog/purge-trash`` route is the sibling (Java) half of nexus-3ck2g,
built concurrently; these tests exercise only the client-side wiring: the
CLI verb's dry-run/confirm gate, the ``HttpCatalogClient.purge_trash``
wire contract, and the ``_ServiceCatalogWriter`` whitelist admission.
"""
from __future__ import annotations

import json

import httpx
import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.catalog import factory
from nexus.catalog.catalog_protocol import CATALOG_WRITE_OPS, CatalogReader
from nexus.commands.catalog_cmds import purge_trash as purge_trash_mod


# ── Fakes ────────────────────────────────────────────────────────────────


class _FakeWriter:
    def __init__(self, result: dict | None = None, raise_exc: Exception | None = None):
        self.calls: list[dict] = []
        self.closed = False
        # nexus-8j1zx fix round: field names/shape now match the REAL wire
        # contract (T1 2fbc12df design of record; confirmed against
        # CatalogRepository.purgeTrashPreview/purgeTrash) — a flat dict with
        # documents_purged (age-gated) and chunks_<dim>_stranded (age-
        # independent), not the previous "aged_tombstones"/nested
        # "stranded_chunks" shape that never existed on the wire.
        self._result = result if result is not None else {
            "dry_run": True,
            "documents_purged": 3,
            "chunks_384_stranded": 0,
            "chunks_768_stranded": 12,
            "chunks_1024_stranded": 0,
        }
        self._raise = raise_exc

    def purge_trash(self, older_than_days: int, dry_run: bool) -> dict:
        self.calls.append({"older_than_days": older_than_days, "dry_run": dry_run})
        if self._raise is not None:
            raise self._raise
        return dict(self._result, dry_run=dry_run)

    def close(self) -> None:
        self.closed = True


def _writer_factory_raises():
    def _boom():
        raise AssertionError("catalog writer factory must not be called")
    return _boom


def _patch_writer(monkeypatch, writer):
    monkeypatch.setattr("nexus.commands.catalog._get_catalog_writer", lambda: writer)


def _http_status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://test/v1/catalog/purge-trash")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


# ── CLI: dry-run default ───────────────────────────────────────────────────


class TestDryRunDefault:
    def test_default_invocation_calls_client_with_dry_run_true(self, monkeypatch):
        writer = _FakeWriter()
        _patch_writer(monkeypatch, writer)

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "purge-trash"])

        assert result.exit_code == 0, result.output
        assert writer.calls == [{"older_than_days": 30, "dry_run": True}]
        assert writer.closed

    def test_default_invocation_prints_counts_and_dry_run_notice(self, monkeypatch):
        writer = _FakeWriter()
        _patch_writer(monkeypatch, writer)

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "purge-trash"])

        assert result.exit_code == 0, result.output
        assert "documents_purged" in result.output
        assert "age-gated" in result.output.lower()
        assert "chunks_768_stranded: 12" in result.output
        # nexus-kcm6c: the output claimed the chunk sweep was NOT age-gated
        # for two weeks after catalog-026 age-gated it — the corrected text
        # names the grace-window protection and must never regress to the
        # old claim.
        assert "not age-gated" not in result.output.lower()
        assert "grace window" in result.output.lower()
        assert "dry-run" in result.output.lower()
        assert "no catalog/t3 rows purged" in result.output.lower()

    def test_default_invocation_prints_population_note(self, monkeypatch):
        """nexus-heizf / nexus-h1zu0 code-review fix round (2026-08-05):
        originally the stranded-chunk vs dangling-manifest disjointness
        caveat, retired RDR-191 Phase 6 (nexus-o8dil.33) alongside the
        dangling-manifest census/manifest-verify it disambiguated against
        (the manifest-chunk FK makes that population unreachable). The
        population note itself must still be in the LIVE text output, not
        docstring/help only."""
        writer = _FakeWriter()
        _patch_writer(monkeypatch, writer)

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "purge-trash"])

        assert result.exit_code == 0, result.output
        assert "population:" in result.output
        assert "tombstoned-doc chunks" in result.output

    def test_default_invocation_labels_document_and_chunk_counts_separately(self, monkeypatch):
        """nexus-3ck2g / nexus-8j1zx: the report distinguishes the
        documents_purged row count from the chunks_<dim>_stranded storage
        counts. nexus-kcm6c update: BOTH honor --older-than-days since
        catalog-026, so the chunk heading now names the grace-window
        protection instead of the retired NOT-age-gated claim (which sent
        the 2026-08-27 shakedown hunting a counter bug that was prose)."""
        writer = _FakeWriter()
        _patch_writer(monkeypatch, writer)

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "purge-trash", "--older-than-days", "45"])

        assert result.exit_code == 0, result.output
        lines = result.output.splitlines()
        docs_line = next(line for line in lines if "documents_purged" in line)
        chunks_header = next(line for line in lines if "chunk storage swept" in line.lower())
        assert "45 day(s)" in docs_line
        assert "age-gated" in docs_line.lower()
        assert "not age-gated" not in chunks_header.lower()
        assert "grace window" in chunks_header.lower()
        # The chunk-storage section header names the mechanism, not the
        # specific day count — only the documents_purged line carries that.
        assert "45 day(s)" not in chunks_header

    def test_older_than_days_forwarded(self, monkeypatch):
        writer = _FakeWriter()
        _patch_writer(monkeypatch, writer)

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "purge-trash", "--older-than-days", "90"])

        assert result.exit_code == 0, result.output
        assert writer.calls == [{"older_than_days": 90, "dry_run": True}]


# ── CLI: confirm gate ───────────────────────────────────────────────────────


class TestConfirmGate:
    def test_no_dry_run_without_confirm_is_report_only(self, monkeypatch):
        writer = _FakeWriter()
        _patch_writer(monkeypatch, writer)

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "purge-trash", "--no-dry-run"])

        assert result.exit_code == 0, result.output
        assert "--confirm" in result.output
        # Still previews (dry_run=True on the wire) — never mutates.
        assert writer.calls == [{"older_than_days": 30, "dry_run": True}]
        assert "dry-run" in result.output.lower()

    def test_no_dry_run_with_confirm_mutates(self, monkeypatch):
        writer = _FakeWriter(result={
            "dry_run": False, "documents_purged": 3, "chunks_384_stranded": 0,
            "chunks_768_stranded": 12, "chunks_1024_stranded": 0,
        })
        _patch_writer(monkeypatch, writer)

        runner = CliRunner()
        result = runner.invoke(
            main, ["catalog", "purge-trash", "--no-dry-run", "--confirm"],
        )

        assert result.exit_code == 0, result.output
        assert writer.calls == [{"older_than_days": 30, "dry_run": False}]
        assert "purge-trash executed" in result.output.lower()
        assert "dry-run" not in result.output.lower()
        assert writer.closed

    def test_dry_run_flag_wins_even_with_confirm(self, monkeypatch):
        """--confirm alone (default --dry-run) must never mutate."""
        writer = _FakeWriter()
        _patch_writer(monkeypatch, writer)

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "purge-trash", "--confirm"])

        assert result.exit_code == 0, result.output
        assert writer.calls == [{"older_than_days": 30, "dry_run": True}]


# ── CLI: gc_purge_marker wiring (nexus-sybbh, client half) ─────────────────


class TestGcPurgeMarkerWiring:
    """The CLI-to-marker integration point: does ``purge_trash_cmd`` actually
    call ``record_purge_marker`` on a REAL execution, and stay silent on a
    dry-run or unconfirmed one? ``tests/test_health_service_checks.py``
    covers the marker module and the doctor check in isolation; neither
    exercises this call site, so it is covered separately here.
    """

    def _spy(self, monkeypatch):
        calls: list[dict] = []
        monkeypatch.setattr(
            "nexus.gc_purge_marker.record_purge_marker",
            lambda result, *, older_than_days: calls.append(
                {"result": result, "older_than_days": older_than_days},
            ),
        )
        return calls

    def test_no_dry_run_with_confirm_records_a_marker(self, monkeypatch):
        writer = _FakeWriter(result={
            "dry_run": False, "documents_purged": 3, "chunks_384_stranded": 0,
            "chunks_768_stranded": 12, "chunks_1024_stranded": 0,
        })
        _patch_writer(monkeypatch, writer)
        calls = self._spy(monkeypatch)

        runner = CliRunner()
        result = runner.invoke(
            main, ["catalog", "purge-trash", "--no-dry-run", "--confirm",
                   "--older-than-days", "45"],
        )

        assert result.exit_code == 0, result.output
        assert len(calls) == 1
        assert calls[0]["older_than_days"] == 45
        assert calls[0]["result"]["documents_purged"] == 3

    def test_default_dry_run_does_not_record_a_marker(self, monkeypatch):
        writer = _FakeWriter()
        _patch_writer(monkeypatch, writer)
        calls = self._spy(monkeypatch)

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "purge-trash"])

        assert result.exit_code == 0, result.output
        assert calls == []

    def test_no_dry_run_without_confirm_does_not_record_a_marker(self, monkeypatch):
        """``--no-dry-run`` alone is report-only (TestConfirmGate above) —
        no real purge happened, so no marker."""
        writer = _FakeWriter()
        _patch_writer(monkeypatch, writer)
        calls = self._spy(monkeypatch)

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "purge-trash", "--no-dry-run"])

        assert result.exit_code == 0, result.output
        assert calls == []

    def test_confirm_without_no_dry_run_does_not_record_a_marker(self, monkeypatch):
        """``--confirm`` alone (default ``--dry-run``) must never mutate —
        and must never record a marker either."""
        writer = _FakeWriter()
        _patch_writer(monkeypatch, writer)
        calls = self._spy(monkeypatch)

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "purge-trash", "--confirm"])

        assert result.exit_code == 0, result.output
        assert calls == []


# ── CLI: partial-purge detection (nexus-ff85q) ───────────────────────────


class TestPartialPurgeIsNeverReportedAsSuccess:
    """nexus-ff85q client half.

    Production 2026-08-03: an execute purged 2 of the 63 age-eligible
    documents its own dry-run had reported and exited 0 with a completion
    report. The engine half (CatalogRepository.purgeTrash) now returns the
    eligible population it measured in the same transaction as
    ``documents_eligible``; these tests pin that the CLI acts on it.
    """

    @staticmethod
    def _execute(monkeypatch, result: dict):
        writer = _FakeWriter(result=result)
        _patch_writer(monkeypatch, writer)
        return writer, CliRunner().invoke(
            main, ["catalog", "purge-trash", "--no-dry-run", "--confirm"]
        )

    def test_partial_purge_exits_non_zero_and_names_the_shortfall(self, monkeypatch):
        writer, result = self._execute(monkeypatch, {
            "dry_run": False, "documents_purged": 2, "documents_eligible": 63,
            "chunks_384_stranded": 0, "chunks_768_stranded": 0,
            "chunks_1024_stranded": 285,
        })

        assert result.exit_code != 0, result.output
        assert "partial purge" in result.output.lower()
        # The exact production magnitudes must appear — an operator has to be
        # able to size the shortfall without a follow-up dry-run.
        assert "2 of 63" in result.output
        assert "61 still eligible" in result.output
        # The benign reading must be named explicitly, with the magnitude that
        # distinguishes it: the engine treats this same signal as a soft WARN
        # because a concurrent restore is a legitimate read-committed cause, and
        # a client that only shouts "not safe to ignore" trains operators to
        # reflexively rerun (substantive-critic Sig-3).
        lowered = result.output.lower()
        assert "read-committed" in lowered
        assert "restore" in lowered
        assert "1-2" in result.output
        # The report itself is still printed BEFORE the failure: the chunk sweep
        # may well have completed and the operator needs to know what it did.
        assert "documents_purged" in result.output
        assert "chunks_1024_stranded: 285" in result.output
        assert writer.closed

    def test_message_promises_no_behaviour_the_command_does_not_implement(self, monkeypatch):
        """The first draft told the operator to act "if the shortfall repeats"
        while implementing no repeat-detector at all — advice the mechanism
        cannot honour (substantive-critic Sig-3). The message must describe only
        what this command actually does: report magnitudes, name the two
        readings, and point at the idempotent re-run."""
        _, result = self._execute(monkeypatch, {
            "dry_run": False, "documents_purged": 2, "documents_eligible": 63,
            "chunks_384_stranded": 0, "chunks_768_stranded": 0,
            "chunks_1024_stranded": 285,
        })

        lowered = result.output.lower()
        assert "repeats" not in lowered
        assert "idempotent" in lowered

    def test_complete_purge_exits_zero(self, monkeypatch):
        _, result = self._execute(monkeypatch, {
            "dry_run": False, "documents_purged": 63, "documents_eligible": 63,
            "chunks_384_stranded": 0, "chunks_768_stranded": 0,
            "chunks_1024_stranded": 285,
        })

        assert result.exit_code == 0, result.output
        assert "partial purge" not in result.output.lower()

    def test_eligible_count_is_not_filed_under_the_chunk_storage_heading(self, monkeypatch):
        """``documents_eligible`` is a document count. The shape-agnostic
        passthrough that prints ``chunks_<dim>_stranded`` would otherwise
        list it under the chunk-storage heading — the mislabelling class
        nexus-8j1zx fixed for the other counts (document rows vs chunk
        storage; both age-gated since catalog-026, nexus-kcm6c)."""
        _, result = self._execute(monkeypatch, {
            "dry_run": False, "documents_purged": 63, "documents_eligible": 63,
            "chunks_384_stranded": 0, "chunks_768_stranded": 0,
            "chunks_1024_stranded": 285,
        })

        assert result.exit_code == 0, result.output
        lines = result.output.splitlines()
        eligible_idx = next(
            i for i, line in enumerate(lines) if "documents_eligible" in line
        )
        chunk_header_idx = next(
            i for i, line in enumerate(lines) if "chunk storage swept" in line.lower()
        )
        assert eligible_idx < chunk_header_idx, result.output
        assert "age-gated" in lines[eligible_idx].lower()

    def test_pre_ff85q_engine_without_eligible_field_still_exits_zero(self, monkeypatch):
        """An engine that predates the eligible-count field sends no verdict
        to act on; the client must not invent one and refuse to work."""
        _, result = self._execute(monkeypatch, {
            "dry_run": False, "documents_purged": 2,
            "chunks_384_stranded": 0, "chunks_768_stranded": 0,
            "chunks_1024_stranded": 285,
        })

        assert result.exit_code == 0, result.output
        assert "partial purge" not in result.output.lower()

    def test_dry_run_is_never_subject_to_the_partial_check(self, monkeypatch):
        """The preview reports its population as ``documents_purged`` and purges
        nothing — a 0-vs-N comparison there would fail every dry-run."""
        writer = _FakeWriter(result={
            "dry_run": True, "documents_purged": 0, "documents_eligible": 63,
            "chunks_384_stranded": 0, "chunks_768_stranded": 0,
            "chunks_1024_stranded": 285,
        })
        _patch_writer(monkeypatch, writer)

        result = CliRunner().invoke(main, ["catalog", "purge-trash"])

        assert result.exit_code == 0, result.output
        assert "partial purge" not in result.output.lower()


# ── CLI: --json ──────────────────────────────────────────────────────────


class TestJsonOutput:
    def test_json_dry_run_emits_parseable_json(self, monkeypatch):
        writer = _FakeWriter()
        _patch_writer(monkeypatch, writer)

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "purge-trash", "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
        assert data["documents_purged"] == 3
        assert data["chunks_384_stranded"] == 0
        assert data["chunks_768_stranded"] == 12
        assert data["chunks_1024_stranded"] == 0
        assert data["dry_run"] is True

    def test_json_carries_population_note(self, monkeypatch):
        """nexus-heizf / nexus-h1zu0 code-review fix round (2026-08-05):
        originally the stranded-chunk vs dangling-manifest disjointness
        caveat, retired RDR-191 Phase 6 (nexus-o8dil.33) alongside its
        counterpart (see the text-mode sibling test above). The population
        note itself must still be in the LIVE --json output, not
        docstring/help only — the exact instrument an agent parses during a
        shakedown."""
        writer = _FakeWriter()
        _patch_writer(monkeypatch, writer)

        result = CliRunner().invoke(main, ["catalog", "purge-trash", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
        assert "tombstoned-doc chunks" in data["population"]

    def test_json_refused_with_no_dry_run(self, monkeypatch):
        _patch_writer(monkeypatch, _writer_factory_raises())

        runner = CliRunner()
        result = runner.invoke(
            main, ["catalog", "purge-trash", "--json", "--no-dry-run"],
        )

        assert result.exit_code != 0
        assert "--json" in result.output
        assert "--no-dry-run" in result.output

    def test_json_refused_with_no_dry_run_and_confirm(self, monkeypatch):
        _patch_writer(monkeypatch, _writer_factory_raises())

        runner = CliRunner()
        result = runner.invoke(
            main, ["catalog", "purge-trash", "--json", "--no-dry-run", "--confirm"],
        )

        assert result.exit_code != 0
        assert "--json" in result.output


# ── CLI: --older-than-days validation ───────────────────────────────────────


class TestOlderThanDaysValidation:
    @pytest.mark.parametrize("value", [0, -1, -30])
    def test_rejects_non_positive_values_without_touching_writer(self, monkeypatch, value):
        _patch_writer(monkeypatch, _writer_factory_raises())

        runner = CliRunner()
        result = runner.invoke(
            main, ["catalog", "purge-trash", "--older-than-days", str(value)],
        )

        assert result.exit_code != 0
        assert "--older-than-days" in result.output
        assert "must be >= 1" in result.output

    def test_accepts_minimum_value(self, monkeypatch):
        writer = _FakeWriter()
        _patch_writer(monkeypatch, writer)

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "purge-trash", "--older-than-days", "1"])

        assert result.exit_code == 0, result.output
        assert writer.calls == [{"older_than_days": 1, "dry_run": True}]


# ── CLI: pre-fix engine degradation ─────────────────────────────────────────


class TestEngineFloorDegradation:
    def test_404_raises_clear_click_exception_naming_3ck2g(self, monkeypatch):
        writer = _FakeWriter(raise_exc=_http_status_error(404))
        _patch_writer(monkeypatch, writer)

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "purge-trash"])

        assert result.exit_code != 0
        assert "nexus-3ck2g" in result.output
        assert "purge-trash" in result.output.lower()
        assert writer.closed  # finally-close still ran

    def test_404_on_mutation_path_also_degrades_cleanly(self, monkeypatch):
        writer = _FakeWriter(raise_exc=_http_status_error(404))
        _patch_writer(monkeypatch, writer)

        runner = CliRunner()
        result = runner.invoke(
            main, ["catalog", "purge-trash", "--no-dry-run", "--confirm"],
        )

        assert result.exit_code != 0
        assert "nexus-3ck2g" in result.output

    def test_non_404_http_error_propagates_uncaught_by_this_verb(self, monkeypatch):
        """A 500 (real engine failure) is not the engine-floor case — it must
        not be mistaken for "engine too old" and swallowed into that
        message; it should surface as a distinct failure."""
        writer = _FakeWriter(raise_exc=_http_status_error(500))
        _patch_writer(monkeypatch, writer)

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "purge-trash"])

        assert result.exit_code != 0
        assert "nexus-3ck2g" not in result.output


# ── Whitelist admission (factory.py / catalog_protocol.py) ─────────────────


class TestWhitelistAdmission:
    def test_purge_trash_is_service_only_not_shared_write_ops(self):
        """Follows the delete_many/update_many precedent: purge_trash has no
        SQLite/daemon-mode equivalent, so it lives in
        _SERVICE_ONLY_WRITE_OPS, layered on top of CATALOG_WRITE_OPS rather
        than added to the shared whitelist."""
        assert "purge_trash" in factory._SERVICE_ONLY_WRITE_OPS
        assert "purge_trash" not in CATALOG_WRITE_OPS

    def test_service_catalog_writer_admits_purge_trash(self):
        class _FakeClient:
            def purge_trash(self, older_than_days=30, dry_run=True):
                return {"dry_run": dry_run, "older_than_days": older_than_days}

            def close(self):
                pass

        writer = factory._ServiceCatalogWriter(_FakeClient())
        fn = writer.purge_trash
        assert callable(fn)
        assert fn(older_than_days=7, dry_run=False) == {
            "dry_run": False, "older_than_days": 7,
        }

    def test_service_catalog_writer_still_rejects_non_whitelisted_names(self):
        class _FakeClient:
            def close(self):
                pass

        writer = factory._ServiceCatalogWriter(_FakeClient())
        with pytest.raises(AttributeError):
            _ = writer.not_a_real_write_op

    def test_purge_trash_not_declared_on_catalog_reader_protocol(self):
        """Reads never go through make_catalog_writer(); purge_trash's
        dry-run preview is a writer-surface op (see the module docstring
        for why), so it must not appear on the reader protocol."""
        assert not hasattr(CatalogReader, "purge_trash")


# ── Registration ────────────────────────────────────────────────────────


def test_purge_trash_registered_under_catalog_group():
    catalog_group = main.commands["catalog"]
    assert "purge-trash" in catalog_group.commands
    assert catalog_group.commands["purge-trash"].callback is purge_trash_mod.purge_trash_cmd.callback
