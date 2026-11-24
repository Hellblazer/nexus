# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P3.7 (engine 0.1.202): the indexer's per-walk restore meets ``restore_origin_unregistered``.

The engine refuses a restore (409, ``reason`` ``restore_origin_unregistered``) when quarantined chunks of the walked
collection carry the null origin marker: the origin has no registry row, so nothing is re-created from its label. The
restore of the primary sibling raises; ``_prune_deleted_files`` must log that once for the collection, skip that
collection's prune, and go on to the next one, instead of a traceback warning per collection.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from structlog.testing import capture_logs

from nexus.db.http_vector_client import VectorServiceError
from nexus.indexer import _prune_deleted_files

CODE = "code__1-1__bge-base-en-v15-768__v1"
DOCS = "docs__1-1__bge-base-en-v15-768__v1"


class _Catalog:
    def chashes_for_collection(self, name: str) -> set[str]:
        return {"a" * 64}


class _Db:
    """The engine client: the restore route answers per origin; a pass that got past the restore is recorded."""

    def __init__(self, refuse: set[str], other: Exception | None = None) -> None:
        self.refuse, self.other = refuse, other
        self.restore_calls: list[str] = []
        self.quarantine_calls: list[str] = []

    def get_collection(self, name: str):
        return SimpleNamespace(count=lambda: 5)

    def gc_restore_rereferenced_bounded(self, quarantine, origin, row_limit, **kw):
        self.restore_calls.append(origin)
        if self.other is not None:
            raise self.other
        if origin in self.refuse:
            raise VectorServiceError(
                f"origin collection {origin} has no registry row", code=409, reason="restore_origin_unregistered",
                engine_body={"reason": "restore_origin_unregistered", "label": origin, "rows": 4},
            )
        return {"restored": 0, "remaining": 0}

    def gc_quarantine_orphans_bounded(self, origin, *a, **kw):
        self.quarantine_calls.append(origin)
        raise _Stop

    gc_quarantine_orphans = gc_quarantine_orphans_bounded


class _Stop(Exception):
    """Ends a collection's pass once it got past the restore (what this test asks is whether it did)."""


def _events(logs: list[dict]) -> list[str]:
    return [entry["event"] for entry in logs]


@pytest.fixture(autouse=True)
def _no_registry(monkeypatch) -> None:
    monkeypatch.setattr("nexus.catalog.chunk_quarantine.quarantine_collection_name", lambda origin: f"quarantine-{origin}")


def test_a_refused_restore_is_logged_once_for_the_collection_and_the_walk_goes_on() -> None:
    db = _Db(refuse={CODE})
    with capture_logs() as logs:
        _prune_deleted_files(CODE, DOCS, db, catalog=_Catalog())

    assert db.restore_calls == [CODE, DOCS], "the docs collection is still walked after the code one was refused"
    assert db.quarantine_calls == [DOCS], "the refused collection's prune is skipped; the next one runs past its restore"
    refused = [e for e in logs if e["event"] == "gc_restore_origin_unregistered"]
    assert len(refused) == 1, _events(logs)
    assert refused[0]["collection"] == CODE and refused[0]["rows"] == 4
    assert refused[0]["log_level"] == "warning"
    assert "exc_info" not in refused[0], "a typed answer is not a traceback"


def test_a_refusal_is_not_reported_as_a_generic_failed_prune() -> None:
    db = _Db(refuse={CODE, DOCS})
    with capture_logs() as logs:
        _prune_deleted_files(CODE, DOCS, db, catalog=_Catalog())

    assert db.quarantine_calls == []
    assert "gc_serverside_prune_failed" not in _events(logs)
    assert _events(logs).count("gc_restore_origin_unregistered") == 2, "once per collection"


def test_any_other_restore_failure_is_still_the_generic_failed_prune() -> None:
    db = _Db(refuse=set(), other=VectorServiceError("boom", code=500))
    with capture_logs() as logs:
        _prune_deleted_files(CODE, DOCS, db, catalog=_Catalog())

    assert "gc_restore_origin_unregistered" not in _events(logs)
    assert _events(logs).count("gc_serverside_prune_failed") == 2
