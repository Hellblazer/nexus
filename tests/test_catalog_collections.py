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

Everything else in this file is PINNED to the local SQLite catalog via the
``local_catalog`` fixture, in four groups, each stated at its own site:

  1. DIE — subject IS the local event-sourcing machinery (``events.jsonl``,
     the ``Projector``, the ``collections`` table DDL). These retire with
     ``nexus/catalog/{catalog,catalog_db,event_log,projector,events}.py``.
  2. PORT-BLOCKED on nexus-cecqy — ``legacy_grandfathered`` is never
     DERIVED service-side.
  3. PORT-BLOCKED on the supersede-validation gap — see the block comment
     above ``test_supersede_collection_marks_old_and_emits_event``:
     ``supersede_collection`` performs no validation and stamps no
     ``superseded_at`` on the service arm.
  4. PORT-BLOCKED on the ``update_document_collection`` idempotency gap —
     see ``test_update_document_collection_idempotent_on_same_target``.

``nexus.catalog.catalog`` / ``event_log`` / ``events`` / ``projector`` are
therefore imported INSIDE the bodies that still need them, never at module
scope, so this file still COLLECTS once the local catalog is deleted.
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


@pytest.fixture()
def local_catalog(tmp_path, local_catalog_backend):
    """A real LOCAL SQLite ``Catalog`` rooted in tmp_path.

    ONLY for tests whose subject is local-catalog-specific, or whose
    assertion has no service-mode expression yet (see the module docstring's
    three groups). ``local_catalog_backend`` is requested so the pin is
    explicit rather than incidental: without it these pass only because a
    fresh tmp ``.catalog.db`` does not exist yet, and ``Catalog`` forces
    ``read_only=True`` over an EXISTING file when the backend is service.
    """
    from nexus.catalog.catalog import Catalog

    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    db_path = tmp_path / "catalog.sqlite"
    return Catalog(catalog_dir=catalog_dir, db_path=db_path)


def _created_events(catalog: Any) -> list:
    """``CollectionCreated`` events from *catalog*'s LOCAL event log."""
    from nexus.catalog.event_log import EventLog
    from nexus.catalog.events import TYPE_COLLECTION_CREATED

    return [
        e for e in EventLog(catalog._dir).replay()
        if e.type == TYPE_COLLECTION_CREATED
    ]


def _superseded_events(catalog: Any) -> list:
    """``CollectionSuperseded`` events from *catalog*'s LOCAL event log."""
    from nexus.catalog.event_log import EventLog
    from nexus.catalog.events import TYPE_COLLECTION_SUPERSEDED

    return [
        e for e in EventLog(catalog._dir).replay()
        if e.type == TYPE_COLLECTION_SUPERSEDED
    ]


# ── is_conformant_collection_name ────────────────────────────────────────


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


def test_collections_table_exists(local_catalog):
    """The collections table is part of the catalog schema.

    nexus-i711w: DIE. Its subject is the LOCAL SQLite ``collections`` DDL —
    ``sqlite_master`` has no service-mode expression at all (the engine's
    schema is Liquibase-managed and covered by the Java suite). Retires with
    ``nexus/catalog/catalog_db.py``.
    """
    rows = local_catalog._db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='collections'"
    ).fetchall()
    assert rows, "collections table must exist after Catalog construction"


def test_collections_columns(local_catalog):
    """The collections table has the expected columns.

    nexus-i711w: DIE, same reason as ``test_collections_table_exists`` —
    ``PRAGMA table_info`` is a SQLite-only observable.
    """
    cols = {
        row[1]
        for row in local_catalog._db.execute("PRAGMA table_info(collections)").fetchall()
    }
    expected = {
        "name", "content_type", "owner_id", "embedding_model",
        "model_version", "display_name", "legacy_grandfathered",
        "superseded_by", "superseded_at", "created_at",
    }
    missing = expected - cols
    assert not missing, f"missing columns: {missing}"


# ── register_collection ──────────────────────────────────────────────────


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


