# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx catalog rename-collection`` on an install whose gate 3 has latched (RDR-229 P3.7, engine 0.1.202).

After the engine's one-row rename the registry row keeps its id and moves to the new label, so the old label has no row
left. The follow-up ``supersede_collection(old, new)`` then answers 404 ``collection not found``; the verb used to take
that for a failed projection update, print the recovery WARN and exit 2 for a rename that had fully succeeded.

These tests run the verb against a real ``HttpCatalogClient`` over an ``httpx.MockTransport`` that plays the engine's
registry for both regimes: the one-row rename (200 with ``ids_stable``, the same ``collection_id`` on the new label) and
the name path (a fresh id on the new label, the old row kept as a tombstone).
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner

from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.cli import main
from nexus.db.collection_state import CollectionState

MODEL = "bge-base-en-v15-768"
OLD = "knowledge__delos"
NEW = f"knowledge__1-1__{MODEL}__v1"


class _Registry:
    """The engine's collection registry, as far as this verb reads and writes it."""

    def __init__(self, *, one_row: bool, supersede_status: int | None = None) -> None:
        self.one_row = one_row
        self.supersede_status = supersede_status  # force the supersede route to answer this refusal
        self.rows: dict[str, dict] = {
            OLD: {"name": OLD, "collection_id": 5, "embedding_model": MODEL, "lifecycle_state": "live",
                  "content_type": "knowledge", "owner_id": "delos"},
        }
        self.posts: list[tuple[str, dict]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        if request.method == "GET" and path == "/v1/catalog/collections/get":
            row = self.rows.get(request.url.params["name"])
            return httpx.Response(200, json=row) if row else httpx.Response(404, json={"error": "collection not found"})
        self.posts.append((path, body))
        if path == "/v1/catalog/collections/rename":
            old, new = body["old_name"], body["new_name"]
            moved = self.rows[old]
            if self.one_row:
                self.rows[new] = {**moved, "name": new}
                del self.rows[old]
                return httpx.Response(200, json={"renamed": {"catalog_documents": 3}, "name": new,
                                                 "collection_id": moved["collection_id"], "ids_stable": True})
            self.rows[new] = {**moved, "name": new, "collection_id": 9}
            self.rows[old] = {**moved, "superseded_by": new}
            return httpx.Response(200, json={"renamed": {"catalog_documents": 3}, "name": new,
                                             "collection_id": 9, "ids_stable": False})
        if path == "/v1/catalog/collections/supersede":
            if self.supersede_status is not None:
                return httpx.Response(self.supersede_status, json={"error": "refused by the engine"})
            if body["name"] not in self.rows:
                return httpx.Response(404, json={"error": f"collection not found: {body['name']}"})
            return httpx.Response(200, json={"updated": 1})
        return httpx.Response(404, json={"error": f"unexpected {request.method} {path}"})


def _catalog(registry: _Registry) -> HttpCatalogClient:
    cat = HttpCatalogClient(base_url="http://mock.test", _token="t")
    cat._client = httpx.Client(base_url="http://mock.test", transport=httpx.MockTransport(registry.handle))
    return cat


def _rename(registry: _Registry) -> "click.testing.Result":  # noqa: F821
    cat = _catalog(registry)
    states = {OLD: CollectionState.PRESENT, NEW: CollectionState.ABSENT}
    with patch("nexus.db.service_endpoint.guard_production_write"), \
         patch("nexus.commands.catalog._get_catalog", return_value=cat), \
         patch("nexus.commands.catalog._get_catalog_writer", return_value=cat), \
         patch("nexus.db.make_t3", return_value=MagicMock()), \
         patch("nexus.db.collection_state.probe_collection_state", side_effect=lambda _db, n: states[n]), \
         patch("nexus.collection_rename.probe_collection_state", side_effect=lambda _db, n: states[n]), \
         patch("nexus.corpus.ensure_collection_registered"):
        return CliRunner().invoke(main, ["catalog", "rename-collection", OLD, NEW, "--yes"])


def test_a_one_row_rename_that_succeeded_is_reported_as_success() -> None:
    registry = _Registry(one_row=True)
    result = _rename(registry)
    out = " ".join(result.output.split())
    assert result.exit_code == 0, out
    assert f"Renamed: {OLD} -> {NEW}" in out, f"the rename never reached its summary: {out}"
    assert "WARN" not in out and "projection update failed" not in out, out
    assert "backfill-collections" not in out, "no recovery plan for a rename that needs none"
    assert "upgrade it" not in out, "the old-engine NOTE is false on a one-row engine"
    assert "Emitted CollectionSuperseded" not in out, "nothing was superseded: the one row moved"
    # the registry holds exactly the moved row, and the verb did not send the supersede it knew would 404
    assert list(registry.rows) == [NEW] and registry.rows[NEW]["collection_id"] == 5
    assert [p for p, _b in registry.posts if p.endswith("/supersede")] == []


def test_a_real_projection_failure_still_warns_and_exits_2() -> None:
    """The name path (a fresh id on the new label) whose supersede the engine refuses is a real failure."""
    registry = _Registry(one_row=False, supersede_status=409)
    result = _rename(registry)
    out = " ".join(result.output.split())
    assert result.exit_code == 2, out
    assert "WARN: T3 was renamed" in out and "projection update failed" in out, out
    assert [p for p, _b in registry.posts if p.endswith("/supersede")], "the supersede was attempted"


def test_a_name_path_rename_still_supersedes_and_announces_it() -> None:
    registry = _Registry(one_row=False)
    result = _rename(registry)
    out = " ".join(result.output.split())
    assert result.exit_code == 0, out
    assert f"Emitted CollectionSuperseded({OLD} -> {NEW})" in out, out
    assert "WARN" not in out


@pytest.mark.parametrize("old_id", [None])
def test_an_engine_that_sends_no_ids_keeps_the_old_behaviour(old_id) -> None:
    """No ``collection_id`` on the source row: the one-row check cannot hold, so a 404 supersede is still a warning."""
    registry = _Registry(one_row=True)
    registry.rows[OLD]["collection_id"] = old_id
    result = _rename(registry)
    assert result.exit_code == 2, result.output
    assert "projection update failed" in " ".join(result.output.split())
