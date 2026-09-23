# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for ``nx catalog sweep-ghosts`` (nexus-29drn, client half).

Mirrors ``tests/test_catalog_purge_trash.py``'s fake-based style — no real
catalog / engine substrate. The engine's own ``POST /v1/catalog/ghost-sweep``
route is the sibling (Java) half; these tests exercise only the client-side
wiring: the CLI verb's dry-run/--apply gate, the
``HttpCatalogClient.ghost_sweep`` wire contract, and the engine-floor
refusal.
"""
from __future__ import annotations

import json

import httpx
import pytest
from click.testing import CliRunner

from nexus.cli import main


class _FakeWriter:
    def __init__(self, result: dict | None = None, raise_exc: Exception | None = None):
        self.calls: list[dict] = []
        self.closed = False
        self._result = result if result is not None else {
            "scanned": 5,
            "ghosts_deleted": 2,
            "marked_dormant": 1,
            "quarantine_held": 1,
            "ghost_names": ["knowledge__gone-a__voyage-context-3__v1",
                             "knowledge__gone-b__voyage-context-3__v1"],
            "dormant_names": ["knowledge__empty-c__voyage-context-3__v1"],
            "dry_run": True,
        }
        self._raise = raise_exc

    def ghost_sweep(self, *, dry_run: bool) -> dict:
        self.calls.append({"dry_run": dry_run})
        if self._raise is not None:
            raise self._raise
        return dict(self._result, dry_run=dry_run)

    def close(self) -> None:
        self.closed = True


def _patch_writer(monkeypatch, writer):
    monkeypatch.setattr("nexus.commands.catalog._get_catalog_writer", lambda: writer)


def _http_status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://test/v1/catalog/ghost-sweep")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


class TestDryRunDefault:
    def test_default_invocation_calls_client_with_dry_run_true(self, monkeypatch):
        writer = _FakeWriter()
        _patch_writer(monkeypatch, writer)

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "sweep-ghosts"])

        assert result.exit_code == 0, result.output
        assert writer.calls == [{"dry_run": True}]
        assert writer.closed

    def test_default_invocation_reports_names_and_dry_run_notice(self, monkeypatch):
        writer = _FakeWriter()
        _patch_writer(monkeypatch, writer)

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "sweep-ghosts"])

        assert result.exit_code == 0, result.output
        assert "scanned: 5" in result.output
        assert "would reclaim" in result.output.lower()
        assert "knowledge__gone-a__voyage-context-3__v1" in result.output
        assert "knowledge__gone-b__voyage-context-3__v1" in result.output
        assert "knowledge__empty-c__voyage-context-3__v1" in result.output
        assert "would mark dormant" in result.output.lower()
        assert "nothing reclaimed" in result.output.lower()
        assert "--apply" in result.output

    def test_default_invocation_never_apply_reaches_the_engine(self, monkeypatch):
        writer = _FakeWriter()
        _patch_writer(monkeypatch, writer)

        runner = CliRunner()
        runner.invoke(main, ["catalog", "sweep-ghosts"])

        assert writer.calls == [{"dry_run": True}], (
            "a bare invocation must never mutate: dry_run must be True"
        )


class TestApply:
    def test_apply_calls_client_with_dry_run_false(self, monkeypatch):
        writer = _FakeWriter({
            "scanned": 2, "ghosts_deleted": 1, "marked_dormant": 0,
            "quarantine_held": 0, "ghost_names": ["knowledge__gone__voyage-context-3__v1"],
            "dormant_names": [], "dry_run": False,
        })
        _patch_writer(monkeypatch, writer)

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "sweep-ghosts", "--apply"])

        assert result.exit_code == 0, result.output
        assert writer.calls == [{"dry_run": False}]
        assert "reclaimed" in result.output.lower()
        assert "would reclaim" not in result.output.lower()
        assert "nothing reclaimed" not in result.output.lower()


class TestJsonOutput:
    def test_json_flag_emits_the_engine_response_verbatim(self, monkeypatch):
        writer = _FakeWriter()
        _patch_writer(monkeypatch, writer)

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "sweep-ghosts", "--json"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["scanned"] == 5
        assert payload["ghosts_deleted"] == 2
        assert payload["dry_run"] is True


class TestEngineFloor:
    def test_404_from_engine_raises_a_clear_upgrade_message(self, monkeypatch):
        writer = _FakeWriter(raise_exc=_http_status_error(404))
        _patch_writer(monkeypatch, writer)

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "sweep-ghosts"])

        assert result.exit_code != 0
        assert "does not yet expose" in result.output
        assert "ghost-sweep" in result.output
        assert writer.closed, "the writer must still be closed on the engine-floor path"

    def test_non_404_http_error_propagates_unmodified(self, monkeypatch):
        writer = _FakeWriter(raise_exc=_http_status_error(500))
        _patch_writer(monkeypatch, writer)

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "sweep-ghosts"])

        assert result.exit_code != 0
        assert "does not yet expose" not in result.output
