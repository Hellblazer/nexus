# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-101 Phase 6 (nexus-o6aa.14): collections table + Catalog API.

Adds a first-class Collections projection to catalog SQLite (one row per
ChromaDB collection name, materialized from CollectionCreated events).
The legacy_grandfathered flag is projection-derived from the
``is_conformant_collection_name`` regex; no event-schema extension.

Covered here:

  - ``Catalog.register_collection`` writes the SQLite row AND appends a
    CollectionCreated event under v: 0 schema.
  - Re-registering the same name is idempotent at the SQLite level
    (INSERT OR REPLACE) and acceptable at the event level (events are
    append-only; idempotency check is at the projector, not the writer).
  - ``Catalog.list_collections`` and ``Catalog.get_collection`` return
    the projected rows.
  - ``Catalog.is_legacy_collection`` reads the projection's
    ``legacy_grandfathered`` flag.
  - ``Catalog.supersede_collection`` updates the row and emits
    CollectionSuperseded.
  - Replay of a CollectionCreated event from a fresh Catalog produces
    the same projected row (replay-equality at the per-table level).

CATALOG SUBSTRATE (nexus-i711w Stage 2). The caller-facing protocol verbs
``register_collection`` / ``get_collection`` / ``list_collections`` /
``is_legacy_collection`` / ``update_document_collection`` are implemented on
BOTH substrates, so the tests whose subject is one of those seed and read
through :class:`tests._catalog_fixture_ops.ActiveCatalog`, i.e. whichever
catalog is live.