def test_register_non_conformant_collection_marks_legacy(local_catalog):
    """A non-conformant name is registered with legacy_grandfathered=True.

    nexus-cecqy: PORT-BLOCKED, pinned to the local catalog so it keeps
    passing. ``legacy_grandfathered`` is never DERIVED on the service arm —
    ``HttpCatalogClient.register_collection`` takes it as a plain
    ``bool = False`` parameter and posts whatever it was handed, so a
    non-conformant name registers as NOT legacy and this assertion inverts.
    THE FIX BELONGS IN ``HttpCatalogClient.register_collection`` (derive from
    ``is_conformant_collection_name`` when the caller did not say), NOT at a
    call site: there are three bare ``register_collection()`` call sites, so
    fixing one leaves the other two wrong.

    This is the regression test for cecqy — do not convert or delete it.
    """
    local_catalog.register_collection("docs__nexus-571b8edd")
    row = local_catalog.get_collection("docs__nexus-571b8edd")
    assert row is not None
    assert row["legacy_grandfathered"] is True


def test_register_collection_writes_event(local_catalog):
    """nexus-i711w: DIE. ``events.jsonl`` IS the subject; the service arm
    emits no event log, so there is no observable to port this to.
    """
    local_catalog.register_collection(
        "code__1-1__voyage-code-3__v1",
        content_type="code",
        owner_id="1-1",
        embedding_model="voyage-code-3",
        model_version="v1",
    )
    created = _created_events(local_catalog)
    assert len(created) == 1
    assert created[0].payload.coll_id == "code__1-1__voyage-code-3__v1"
    assert created[0].payload.content_type == "code"
    assert created[0].payload.embedding_model == "voyage-code-3"


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


def test_is_legacy_collection_reads_projection(local_catalog):
    """nexus-cecqy: PORT-BLOCKED, pinned. Same defect as
    ``test_register_non_conformant_collection_marks_legacy`` —
    ``is_legacy_collection`` reads a flag the service arm never derives, so
    the ``knowledge__delos`` -> ``True`` assertion inverts there. Fix location
    is ``HttpCatalogClient.register_collection``, not a call site.
    """
    local_catalog.register_collection("knowledge__delos")
    local_catalog.register_collection(
        "code__1-1__voyage-code-3__v1",
        content_type="code", owner_id="1-1",
        embedding_model="voyage-code-3", model_version="v1",
    )
    assert local_catalog.is_legacy_collection("knowledge__delos") is True
    assert local_catalog.is_legacy_collection("code__1-1__voyage-code-3__v1") is False


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
# nothing happens, i.e. deleting the invariants. They stay pinned, passing,
# and become the regression tests when the guards land service-side.


def test_supersede_collection_marks_old_and_emits_event(local_catalog):
    """PINNED — see the supersede-family note above.

    Both halves are unportable today: the event log does not exist on the
    service arm, and ``superseded_at`` is left NULL there (defect 4), so even
    the row-state half (``assert old["superseded_at"]``) inverts.
    """
    local_catalog.register_collection("docs__nexus-571b8edd")
    local_catalog.register_collection(
        "docs__1-1__voyage-context-3__v1",
        content_type="docs", owner_id="1-1",
        embedding_model="voyage-context-3", model_version="v1",
    )
    local_catalog.supersede_collection(
        "docs__nexus-571b8edd",
        "docs__1-1__voyage-context-3__v1",
        reason="rename to canonical",
    )
    old = local_catalog.get_collection("docs__nexus-571b8edd")
    assert old is not None
    assert old["superseded_by"] == "docs__1-1__voyage-context-3__v1"
    assert old["superseded_at"]

    events = _superseded_events(local_catalog)
    assert len(events) == 1
    assert events[0].payload.old_coll_id == "docs__nexus-571b8edd"
    assert events[0].payload.new_coll_id == "docs__1-1__voyage-context-3__v1"


def test_supersede_unknown_old_collection_raises(local_catalog):
    """PINNED — supersede-family defect 1 (unknown old_name silently no-ops
    service-side, HTTP 200 / updated=0, no exception). See the note above.
    """
    local_catalog.register_collection("docs__1-1__voyage-context-3__v1",
                                      content_type="docs", owner_id="1-1",
                                      embedding_model="voyage-context-3",
                                      model_version="v1")
    with pytest.raises(ValueError, match="not registered"):
        local_catalog.supersede_collection(
            "never_seen",
            "docs__1-1__voyage-context-3__v1",
        )


