# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-104 Step 0: regression for ``DELETE FROM collections`` in rebuild.

The pre-fix ``Catalog._ensure_consistent`` (event-sourced path) and
``CatalogDB.rebuild`` (legacy path) both DELETE ``owners``, ``documents``,
and ``links`` before reloading. The ``collections`` table was excluded.
Combined with ``_v0_collection_created``'s ``INSERT OR REPLACE`` plus the
``COALESCE``-preservation pattern for ``superseded_by`` /
``superseded_at`` / ``created_at``, the rebuild silently inherited stale
supersede metadata that no replay event re-validated.

Round 3 PASSED preserves the COALESCE because it is load-bearing for the
degraded-path retry case (incremental rolls back mid-delta → marker stays
put → next retry replays the same delta against an un-cleared
``collections`` table). The Step 0 fix adds the missing DELETE without
touching the projector verb.

Tests pin both paths plus the round-trip through CollectionSuperseded.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus.catalog.catalog import Catalog
from nexus.catalog.catalog_db import CatalogDB


def _seed_catalog_with_collection(catalog_dir: Path) -> str:
    """Populate a catalog dir with one owner, one doc, one collection.

    ``register`` writes ``documents.jsonl`` so a fresh ``Catalog()`` over
    the same dir hits the ``self._documents_path.exists()`` branch and
    runs ``_ensure_consistent``. Returns the registered collection name.
    """
    cat = Catalog(catalog_dir, catalog_dir / "catalog.db")
    owner = cat.register_owner(
        name="seed-owner", owner_type="repo", repo_hash="seed-hash",
    )
    cat.register(
        owner=owner, title="seed-doc", content_type="prose", file_path="seed.md",
    )
    coll_name = "code__1-1__voyage-code-3__v1"
    cat.register_collection(
        coll_name,
        content_type="code",
        owner_id="1-1",
        embedding_model="voyage-code-3",
        model_version="v1",
    )
    cat._db.close()
    return coll_name


def _force_rebuild(catalog_dir: Path) -> None:
    """Wipe the consistency marker so the next ``Catalog()`` rebuilds.

    ``_ensure_consistent`` short-circuits when ``current_mtime <=
    last_consistency_mtime``. Deleting the row sets the marker back to
    0.0 on read, which always trips the rebuild against any non-empty
    canonical truth.
    """
    db = CatalogDB(catalog_dir / "catalog.db")
    try:
        with db.transaction():
            db.execute("DELETE FROM _meta WHERE key = ?", ("last_consistency_mtime",))
    finally:
        db.close()


def test_event_sourced_rebuild_clears_stale_collections_metadata(tmp_path: Path) -> None:
    """Stale supersede metadata in ``collections`` is wiped on rebuild.

    Setup the catalog with a registered collection (no supersede). Mutate
    the row directly to plant a fake ``superseded_by`` value. Force a
    rebuild. The replay's ``CollectionCreated`` event carries
    ``superseded_by=""`` per payload defaults; the COALESCE in
    ``_v0_collection_created`` would preserve the planted value across
    the rebuild without the Step 0 DELETE. With the DELETE, the table
    is cleared first and the replay re-projects the empty supersede
    fields from the event payload.
    """
    coll_name = _seed_catalog_with_collection(tmp_path)

    db = CatalogDB(tmp_path / "catalog.db")
    try:
        with db.transaction():
            db.execute(
                "UPDATE collections SET superseded_by = ?, superseded_at = ? "
                "WHERE name = ?",
                ("STALE-SUPERSEDED-BY", "1970-01-01T00:00:00+00:00", coll_name),
            )
    finally:
        db.close()

    _force_rebuild(tmp_path)

    cat = Catalog(tmp_path, tmp_path / "catalog.db")
    row = cat.get_collection(coll_name)
    assert row is not None
    assert row["superseded_by"] == "", (
        "rebuild must DELETE FROM collections so the replay's empty "
        "supersede metadata wins over the planted stale value"
    )
    assert row["superseded_at"] == ""


def test_event_sourced_rebuild_replay_preserves_supersede_round_trip(
    tmp_path: Path,
) -> None:
    """Rebuild round-trips ``CollectionSuperseded`` correctly.

    Register A and B, supersede A→B, force rebuild. The replay applies
    ``CollectionCreated(A)`` with empty supersede fields, then
    ``CollectionCreated(B)``, then ``CollectionSuperseded(A, B)`` which
    UPDATEs A's row. Final state must show A.superseded_by=B.

    Without the Step 0 DELETE this still passes (the UPDATE re-applies),
    but combined with the ``test_..._clears_stale_collections_metadata``
    test above it confirms the DELETE does not regress the legitimate
    round-trip.
    """
    cat = Catalog(tmp_path, tmp_path / "catalog.db")
    owner = cat.register_owner(
        name="seed-owner", owner_type="repo", repo_hash="seed-hash",
    )
    cat.register(
        owner=owner, title="seed-doc", content_type="prose", file_path="seed.md",
    )
    old_name = "docs__nexus-571b8edd"
    new_name = "docs__1-1__voyage-context-3__v1"
    cat.register_collection(old_name)
    cat.register_collection(
        new_name,
        content_type="docs",
        owner_id="1-1",
        embedding_model="voyage-context-3",
        model_version="v1",
    )
    cat.supersede_collection(old_name, new_name, reason="rename to canonical")
    cat._db.close()

    _force_rebuild(tmp_path)

    cat = Catalog(tmp_path, tmp_path / "catalog.db")
    old = cat.get_collection(old_name)
    assert old is not None
    assert old["superseded_by"] == new_name
    assert old["superseded_at"]  # non-empty ISO timestamp
    new = cat.get_collection(new_name)
    assert new is not None
    assert new["superseded_by"] == ""


def test_legacy_rebuild_clears_stale_collections_table(tmp_path: Path) -> None:
    """``CatalogDB.rebuild`` clears ``collections`` even with no events.

    The legacy rebuild path takes ``owners`` / ``documents`` / ``links``
    dicts and reloads them. ``collections`` is event-sourced exclusively
    (no legacy JSONL counterpart), so the legacy rebuild path's
    contribution is to clear any pre-existing rows so an event-sourced
    replay (which always follows when ``events.jsonl`` exists) lands on
    a clean slate. This test pins the DELETE in the legacy path
    independently of the orchestrator.
    """
    db = CatalogDB(tmp_path / "catalog.db")
    try:
        with db.transaction():
            db.execute(
                "INSERT INTO collections "
                "(name, content_type, owner_id, embedding_model, model_version, "
                "display_name, legacy_grandfathered, superseded_by, "
                "superseded_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "code__1-1__voyage-code-3__v1",
                    "code",
                    "1-1",
                    "voyage-code-3",
                    "v1",
                    "code__1-1__voyage-code-3__v1",
                    0,
                    "",
                    "",
                    "1970-01-01T00:00:00+00:00",
                ),
            )

        db.rebuild(owners={}, documents={}, links=[])

        rows = db.execute("SELECT name FROM collections").fetchall()
        assert rows == [], (
            "CatalogDB.rebuild must DELETE FROM collections so a "
            "subsequent event-sourced replay starts from an empty "
            "table; without the DELETE, stale rows from a prior run "
            "persist forever even when no event re-validates them"
        )
    finally:
        db.close()