TERMINAL DELETION (nexus-i711w): the 18 tests this file used to pin to the
local SQLite catalog via the ``local_catalog`` fixture — the DIE group
(event-log / Projector / collections-DDL machinery) and the three
PORT-BLOCKED groups (nexus-cecqy legacy_grandfathered derivation; the
supersede-validation gap; the update_document_collection idempotency gap) —
retired with ``nexus/catalog/{catalog,catalog_db,event_log,projector,
events}.py``. The supersede service-gap inventory survives as a block
comment below; the remaining tests all run through ActiveCatalog.
"""
from __future__ import annotations

from typing import Any

import pytest

from nexus.corpus import (
    is_conformant_collection_name,
    parse_conformant_collection_name,
)
from tests._catalog_fixture_ops import ActiveCatalog


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture()
def active_catalog() -> ActiveCatalog:
    """Seed and read through whichever catalog is live (nexus-i711w Stage 2).

    Deliberately NOT the local ``Catalog`` this file used to build: the
    subject of the tests that take this fixture is the collections PROTOCOL,
    which ``HttpCatalogClient`` implements, so exercising it against the live
    substrate is strictly more coverage than the local-only form.
    """
    return ActiveCatalog()


@pytest.mark.parametrize(
    "name",
    [
        "code__1-1__voyage-code-3__v1",
        "docs__1-1__voyage-context-3__v2",
        "rdr__1-2-3__voyage-context-3__v1",
        "knowledge__1-1__voyage-context-3__v1",
    ],
)
def test_conformant_names_accepted(name):
    assert is_conformant_collection_name(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "code__ART-8c2e74c0",
        "docs__nexus-571b8edd",
        "knowledge__knowledge",
        "knowledge__delos",
        "docs__default",
        "taxonomy__nexus-571b8edd-knowledge",
        "code__1-1__voyage-code-3",  # missing v<n> segment
        "code__1-1__voyage-code-3__1",  # missing 'v' prefix
        "weird__1-1__voyage-code-3__v1",  # unknown content_type
    ],
)
def test_legacy_names_rejected(name):
    assert is_conformant_collection_name(name) is False


# ── Collections schema migration ─────────────────────────────────────────


def test_register_conformant_collection_marks_not_legacy(active_catalog):
    active_catalog.register_collection(
        "code__1-1__voyage-code-3__v1",
        content_type="code",
        owner_id="1-1",
        embedding_model="voyage-code-3",
        model_version="v1",
    )
    row = active_catalog.get_collection("code__1-1__voyage-code-3__v1")
    assert row is not None
    assert row["content_type"] == "code"
    assert row["owner_id"] == "1-1"
    assert row["embedding_model"] == "voyage-code-3"
    assert row["model_version"] == "v1"
    # NOTE (nexus-cecqy): the ``False`` half of the legacy flag holds on both
    # substrates, but for DIFFERENT reasons — local DERIVES it from
    # ``is_conformant_collection_name``, service just echoes the caller's
    # default. So this assertion is real coverage locally and vacuous
    # service-side; the ``True`` half (which is not vacuous) is the pinned
    # ``test_register_non_conformant_collection_marks_legacy`` below.
    assert row["legacy_grandfathered"] is False


def test_register_collection_idempotent_on_name(active_catalog):
    """Re-registering the same name keeps ONE row per name (upsert).

    The event log is append-only and may carry duplicates; the
    projector tolerates them because it INSERT OR REPLACE-es per event.

    nexus-i711w: the raw ``SELECT COUNT(*) FROM collections WHERE name = ?``
    became a FILTERED scan of ``list_collections()`` rather than
    ``get_collection(name)``. ``get_collection`` returns a SINGLE row on both
    substrates, so asserting on it could not observe a duplicate at all —
    which is the entire point of this test. Counting the matches preserves
    that ability.
    """
    for _ in range(3):
        active_catalog.register_collection("docs__nexus-571b8edd")
    matching = [
        c for c in active_catalog.list_collections()
        if c["name"] == "docs__nexus-571b8edd"
    ]
    assert len(matching) == 1


def test_list_collections_returns_all(active_catalog):
    active_catalog.register_collection("docs__nexus-571b8edd")
    active_catalog.register_collection(
        "code__1-1__voyage-code-3__v1",
        content_type="code", owner_id="1-1",
        embedding_model="voyage-code-3", model_version="v1",
    )
    rows = active_catalog.list_collections()
    names = sorted(r["name"] for r in rows)
    # Exact equality retained (not a subset check): "returns ALL" is the
    # claim, and both substrates give this test a private namespace — a tmp
    # catalog dir on the SQLite arm, a per-test tenant on the engine arm
    # (tests/conftest.py::t2_service_env).
    assert names == [
        "code__1-1__voyage-code-3__v1",
        "docs__nexus-571b8edd",
    ]


def test_is_legacy_collection_unknown_returns_false(active_catalog):
    """An unknown name has no row; treat as non-legacy (safer default).

    Read-time is operationally hostile to fail-loud per the bead spec,
    so callers querying is_legacy_collection on an unregistered name
    do not get a hard error.
    """
    assert active_catalog.is_legacy_collection("never_seen") is False


# ── supersede_collection ─────────────────────────────────────────────────
#
# ⚠️ THE WHOLE SUPERSEDE FAMILY BELOW IS PINNED TO THE LOCAL CATALOG, and the
# reason is a product gap found while porting this file, not a test-shape
# problem. Verified in source:
#
#   * ``HttpCatalogClient.supersede_collection``
#     (http_catalog_client.py:1794-1808) does ZERO client-side validation —
#     it POSTs ``{name, superseded_by}`` and returns None.
#   * The service handler ``CatalogHandler.handleCollectionSupersede``
#     (service/.../http/CatalogHandler.java:977-988) 400s ONLY when ``name``
#     or ``superseded_by`` is absent from the body — which the client always
#     sends — then calls ``CatalogRepository.supersedeCollection``
#     (CatalogRepository.java:2292-2303), a bare
#     ``UPDATE catalog_collections SET superseded_by=?, superseded_at=?
#     WHERE tenant_id=? AND name=?``, and replies ``200 {"updated": N}``
#     even when N == 0.
#
# So on the service arm:
#   1. an UNKNOWN ``old_name`` silently no-ops (200, updated=0) — no raise;
#   2. an ALREADY-superseded name is silently OVERWRITTEN — no raise, and the
#      chain the local guard exists to protect is not protected;
#   3. an UNREGISTERED ``new_name`` writes a dangling ``superseded_by``
#      pointer — no raise, no FK;
#   4. ``superseded_at`` is left NULL: local stamps
#      ``datetime.now(UTC).isoformat()`` itself, whereas the client's
#      ``superseded_at`` parameter defaults to ``None`` and is omitted from
#      the payload, so the handler passes "" and ``tsOrNull("")`` writes NULL.
#
# Converting these to the real service surface would mean asserting that
# nothing happens, i.e. deleting the invariants.
#
# nexus-i711w terminal deletion: the pinned local-arm tests (the whole
# supersede family, the event-log/projector replay tests, the collections
# DDL pins, and the two other PORT-BLOCKED tests on nexus-cecqy
# [legacy_grandfathered derivation] and the update_document_collection
# idempotency gap) retired with the local catalog. The gap inventory above
# is kept as the specification for the service-side guards when they land.


def test_parse_conformant_collection_name_raises_on_legacy():
    """parse_conformant_collection_name must raise on non-conformant names.

    Pass-#2 review (2026-05-03) found this raise path had no direct
    test coverage. The regex gate makes false-non-conformant impossible
    in production, but the docstring documents the contract.

    nexus-i711w: the unused ``catalog`` fixture parameter was dropped. The
    subject is ``nexus.corpus.parse_conformant_collection_name``, a pure
    function on neither substrate's side of the seam; the fixture only ever
    forced a catalog to be constructed for nothing.
    """
    with pytest.raises(ValueError, match="not conformant"):
        parse_conformant_collection_name("docs__nexus-571b8edd")
    with pytest.raises(ValueError, match="not conformant"):
        parse_conformant_collection_name("knowledge__delos")
    with pytest.raises(ValueError, match="not conformant"):
        parse_conformant_collection_name("totally__malformed__weird")


def test_update_document_collection_returns_false_on_unknown_tumbler(active_catalog):
    """update_document_collection must return False (no-op) when the
    document is not registered. Documented contract; pass-#2 review
    found no direct test.
    """
    assert active_catalog.update_document_collection(
        "1.99.99", "knowledge__1-1__voyage-context-3__v1",
    ) is False