def test_supersede_already_superseded_raises(local_catalog):
    """Superseding a name that already has superseded_by set is rejected;
    silently overwriting would orphan the prior CollectionSuperseded
    event in the log.

    PINNED — supersede-family defect 2: the service arm performs exactly the
    silent overwrite this guard forbids. See the note above.
    """
    local_catalog.register_collection("docs__nexus-571b8edd")
    local_catalog.register_collection(
        "docs__1-1__voyage-context-3__v1",
        content_type="docs", owner_id="1-1",
        embedding_model="voyage-context-3", model_version="v1",
    )
    local_catalog.register_collection(
        "docs__1-1__voyage-context-3__v2",
        content_type="docs", owner_id="1-1",
        embedding_model="voyage-context-3", model_version="v2",
    )
    local_catalog.supersede_collection(
        "docs__nexus-571b8edd", "docs__1-1__voyage-context-3__v1",
    )
    with pytest.raises(ValueError, match="already superseded"):
        local_catalog.supersede_collection(
            "docs__nexus-571b8edd", "docs__1-1__voyage-context-3__v2",
        )


def test_supersede_unregistered_new_raises(local_catalog):
    """Refuse to point superseded_by at a non-existent collection;
    that produces a dangling pointer no foreign-key-style join can
    resolve.

    PINNED — supersede-family defect 3: the service arm writes the dangling
    pointer. See the note above.
    """
    local_catalog.register_collection("docs__nexus-571b8edd")
    with pytest.raises(ValueError, match="new .* is not.*registered"):
        local_catalog.supersede_collection(
            "docs__nexus-571b8edd", "docs__never-registered",
        )


def test_register_collection_short_circuits_on_identical_re_call(local_catalog):
    """Re-calling register_collection with identical canonical fields
    must NOT append a duplicate event (log-bloat smell).

    nexus-i711w: DIE — the assertion counts EVENTS, and the service arm has
    no event log to count.
    """
    local_catalog.register_collection(
        "code__1-1__voyage-code-3__v1",
        content_type="code", owner_id="1-1",
        embedding_model="voyage-code-3", model_version="v1",
    )
    events_after_first = _created_events(local_catalog)
    local_catalog.register_collection(
        "code__1-1__voyage-code-3__v1",
        content_type="code", owner_id="1-1",
        embedding_model="voyage-code-3", model_version="v1",
    )
    events_after_second = _created_events(local_catalog)
    assert len(events_after_first) == 1
    assert len(events_after_second) == 1


def test_register_collection_re_emits_on_field_change(local_catalog):
    """If a canonical field changes between calls, the new event is
    emitted so the projection picks up the new value.

    nexus-i711w: DIE — event-count assertion, no service-arm observable.
    """
    local_catalog.register_collection("code__nexus-571b8edd")  # legacy form, empty fields
    local_catalog.register_collection(
        "code__nexus-571b8edd",
        embedding_model="voyage-code-3",  # operator filling in metadata
    )
    events = _created_events(local_catalog)
    assert len(events) == 2  # both calls emitted


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


def test_v0_collection_superseded_blank_id_guard(local_catalog):
    """Direct projector test: a malformed CollectionSuperseded event
    with empty old_coll_id or new_coll_id is treated as a no-op,
    not crashed.

    Pass-#2 review found the guard was untested; if it were silently
    removed the doctor's replay-equality check would still pass
    against well-formed events while crashing on a single replay of a
    malformed line.

    nexus-i711w: DIE. The SUBJECT is ``Projector._v0_collection_superseded``
    — a local-only class driven here directly. Retires with
    ``nexus/catalog/projector.py``.
    """
    from nexus.catalog.events import CollectionSupersededPayload, make_event

    local_catalog.register_collection("docs__nexus-571b8edd")

    # Both fields missing
    event_blank_old = make_event(
        CollectionSupersededPayload(old_coll_id="", new_coll_id="x"), v=0,
    )
    local_catalog._projector.apply(event_blank_old)
    local_catalog._db.commit()
    # Row unchanged
    assert local_catalog.get_collection("docs__nexus-571b8edd")["superseded_by"] == ""

    event_blank_new = make_event(
        CollectionSupersededPayload(
            old_coll_id="docs__nexus-571b8edd", new_coll_id="",
        ),
        v=0,
    )
    local_catalog._projector.apply(event_blank_new)
    local_catalog._db.commit()
    assert local_catalog.get_collection("docs__nexus-571b8edd")["superseded_by"] == ""


