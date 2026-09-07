# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for ``nx catalog trash`` / ``nx catalog restore`` (nexus-dkymw,
client half — the engine-side ``CatalogRepository.restoreDocument`` /
``.listTrash`` and their ``POST /v1/catalog/restore`` / ``GET
/v1/catalog/trash`` routes are the Java sibling half, built concurrently).

Mirrors ``tests/test_catalog_purge_trash.py``'s fake-based style for the CLI
layer — no real catalog/engine substrate — plus direct
``HttpCatalogClient._post``/``._get`` monkeypatching (the
``test_register_returns_tumbler_...`` style already used in
``tests/catalog/test_http_catalog_client.py``) for the wire contract.
"""
from __future__ import annotations

import httpx
import pytest
from click.testing import CliRunner

from nexus.catalog.factory import _SERVICE_ONLY_WRITE_OPS, _ServiceCatalogWriter
from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.cli import main


# ── HttpCatalogClient wire contract ─────────────────────────────────────────


class TestHttpCatalogClientWireContract:
    def _client(self) -> HttpCatalogClient:
        return HttpCatalogClient(base_url="http://127.0.0.1:1", tenant="t", _token="x")

    def test_restore_document_posts_tumbler_and_returns_bool(self) -> None:
        client = self._client()
        calls: list[tuple[str, dict | None]] = []

        def _fake_post(path: str, body: dict | None = None, **_kwargs) -> dict:
            calls.append((path, body))
            return {"restored": 1}

        client._post = _fake_post  # type: ignore[method-assign]
        assert client.restore_document("1.2.3") is True
        assert calls == [("/restore", {"tumbler": "1.2.3"})]

    def test_restore_document_zero_is_false(self) -> None:
        client = self._client()
        client._post = lambda path, body=None, **_k: {"restored": 0}  # type: ignore[method-assign]
        assert client.restore_document("1.2.3") is False

    def test_restore_document_engine_floor_404_propagates(self) -> None:
        client = self._client()

        def _fake_post(path: str, body: dict | None = None, **_kwargs) -> dict:
            request = httpx.Request("POST", "http://test/v1/catalog/restore")
            response = httpx.Response(404, request=request)
            raise httpx.HTTPStatusError("404", request=request, response=response)

        client._post = _fake_post  # type: ignore[method-assign]
        with pytest.raises(httpx.HTTPStatusError):
            client.restore_document("1.2.3")

    def test_list_trash_gets_and_returns_documents_verbatim(self) -> None:
        client = self._client()
        calls: list[tuple[str, dict]] = []
        docs = [
            {"tumbler": "1.2.3", "title": "Trashed A", "deleted_at": "2026-09-01T00:00:00Z"},
            {"tumbler": "1.2.4", "title": "Trashed B", "deleted_at": "2026-09-02T00:00:00Z"},
        ]

        def _fake_get(path: str, **kwargs) -> dict:
            calls.append((path, kwargs))
            return {"documents": docs, "count": len(docs)}

        client._get = _fake_get  # type: ignore[method-assign]
        out = client.list_trash(limit=50, offset=10)
        assert out == docs
        assert calls == [("/trash", {"limit": 50, "offset": 10})]

    def test_list_trash_empty_result(self) -> None:
        client = self._client()
        client._get = lambda path, **k: {"documents": [], "count": 0}  # type: ignore[method-assign]
        assert client.list_trash() == []


# ── _ServiceCatalogWriter admission ─────────────────────────────────────────


class TestRestoreDocumentWriterAdmission:
    def test_restore_document_is_service_only_not_shared_whitelist(self) -> None:
        # Same disposition as purge_trash/delete_many/delete_collection: a
        # service-only op with no local-catalog equivalent, so it lives in
        # _SERVICE_ONLY_WRITE_OPS rather than the shared CATALOG_WRITE_OPS /
        # catalog_protocol.py Protocol pair (which requires a canonical
        # Catalog method to fidelity-test parameter shapes against).
        assert "restore_document" in _SERVICE_ONLY_WRITE_OPS

    def test_writer_forwards_restore_document(self) -> None:
        class _FakeClient:
            def restore_document(self, tumbler):
                return tumbler == "1.2.3"

        writer = _ServiceCatalogWriter(_FakeClient())
        assert writer.restore_document("1.2.3") is True
        assert writer.restore_document("9.9.9") is False

    def test_writer_rejects_unwhitelisted_name(self) -> None:
        writer = _ServiceCatalogWriter(object())
        with pytest.raises(AttributeError):
            writer.list_trash  # noqa: B018 — accessing to trigger __getattr__; not a write op


# ── CLI: nx catalog trash ───────────────────────────────────────────────────


class _FakeReader:
    """``resolve``/``find`` both report "nothing live" by default — the
    live resolver (``nexus.catalog.resolve_tumbler``) always misses, so
    every restore-by-title test below exercises the trash-listing
    fallback (``_resolve_restore_target``'s whole reason for existing)
    unless a subclass overrides one of these two.
    """

    def __init__(self, docs: list[dict] | None = None, raise_exc: Exception | None = None):
        self._docs = docs if docs is not None else []
        self._raise = raise_exc
        self.calls: list[dict] = []

    def resolve(self, tumbler):
        return None

    def find(self, query, *, content_type=None):
        return []

    def list_trash(self, *, limit: int = 200, offset: int = 0) -> list[dict]:
        self.calls.append({"limit": limit, "offset": offset})
        if self._raise is not None:
            raise self._raise
        return self._docs


class _FakeWriter:
    def __init__(self, restored: int = 1, raise_exc: Exception | None = None):
        self._restored = restored
        self._raise = raise_exc
        self.calls: list[str] = []

    def restore_document(self, tumbler: str) -> bool:
        self.calls.append(tumbler)
        if self._raise is not None:
            raise self._raise
        return bool(self._restored)


def _http_404(path: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", f"http://test{path}")
    response = httpx.Response(404, request=request)
    return httpx.HTTPStatusError("404", request=request, response=response)


def _patch_reader(monkeypatch, reader):
    monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: reader)


def _patch_writer(monkeypatch, writer):
    monkeypatch.setattr("nexus.commands.catalog._get_catalog_writer", lambda: writer)


class TestTrashCmd:
    def test_empty_trash_prints_message(self, monkeypatch):
        _patch_reader(monkeypatch, _FakeReader([]))
        result = CliRunner().invoke(main, ["catalog", "trash"])
        assert result.exit_code == 0, result.output
        assert "trash is empty" in result.output.lower()

    def test_populated_trash_lists_tumbler_and_title(self, monkeypatch):
        docs = [
            {"tumbler": "1.2.3", "title": "Doc A", "deleted_at": "2026-09-01T00:00:00Z"},
            {"tumbler": "1.2.4", "title": "Doc B", "deleted_at": "2026-09-02T00:00:00Z"},
        ]
        _patch_reader(monkeypatch, _FakeReader(docs))
        result = CliRunner().invoke(main, ["catalog", "trash"])
        assert result.exit_code == 0, result.output
        assert "1.2.3" in result.output
        assert "Doc A" in result.output
        assert "1.2.4" in result.output
        assert "Doc B" in result.output

    def test_limit_flag_forwarded(self, monkeypatch):
        reader = _FakeReader([])
        _patch_reader(monkeypatch, reader)
        result = CliRunner().invoke(main, ["catalog", "trash", "--limit", "5"])
        assert result.exit_code == 0, result.output
        assert reader.calls == [{"limit": 5, "offset": 0}]

    def test_engine_floor_404_raises_clean_error(self, monkeypatch):
        _patch_reader(monkeypatch, _FakeReader(raise_exc=_http_404("/trash")))
        result = CliRunner().invoke(main, ["catalog", "trash"])
        assert result.exit_code != 0
        assert "engine" in result.output.lower()
        assert "nexus-dkymw" in result.output


# ── CLI: nx catalog restore ─────────────────────────────────────────────────


class TestRestoreCmd:
    def test_restore_by_tumbler_success(self, monkeypatch):
        _patch_reader(monkeypatch, _FakeReader([]))
        writer = _FakeWriter(restored=1)
        _patch_writer(monkeypatch, writer)

        result = CliRunner().invoke(main, ["catalog", "restore", "1.2.3"])
        assert result.exit_code == 0, result.output
        assert "Restored: 1.2.3" in result.output
        assert writer.calls == ["1.2.3"]

    def test_restore_by_tumbler_not_restored_reports_no_op(self, monkeypatch):
        _patch_reader(monkeypatch, _FakeReader([]))
        _patch_writer(monkeypatch, _FakeWriter(restored=0))

        result = CliRunner().invoke(main, ["catalog", "restore", "1.2.3"])
        assert result.exit_code == 0, result.output
        assert "not restored" in result.output.lower()
        assert "re-indexing" in result.output.lower()

    def test_restore_by_title_falls_back_to_trash_listing(self, monkeypatch):
        # Live resolver has nothing (title matches only a tombstoned doc);
        # the trash-listing fallback must find it by title.
        reader = _FakeReader([{"tumbler": "1.2.9", "title": "Trashed Doc", "deleted_at": "x"}])
        _patch_reader(monkeypatch, reader)
        writer = _FakeWriter(restored=1)
        _patch_writer(monkeypatch, writer)

        result = CliRunner().invoke(main, ["catalog", "restore", "Trashed Doc"])
        assert result.exit_code == 0, result.output
        assert "Restored: 1.2.9" in result.output
        assert writer.calls == ["1.2.9"]

    def test_restore_by_ambiguous_trash_title_raises(self, monkeypatch):
        reader = _FakeReader([
            {"tumbler": "1.2.9", "title": "Dup", "deleted_at": "x"},
            {"tumbler": "1.2.10", "title": "Dup", "deleted_at": "y"},
        ])
        _patch_reader(monkeypatch, reader)
        _patch_writer(monkeypatch, _FakeWriter())

        result = CliRunner().invoke(main, ["catalog", "restore", "Dup"])
        assert result.exit_code != 0
        assert "ambiguous" in result.output.lower()

    def test_restore_unknown_title_raises(self, monkeypatch):
        _patch_reader(monkeypatch, _FakeReader([]))
        _patch_writer(monkeypatch, _FakeWriter())

        result = CliRunner().invoke(main, ["catalog", "restore", "Nonexistent"])
        assert result.exit_code != 0
        assert "not found" in result.output.lower()

    def test_restore_engine_floor_404_raises_clean_error(self, monkeypatch):
        _patch_reader(monkeypatch, _FakeReader([]))
        _patch_writer(monkeypatch, _FakeWriter(raise_exc=_http_404("/restore")))

        result = CliRunner().invoke(main, ["catalog", "restore", "1.2.3"])
        assert result.exit_code != 0
        assert "engine" in result.output.lower()
        assert "nexus-dkymw" in result.output
