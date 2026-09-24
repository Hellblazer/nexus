# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-29drn: ``HttpCatalogClient.ghost_sweep`` — the operator-facing
caller for the engine's RDR-204 ghost sweep (``POST
/v1/catalog/ghost-sweep``), backing ``nx catalog sweep-ghosts``.

Repository-level classification is pinned engine-side
(``GhostSweepDormantMarkingTest``/``CatalogHandlerGhostSweepTest``); this
file pins only the client's wire-shape and the write-only-proxy wiring,
same split as ``test_gc_audit_record_client.py``.

The collection names in the fake engine responses use a model-neutral
placeholder ("some-model"), not a real voyage token: `_post` is
monkeypatched here, so no embedding call is ever made and no mode
(local/cloud) applies -- this file is entirely about the wire shape,
never about embedding-mode behavior.
"""

from __future__ import annotations

import pytest

from nexus.catalog.factory import _SERVICE_ONLY_WRITE_OPS, _ServiceCatalogWriter
from nexus.catalog.http_catalog_client import HttpCatalogClient


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> tuple[HttpCatalogClient, list]:
    posted: list[tuple[str, dict]] = []
    c = HttpCatalogClient(base_url="http://127.0.0.1:9", tenant="t", _token="x")
    monkeypatch.setattr(
        c, "_post", lambda path, body=None, **kw: (posted.append((path, body)) or {
            "scanned": 3, "ghosts_deleted": 1, "marked_dormant": 1, "quarantine_held": 0,
            "ghost_names": ["knowledge__x__some-model__v1"],
            "dormant_names": ["knowledge__y__some-model__v1"],
            "dry_run": body.get("dry_run") if body else True,
        }),
    )
    return c, posted


def test_ghost_sweep_defaults_to_dry_run_true(client) -> None:
    c, posted = client
    result = c.ghost_sweep()
    assert posted == [("/ghost-sweep", {"dry_run": True})]
    assert result["dry_run"] is True
    assert result["ghost_names"] == ["knowledge__x__some-model__v1"]


def test_ghost_sweep_dry_run_false_posts_the_apply_flag(client) -> None:
    c, posted = client
    result = c.ghost_sweep(dry_run=False)
    assert posted == [("/ghost-sweep", {"dry_run": False})]
    assert result["dry_run"] is False


def test_ghost_sweep_returns_engine_response_verbatim(client) -> None:
    c, _posted = client
    result = c.ghost_sweep()
    assert result == {
        "scanned": 3, "ghosts_deleted": 1, "marked_dormant": 1, "quarantine_held": 0,
        "ghost_names": ["knowledge__x__some-model__v1"],
        "dormant_names": ["knowledge__y__some-model__v1"],
        "dry_run": True,
    }


def test_ghost_sweep_is_a_whitelisted_service_write_op() -> None:
    """The verb reaches it through the write-only proxy, mirroring
    purge_trash -- the dry-run PREVIEW is itself an engine-side read behind
    the write surface, so it belongs on the writer even in preview mode."""
    assert "ghost_sweep" in _SERVICE_ONLY_WRITE_OPS

    class _Backend:
        def ghost_sweep(self, **kw):
            return {"scanned": 0, "ghosts_deleted": 0, "marked_dormant": 0,
                     "quarantine_held": 0, "ghost_names": [], "dormant_names": [],
                     "dry_run": kw.get("dry_run", True)}

    assert _ServiceCatalogWriter(_Backend()).ghost_sweep()["ghosts_deleted"] == 0
    with pytest.raises(AttributeError):
        _ServiceCatalogWriter(_Backend()).gc_audit_list