def test_legacy_grandfathered_frozen_on_event_survives_regex_change(
    local_catalog, monkeypatch,
):
    """nexus-7m8n: ``legacy_grandfathered`` is frozen on the
    ``CollectionCreated`` event at write time. A subsequent change to
    :func:`nexus.corpus.is_conformant_collection_name` does NOT flip
    the projected row when the event log is replayed.

    Pre-fix shape: the projector evaluated the regex live on every
    replay. Extending the regex (e.g. to allow a new content type)
    silently re-classified historic rows; same log produced different
    projected state across releases.

    Post-fix shape: the writer populates
    ``CollectionCreatedPayload.legacy_grandfathered`` with the regex
    result at write time; the projector reads from the payload and
    only falls back to the live regex for older events that lack the
    field (``legacy_grandfathered`` is ``None``).
    """
    from nexus.catalog import events as ev
    from nexus.catalog.event_log import EventLog
    from nexus.catalog.projector import Projector

    local_catalog.register_collection("docs__1-1__voyage-context-3__v1")
    row_before = local_catalog.get_collection("docs__1-1__voyage-context-3__v1")
    assert row_before is not None
    assert row_before["legacy_grandfathered"] == 0, (
        "precondition: a conformant name must register as not-legacy"
    )

    # Reset the projection so we can replay from the event log into a
    # fresh state and observe what the projector materialises.
    local_catalog._db.execute("DELETE FROM collections")  # epsilon-allow: test resets projection state to observe a fresh replay through the projector
    local_catalog._db.commit()

    # Mutate the regex to a degenerate "everything is non-conformant"
    # form. Pre-fix this would flip the projected row on replay; post-
    # fix the payload's frozen value wins.
    import nexus.corpus
    monkeypatch.setattr(
        nexus.corpus, "is_conformant_collection_name", lambda _name: False,
    )

    # Replay the event log through a fresh projector instance bound
    # to the same DB.
    projector = Projector(local_catalog._db)
    for event in EventLog(local_catalog._dir).replay():
        projector.apply(event)
    local_catalog._db.commit()

    row_after = local_catalog.get_collection("docs__1-1__voyage-context-3__v1")
    assert row_after is not None, "row must materialise from the replay"
    assert row_after["legacy_grandfathered"] == 0, (
        f"replay must read legacy_grandfathered from the payload, not "
        f"the live regex; got {row_after['legacy_grandfathered']!r}. "
        f"Pre-7m8n the regex monkeypatch would have flipped this to 1."
    )


def test_legacy_grandfathered_falls_back_to_regex_for_pre_7m8n_events(local_catalog):
    """nexus-7m8n: events synthesized before the payload field landed
    deserialize with ``legacy_grandfathered=None``. The projector
    falls back to evaluating ``is_conformant_collection_name`` so
    pre-7m8n event logs continue to project correctly.
    """
    from nexus.catalog.events import CollectionCreatedPayload, make_event

    # Manually craft a payload with legacy_grandfathered=None to
    # simulate a pre-7m8n synthesized event (Event.from_dict on a
    # JSONL line lacking the field would land here).
    payload = CollectionCreatedPayload(
        coll_id="docs__legacy-style-name",
        owner_id="owner",
        content_type="docs",
        embedding_model="voyage-context-3",
        model_version="1",
        legacy_grandfathered=None,
    )
    event = make_event(payload, v=0)

    local_catalog._projector.apply(event)
    local_catalog._db.commit()

    row = local_catalog.get_collection("docs__legacy-style-name")
    assert row is not None
    # The fallback evaluates the live regex; "docs__legacy-style-name"
    # is not conformant under the current regex, so the projector
    # marks it legacy.
    assert row["legacy_grandfathered"] == 1, (
        "pre-7m8n event (legacy_grandfathered=None) must fall back to "
        "the live regex; a non-conformant name should project as legacy."
    )


