# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx catalog gc-audit list`` (nexus-fduai): the thin lister over
``HttpCatalogClient.gc_audit_list`` — filters pass through verbatim, rows
are shown as the engine sent them, a route-less engine is named."""

from __future__ import annotations

import json

import httpx
import pytest
from click.testing import CliRunner

from nexus.cli import main

_ROWS = [
    {
        "id": 12, "operation": "t3_gc", "collection": "knowledge__x", "actor": "nx t3 gc",
        "dry_run": False, "chash_count": 2, "chashes": ["a" * 64, "b" * 64],
        "details": {"deleted": 2, "requested": 2}, "created_at": "2026-08-28T13:00:00Z",
    },
    {
        "id": 11, "operation": "purge_trash", "collection": None, "actor": "engine",
        "dry_run": True, "chash_count": 0, "chashes": [], "details": {},
        "created_at": "2026-08-28T12:00:00Z",
    },
]


class _Cat:
    def __init__(self, rows=None, raise_exc=None):
        self.calls: list[dict] = []
        self._rows = rows if rows is not None else _ROWS
        self._raise = raise_exc

    def gc_audit_list(self, **kw):
        self.calls.append(kw)
        if self._raise is not None:
            raise self._raise
        return list(self._rows)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _patch(monkeypatch, cat):
    monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: cat)


def test_filters_pass_through_and_json_shows_every_field(runner, monkeypatch):
    cat = _Cat()
    _patch(monkeypatch, cat)
    result = runner.invoke(main, [
        "catalog", "gc-audit", "list", "--operation", "t3_gc",
        "--collection", "knowledge__x", "--limit", "5", "--offset", "10", "--json",
    ])
    assert result.exit_code == 0, result.output
    assert cat.calls == [{
        "collection": "knowledge__x", "operation": "t3_gc", "limit": 5, "offset": 10,
    }]
    assert json.loads(result.stdout) == _ROWS  # stdout, never .output (nexus-84a6)


def test_text_mode_one_line_per_row_with_paging_hint(runner, monkeypatch):
    _patch(monkeypatch, _Cat())
    result = runner.invoke(main, ["catalog", "gc-audit", "list"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert any("t3_gc" in l and "nx t3 gc" in l and "knowledge__x" in l for l in lines)
    assert any("purge_trash" in l and "engine" in l for l in lines)
    assert "2 row(s) shown (offset 0); use --offset 50" in result.output


def test_empty_trail_says_so(runner, monkeypatch):
    _patch(monkeypatch, _Cat(rows=[]))
    result = runner.invoke(main, ["catalog", "gc-audit", "list", "--operation", "nothing"])
    assert result.exit_code == 0, result.output
    assert "No gc_audit rows match." in result.output


def test_routeless_engine_is_named_not_reported_as_empty(runner, monkeypatch):
    request = httpx.Request("GET", "http://test/v1/catalog/gc_audit/list")
    exc = httpx.HTTPStatusError(
        "HTTP 404", request=request, response=httpx.Response(404, request=request),
    )
    _patch(monkeypatch, _Cat(raise_exc=exc))
    result = runner.invoke(main, ["catalog", "gc-audit", "list"])
    assert result.exit_code != 0
    assert "no /gc_audit/list route" in result.output
    assert "No gc_audit rows match" not in result.output


def test_other_http_errors_propagate(runner, monkeypatch):
    request = httpx.Request("GET", "http://test/v1/catalog/gc_audit/list")
    exc = httpx.HTTPStatusError(
        "HTTP 500", request=request, response=httpx.Response(500, request=request),
    )
    _patch(monkeypatch, _Cat(raise_exc=exc))
    result = runner.invoke(main, ["catalog", "gc-audit", "list"])
    assert result.exit_code != 0
    assert isinstance(result.exception, httpx.HTTPStatusError)
