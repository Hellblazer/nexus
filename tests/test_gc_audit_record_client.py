# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-fduai: ``HttpCatalogClient.record_gc_audit`` — the client-facing
gc_audit producer (``POST /v1/catalog/gc_audit/record``, nexus-jqvzk) that
``nx t3 gc`` reports its own T3 delete through."""

from __future__ import annotations

import pytest

from nexus.catalog.factory import _SERVICE_ONLY_WRITE_OPS, _ServiceCatalogWriter
from nexus.catalog.http_catalog_client import HttpCatalogClient


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> tuple[HttpCatalogClient, list]:
    posted: list[tuple[str, dict]] = []
    c = HttpCatalogClient(base_url="http://127.0.0.1:9", tenant="t", _token="x")
    monkeypatch.setattr(
        c, "_post", lambda path, body=None, **kw: (posted.append((path, body)) or {"id": 7}),
    )
    return c, posted


def test_record_gc_audit_posts_the_engine_body_shape_and_returns_the_id(client) -> None:
    c, posted = client
    audit_id = c.record_gc_audit(
        operation="t3_gc", collection="knowledge__x", actor="nx t3 gc",
        chashes=["a" * 64, "b" * 64], details={"deleted": 2},
    )
    assert audit_id == 7
    assert posted == [(
        "/gc_audit/record",
        {
            "operation": "t3_gc",
            "dry_run": False,
            "chashes": ["a" * 64, "b" * 64],
            "collection": "knowledge__x",
            "actor": "nx t3 gc",
            "details": {"deleted": 2},
        },
    )]


def test_record_gc_audit_omits_blank_optionals(client) -> None:
    c, posted = client
    c.record_gc_audit(operation="probe")
    assert posted == [("/gc_audit/record", {"operation": "probe", "dry_run": False, "chashes": []})]


def test_record_gc_audit_is_a_whitelisted_service_write_op() -> None:
    """The verb reaches it through the write-only proxy; a read-side handle
    must not be the path an audit row is written down."""
    assert "record_gc_audit" in _SERVICE_ONLY_WRITE_OPS

    class _Backend:
        def record_gc_audit(self, **kw):
            return 9

    assert _ServiceCatalogWriter(_Backend()).record_gc_audit(operation="t3_gc") == 9
    with pytest.raises(AttributeError):
        _ServiceCatalogWriter(_Backend()).gc_audit_list