def test_v0_collection_superseded_replay_is_deterministic(local_catalog):
    """nexus-qpet.1: replaying the same supersede event twice must
    produce the same ``superseded_at`` value.

    Pre-fix shape: an event with empty payload.superseded_at fell back
    to ``datetime.now(UTC).isoformat()``. Each replay produced a
    different timestamp; replay-equality drifted.

    Post-fix shape: empty payload.superseded_at falls back to "" (the
    schema default), matching the pattern used by
    ``_v0_collection_created`` for ``created_at``. Two replays produce
    identical projected rows.

    The fallback is dead code in production today (Phase 6 always
    populates the field). The fix protects against future synthesizers
    that might emit pre-amendment-shaped events.

    nexus-i711w: DIE — the subject is ``Projector._v0_collection_superseded``
    replay determinism, a local-only pipeline. Retires with
    ``nexus/catalog/projector.py``.
    """
    from nexus.catalog.events import CollectionSupersededPayload, make_event

    local_catalog.register_collection("docs__nexus-571b8edd")
    local_catalog.register_collection("docs__1-1__voyage-context-3__v1")

    # Manually craft an event with EMPTY superseded_at to exercise the
    # fallback. Production callers always set it, but a synthesizer
    # replaying older event logs might not.
    blank_ts_event = make_event(
        CollectionSupersededPayload(
            old_coll_id="docs__nexus-571b8edd",
            new_coll_id="docs__1-1__voyage-context-3__v1",
            superseded_at="",
        ),
        v=0,
    )

    local_catalog._projector.apply(blank_ts_event)
    local_catalog._db.commit()
    first_ts = local_catalog.get_collection("docs__nexus-571b8edd")["superseded_at"]

    # Reset the row's superseded_at column without re-emitting an event,
    # then replay the same event. Deterministic projector means same ts.
    local_catalog._db.execute(  # epsilon-allow: test resets a single column to observe deterministic projector replay
        "UPDATE collections SET superseded_at = '' WHERE name = ?",
        ("docs__nexus-571b8edd",),
    )
    local_catalog._db.commit()

    local_catalog._projector.apply(blank_ts_event)
    local_catalog._db.commit()
    second_ts = local_catalog.get_collection("docs__nexus-571b8edd")["superseded_at"]

    assert first_ts == second_ts, (
        f"replay must be deterministic; got first={first_ts!r}, "
        f"second={second_ts!r}. The pre-fix fallback was "
        f"datetime.now(UTC).isoformat() which changed per call."
    )


def test_update_document_collection_returns_false_on_unknown_tumbler(active_catalog):
    """update_document_collection must return False (no-op) when the
    document is not registered. Documented contract; pass-#2 review
    found no direct test.
    """
    assert active_catalog.update_document_collection(
        "1.99.99", "knowledge__1-1__voyage-context-3__v1",
    ) is False


def test_update_document_collection_idempotent_on_same_target(local_catalog):
    """Re-pointing a doc to its current physical_collection is a no-op
    (returns False; no event written).

    ⚠️ PORT-BLOCKED, PINNED. Found by attempting the port and MEASURED, not
    predicted: with ``active_catalog`` this fails ``assert True is False``.

    The documented contract (``catalog_writes.py``:209-210) is "Returns True
    if the doc was re-pointed, False if not found OR already pointed at
    ``new_collection`` (idempotent)". The local writer honours it via
    ``_update_document_collection_locked``, which short-circuits on an
    unchanged value. ``HttpCatalogClient.update_document_collection``
    (http_catalog_client.py:1844-1850) has no such short-circuit: it POSTs
    ``/update`` and returns ``updated > 0``, and the service's UPDATE reports
    one affected row whether or not the value changed — so the idempotent
    no-op reports True there.

    That matters to callers who branch on the return: ``nx catalog
    migrate-fallback``-style loops count "re-pointed" documents, so on the
    service arm every already-correct document is counted as moved.

    Kept as the local contract test rather than converted — asserting True
    here would enshrine the divergence. nexus-i711w: the raw
    ``INSERT INTO documents`` seed (which pinned the literal tumbler
    ``1.5.1``) is nonetheless gone in favour of ``register_owner`` +
    ``register``; nothing here asserts on the tumbler VALUE, only that the
    document exists and is already pointed at ``knowledge__delos``.
    """
    owner = local_catalog.register_owner("collections-idem-target", "curator")
    tumbler = local_catalog.register(
        owner,
        "doc-idem-target",
        content_type="text",
        file_path="/tmp/x.md",
        physical_collection="knowledge__delos",
        chunk_count=1,
    )
    # Non-vacuous: a seed the catalog under read could not see would make the
    # call a not-found no-op, which ALSO returns False — so assert the
    # precondition that distinguishes "already at target" from "absent".
    entry = local_catalog.resolve(tumbler)
    assert entry is not None, "seed must be visible to the catalog under test"
    assert entry.physical_collection == "knowledge__delos"

    assert local_catalog.update_document_collection(
        str(tumbler), "knowledge__delos",  # already at this collection
    ) is False


