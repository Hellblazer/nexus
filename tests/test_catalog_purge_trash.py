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
        assert "not age-gated" in result.output.lower()
        assert "dry-run" in result.output.lower()
        assert "no catalog/t3 rows purged" in result.output.lower()

    def test_default_invocation_labels_age_gated_and_age_independent_counts_separately(self, monkeypatch):
        """nexus-3ck2g code-review Important / nexus-8j1zx fix round: the
        report must visually distinguish the age-gated documents_purged
        count from the age-independent chunks_<dim>_stranded counts — not
        list them flat under one "(older than N day(s))" header, which
        misattributed the age gate to the chunk sweep too."""
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
        assert "not age-gated" in chunks_header.lower()
        # The chunk-storage section header must NOT itself carry the age
        # qualifier — only the documents_purged line does.
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
