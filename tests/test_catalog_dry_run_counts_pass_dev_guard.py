# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx doctor``'s "Catalog garbage" and "Ghost collections" rows from a dev checkout.

Both rows count through a dry run: ``HttpCatalogClient.purge_trash(dry_run=True)`` and ``ghost_sweep(dry_run=True)``.
The engine answers a dry run from a read-only preview (``CatalogHandler.handlePurgeTrash`` calls
``purgeTrashPreview``; ``handleGhostSweep`` classifies without mutating), but both methods posted with the mixin's
default ``mutates=True``, so the dev-checkout production-write guard
(``nexus.db.service_endpoint.guard_production_write``) refused the call against the operator's real endpoint: the rows
read "could not count" / "not applicable" from a checkout and "none" from the installed ``nx``.

These tests run the real guard (the suite's blanket in-process opt-in is withdrawn, and this file's own worktree is a
dev checkout) over ``httpx.MockTransport``: a dry run reaches the wire and the rows report the engine's counts; a live
call is still refused, before any request, so the guard keeps refusing every real write.
Tenant-free: no engine, no storage touch.
"""
from __future__ import annotations

import json

import httpx
import pytest

from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.commands.doctor import _run_check_ghost_sweep
from nexus.db import service_endpoint
from nexus.db.service_endpoint import ProductionWriteGuardError
from nexus.garbage import catalog_garbage

_PURGE_PREVIEW = {"documents_purged": 2, "chunks_768_stranded": 5, "dry_run": True}
_GHOST_PREVIEW = {"scanned": 9, "ghosts_deleted": 3, "marked_dormant": 0, "quarantine_held": 0, "dry_run": True}


@pytest.fixture
def real_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard as a dev-checkout operator meets it: no opt-in, in a process whose package resolves from a checkout."""
    monkeypatch.setattr(service_endpoint, "_test_only_opt_in_reason", None)
    monkeypatch.delenv(service_endpoint.PROD_WRITE_OPT_IN_ENV, raising=False)
    assert service_endpoint.is_dev_checkout_process(), "this suite runs from a checkout, or the guard is not engaged"


@pytest.fixture
def wire(real_guard: None) -> tuple[HttpCatalogClient, list[dict]]:
    """A real ``HttpCatalogClient`` over a mock transport that records each request and answers a preview."""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        seen.append({"path": request.url.path, "method": request.method, "body": body})
        if request.url.path.endswith("/purge-trash"):
            return httpx.Response(200, json=_PURGE_PREVIEW)
        if request.url.path.endswith("/ghost-sweep"):
            return httpx.Response(200, json=_GHOST_PREVIEW)
        if request.url.path.endswith("/links/orphaned"):
            return httpx.Response(200, json={"links": []})
        return httpx.Response(404, json={"error": "unexpected"})

    cat = HttpCatalogClient(base_url="http://mock.test", _token="t")
    cat._client = httpx.Client(base_url="http://mock.test", transport=httpx.MockTransport(handler))
    return cat, seen


def test_a_purge_trash_dry_run_is_a_read_and_passes_the_guard(wire) -> None:
    cat, seen = wire
    assert cat.purge_trash(older_than_days=1, dry_run=True) == _PURGE_PREVIEW
    assert [(r["path"], r["body"]) for r in seen] == [("/v1/catalog/purge-trash", {"older_than_days": 1, "dry_run": True})]


def test_a_ghost_sweep_dry_run_is_a_read_and_passes_the_guard(wire) -> None:
    cat, seen = wire
    assert cat.ghost_sweep(dry_run=True) == _GHOST_PREVIEW
    assert [(r["path"], r["body"]) for r in seen] == [("/v1/catalog/ghost-sweep", {"dry_run": True})]


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda c: c.purge_trash(older_than_days=1, dry_run=False), id="purge_trash-live"),
        pytest.param(lambda c: c.ghost_sweep(dry_run=False), id="ghost_sweep-live"),
        # Anything that is not literally True is a write: a truthy-but-wrong value must not read as a dry run.
        pytest.param(lambda c: c.purge_trash(older_than_days=1, dry_run=1), id="purge_trash-dry_run=1"),
        pytest.param(lambda c: c.ghost_sweep(dry_run=None), id="ghost_sweep-dry_run=None"),
    ],
)
def test_the_guard_still_refuses_a_live_call_before_any_request(wire, call) -> None:
    cat, seen = wire
    with pytest.raises(ProductionWriteGuardError, match="STOP: refusing a WRITE"):
        call(cat)
    assert seen == [], "the refusal must come before the first network attempt"


def test_the_catalog_garbage_row_reports_the_real_counts_from_a_dev_checkout(wire) -> None:
    cat, _seen = wire
    garbage = catalog_garbage(cat)
    assert garbage.error is None, garbage.error
    assert (garbage.orphaned_links, garbage.trash_documents, garbage.stranded_chunks) == (0, 2, 5)


def test_the_ghost_collections_row_reports_the_real_count_from_a_dev_checkout(
    wire, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    cat, _seen = wire
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_writer", lambda: cat)
    cat.close = lambda: None  # type: ignore[method-assign]  # the fixture's client is the test's to hold
    _run_check_ghost_sweep()
    out = capsys.readouterr().out
    assert "not applicable" not in out, out
    assert "3 collection(s) would be reclaimed" in out, out