def test_idempotent_supersede_skipped_due_to_already_superseded(local_catalog):
    """A second supersede on the same name must NOT silently extend
    the chain; the test_supersede_already_superseded_raises test covers
    the raise. This case also confirms no extra event lands in the
    log if the call raised.

    PINNED — supersede-family defects 2 + the missing event log. Both of this
    test's observables are absent service-side: the second call does not
    raise there (it overwrites), and there is no event log whose length could
    witness "did not write". See the supersede-family note above.
    """
    local_catalog.register_collection("docs__nexus-571b8edd")
    local_catalog.register_collection(
        "docs__1-1__voyage-context-3__v1",
        content_type="docs", owner_id="1-1",
        embedding_model="voyage-context-3", model_version="v1",
    )
    local_catalog.register_collection(
        "docs__1-1__voyage-context-3__v2",
        content_type="docs", owner_id="1-1",
        embedding_model="voyage-context-3", model_version="v2",
    )
    local_catalog.supersede_collection(
        "docs__nexus-571b8edd", "docs__1-1__voyage-context-3__v1",
    )
    with pytest.raises(ValueError):
        local_catalog.supersede_collection(
            "docs__nexus-571b8edd", "docs__1-1__voyage-context-3__v2",
        )
    events = _superseded_events(local_catalog)
    assert len(events) == 1  # second call raised, did not write


# ── Projector replay ─────────────────────────────────────────────────────


def test_register_collection_replay_produces_same_row(local_catalog, tmp_path):
    """Replaying the events.jsonl into a fresh Catalog produces the
    same projected row.

    Tests the projector's CollectionCreated handler in isolation, not
    the convenience writer.

    nexus-i711w: DIE. Replay-equality is defined only over the local
    ``events.jsonl`` -> ``Projector`` -> SQLite pipeline. Retires with
    ``nexus/catalog/{event_log,projector}.py``.
    """
    from nexus.catalog.catalog import Catalog
    from nexus.catalog.event_log import EventLog

    local_catalog.register_collection(
        "code__1-1__voyage-code-3__v1",
        content_type="code",
        owner_id="1-1",
        embedding_model="voyage-code-3",
        model_version="v1",
    )
    expected = local_catalog.get_collection("code__1-1__voyage-code-3__v1")

    # Fresh catalog over the same dir but a new sqlite path,
    # then replay events into it. The original catalog stays open;
    # SQLite handles concurrent connections to separate paths.
    fresh_db = tmp_path / "fresh.sqlite"
    fresh = Catalog(catalog_dir=local_catalog._dir, db_path=fresh_db)
    for event in EventLog(local_catalog._dir).replay():
        fresh._projector.apply(event)
    fresh._db.commit()

    actual = fresh.get_collection("code__1-1__voyage-code-3__v1")
    assert actual is not None
    assert actual["content_type"] == expected["content_type"]
    assert actual["owner_id"] == expected["owner_id"]
    assert actual["embedding_model"] == expected["embedding_model"]
    assert actual["model_version"] == expected["model_version"]
    assert actual["legacy_grandfathered"] == expected["legacy_grandfathered"]
