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
"""
from __future__ import annotations

import pytest

from nexus.catalog.catalog import Catalog
from nexus.catalog.event_log import EventLog
from nexus.catalog.events import (
    CollectionSupersededPayload,
    TYPE_COLLECTION_CREATED,
    TYPE_COLLECTION_SUPERSEDED,
    make_event,
)
from nexus.corpus import (
    is_conformant_collection_name,
    parse_conformant_collection_name,
)


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture()
def catalog(tmp_path):
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    db_path = tmp_path / "catalog.sqlite"
    return Catalog(catalog_dir=catalog_dir, db_path=db_path)


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


def test_collections_table_exists(catalog):
    """The collections table is part of the catalog schema."""
    rows = catalog._db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='collections'"
    ).fetchall()
    assert rows, "collections table must exist after Catalog construction"


def test_collections_columns(catalog):
    """The collections table has the expected columns."""
    cols = {
        row[1]
        for row in catalog._db.execute("PRAGMA table_info(collections)").fetchall()
    }
    expected = {
        "name", "content_type", "owner_id", "embedding_model",
        "model_version", "display_name", "legacy_grandfathered",
        "superseded_by", "superseded_at", "created_at",
    }
    missing = expected - cols
    assert not missing, f"missing columns: {missing}"


# ── register_collection ──────────────────────────────────────────────────


def test_register_conformant_collection_marks_not_legacy(catalog):
    catalog.register_collection(
        "code__1-1__voyage-code-3__v1",
        content_type="code",
        owner_id="1-1",
        embedding_model="voyage-code-3",
        model_version="v1",
    )
    row = catalog.get_collection("code__1-1__voyage-code-3__v1")
    assert row is not None
    assert row["content_type"] == "code"
    assert row["owner_id"] == "1-1"
    assert row["embedding_model"] == "voyage-code-3"
    assert row["model_version"] == "v1"
    assert row["legacy_grandfathered"] is False


def test_register_non_conformant_collection_marks_legacy(catalog):
    """A non-conformant name is registered with legacy_grandfathered=True."""
    catalog.register_collection("docs__nexus-571b8edd")
    row = catalog.get_collection("docs__nexus-571b8edd")
    assert row is not None
    assert row["legacy_grandfathered"] is True


def test_register_collection_writes_event(catalog):
    catalog.register_collection(
        "code__1-1__voyage-code-3__v1",
        content_type="code",
        owner_id="1-1",
        embedding_model="voyage-code-3",
        model_version="v1",
    )
    events = list(EventLog(catalog._dir).replay())
    created = [e for e in events if e.type == TYPE_COLLECTION_CREATED]
    assert len(created) == 1
    assert created[0].payload.coll_id == "code__1-1__voyage-code-3__v1"
    assert created[0].payload.content_type == "code"
    assert created[0].payload.embedding_model == "voyage-code-3"


def test_register_collection_idempotent_on_name(catalog):
    """Re-registering the same name is a no-op at the SQLite level
    (INSERT OR REPLACE keeps one row per name).

    The event log is append-only and may carry duplicates; the
    projector tolerates them because it INSERT OR REPLACE-es per event.
    """
    for _ in range(3):
        catalog.register_collection("docs__nexus-571b8edd")
    rows = catalog._db.execute(
        "SELECT COUNT(*) FROM collections WHERE name = ?",
        ("docs__nexus-571b8edd",),
    ).fetchone()
    assert rows[0] == 1


def test_list_collections_returns_all(catalog):
    catalog.register_collection("docs__nexus-571b8edd")
    catalog.register_collection(
        "code__1-1__voyage-code-3__v1",
        content_type="code", owner_id="1-1",
        embedding_model="voyage-code-3", model_version="v1",
    )
    rows = catalog.list_collections()
    names = sorted(r["name"] for r in rows)
    assert names == [
        "code__1-1__voyage-code-3__v1",
        "docs__nexus-571b8edd",
    ]


def test_is_legacy_collection_reads_projection(catalog):
    catalog.register_collection("knowledge__delos")
    catalog.register_collection(
        "code__1-1__voyage-code-3__v1",
        content_type="code", owner_id="1-1",
        embedding_model="voyage-code-3", model_version="v1",
    )
    assert catalog.is_legacy_collection("knowledge__delos") is True
    assert catalog.is_legacy_collection("code__1-1__voyage-code-3__v1") is False


def test_is_legacy_collection_unknown_returns_false(catalog):
    """An unknown name has no row; treat as non-legacy (safer default).

    Read-time is operationally hostile to fail-loud per the bead spec,
    so callers querying is_legacy_collection on an unregistered name
    do not get a hard error.
    """
    assert catalog.is_legacy_collection("never_seen") is False


# ── supersede_collection ─────────────────────────────────────────────────


def test_supersede_collection_marks_old_and_emits_event(catalog):
    catalog.register_collection("docs__nexus-571b8edd")
    catalog.register_collection(
        "docs__1-1__voyage-context-3__v1",
        content_type="docs", owner_id="1-1",
        embedding_model="voyage-context-3", model_version="v1",
    )
    catalog.supersede_collection(
        "docs__nexus-571b8edd",
        "docs__1-1__voyage-context-3__v1",
        reason="rename to canonical",
    )
    old = catalog.get_collection("docs__nexus-571b8edd")
    assert old is not None
    assert old["superseded_by"] == "docs__1-1__voyage-context-3__v1"
    assert old["superseded_at"]

    events = [
        e for e in EventLog(catalog._dir).replay()
        if e.type == TYPE_COLLECTION_SUPERSEDED
    ]
    assert len(events) == 1
    assert events[0].payload.old_coll_id == "docs__nexus-571b8edd"
    assert events[0].payload.new_coll_id == "docs__1-1__voyage-context-3__v1"


def test_supersede_unknown_old_collection_raises(catalog):
    catalog.register_collection("docs__1-1__voyage-context-3__v1",
                                content_type="docs", owner_id="1-1",
                                embedding_model="voyage-context-3",
                                model_version="v1")
    with pytest.raises(ValueError, match="not registered"):
        catalog.supersede_collection(
            "never_seen",
            "docs__1-1__voyage-context-3__v1",
        )


def test_supersede_already_superseded_raises(catalog):
    """Superseding a name that already has superseded_by set is rejected;
    silently overwriting would orphan the prior CollectionSuperseded
    event in the log.
    """
    catalog.register_collection("docs__nexus-571b8edd")
    catalog.register_collection(
        "docs__1-1__voyage-context-3__v1",
        content_type="docs", owner_id="1-1",
        embedding_model="voyage-context-3", model_version="v1",
    )
    catalog.register_collection(
        "docs__1-1__voyage-context-3__v2",
        content_type="docs", owner_id="1-1",
        embedding_model="voyage-context-3", model_version="v2",
    )
    catalog.supersede_collection(
        "docs__nexus-571b8edd", "docs__1-1__voyage-context-3__v1",
    )
    with pytest.raises(ValueError, match="already superseded"):
        catalog.supersede_collection(
            "docs__nexus-571b8edd", "docs__1-1__voyage-context-3__v2",
        )


def test_supersede_unregistered_new_raises(catalog):
    """Refuse to point superseded_by at a non-existent collection;
    that produces a dangling pointer no foreign-key-style join can
    resolve.
    """
    catalog.register_collection("docs__nexus-571b8edd")
    with pytest.raises(ValueError, match="new .* is not.*registered"):
        catalog.supersede_collection(
            "docs__nexus-571b8edd", "docs__never-registered",
        )


def test_register_collection_short_circuits_on_identical_re_call(catalog):
    """Re-calling register_collection with identical canonical fields
    must NOT append a duplicate event (log-bloat smell).
    """
    catalog.register_collection(
        "code__1-1__voyage-code-3__v1",
        content_type="code", owner_id="1-1",
        embedding_model="voyage-code-3", model_version="v1",
    )
    events_after_first = [
        e for e in EventLog(catalog._dir).replay()
        if e.type == TYPE_COLLECTION_CREATED
    ]
    catalog.register_collection(
        "code__1-1__voyage-code-3__v1",
        content_type="code", owner_id="1-1",
        embedding_model="voyage-code-3", model_version="v1",
    )
    events_after_second = [
        e for e in EventLog(catalog._dir).replay()
        if e.type == TYPE_COLLECTION_CREATED
    ]
    assert len(events_after_first) == 1
    assert len(events_after_second) == 1


def test_register_collection_re_emits_on_field_change(catalog):
    """If a canonical field changes between calls, the new event is
    emitted so the projection picks up the new value.
    """
    catalog.register_collection("code__nexus-571b8edd")  # legacy form, empty fields
    catalog.register_collection(
        "code__nexus-571b8edd",
        embedding_model="voyage-code-3",  # operator filling in metadata
    )
    events = [
        e for e in EventLog(catalog._dir).replay()
        if e.type == TYPE_COLLECTION_CREATED
    ]
    assert len(events) == 2  # both calls emitted


def test_parse_conformant_collection_name_raises_on_legacy(catalog):
    """parse_conformant_collection_name must raise on non-conformant names.

    Pass-#2 review (2026-05-03) found this raise path had no direct
    test coverage. The regex gate makes false-non-conformant impossible
    in production, but the docstring documents the contract.
    """
    with pytest.raises(ValueError, match="not conformant"):
        parse_conformant_collection_name("docs__nexus-571b8edd")
    with pytest.raises(ValueError, match="not conformant"):
        parse_conformant_collection_name("knowledge__delos")
    with pytest.raises(ValueError, match="not conformant"):
        parse_conformant_collection_name("totally__malformed__weird")


def test_v0_collection_superseded_blank_id_guard(catalog):
    """Direct projector test: a malformed CollectionSuperseded event
    with empty old_coll_id or new_coll_id is treated as a no-op,
    not crashed.

    Pass-#2 review found the guard was untested; if it were silently
    removed the doctor's replay-equality check would still pass
    against well-formed events while crashing on a single replay of a
    malformed line.
    """
    catalog.register_collection("docs__nexus-571b8edd")

    # Both fields missing
    event_blank_old = make_event(
        CollectionSupersededPayload(old_coll_id="", new_coll_id="x"), v=0,
    )
    catalog._projector.apply(event_blank_old)
    catalog._db.commit()
    # Row unchanged
    assert catalog.get_collection("docs__nexus-571b8edd")["superseded_by"] == ""

    event_blank_new = make_event(
        CollectionSupersededPayload(
            old_coll_id="docs__nexus-571b8edd", new_coll_id="",
        ),
        v=0,
    )
    catalog._projector.apply(event_blank_new)
    catalog._db.commit()
    assert catalog.get_collection("docs__nexus-571b8edd")["superseded_by"] == ""


def test_legacy_grandfathered_frozen_on_event_survives_regex_change(catalog, monkeypatch):
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

    catalog.register_collection("docs__1-1__voyage-context-3__v1")
    row_before = catalog.get_collection("docs__1-1__voyage-context-3__v1")
    assert row_before is not None
    assert row_before["legacy_grandfathered"] == 0, (
        "precondition: a conformant name must register as not-legacy"
    )

    # Reset the projection so we can replay from the event log into a
    # fresh state and observe what the projector materialises.
    catalog._db.execute("DELETE FROM collections")  # epsilon-allow: test resets projection state to observe a fresh replay through the projector
    catalog._db.commit()

    # Mutate the regex to a degenerate "everything is non-conformant"
    # form. Pre-fix this would flip the projected row on replay; post-
    # fix the payload's frozen value wins.
    import nexus.corpus
    monkeypatch.setattr(
        nexus.corpus, "is_conformant_collection_name", lambda _name: False,
    )

    # Replay the event log through a fresh projector instance bound
    # to the same DB.
    projector = Projector(catalog._db)
    for event in EventLog(catalog._dir).replay():
        projector.apply(event)
    catalog._db.commit()

    row_after = catalog.get_collection("docs__1-1__voyage-context-3__v1")
    assert row_after is not None, "row must materialise from the replay"
    assert row_after["legacy_grandfathered"] == 0, (
        f"replay must read legacy_grandfathered from the payload, not "
        f"the live regex; got {row_after['legacy_grandfathered']!r}. "
        f"Pre-7m8n the regex monkeypatch would have flipped this to 1."
    )


def test_legacy_grandfathered_falls_back_to_regex_for_pre_7m8n_events(catalog):
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

    catalog._projector.apply(event)
    catalog._db.commit()

    row = catalog.get_collection("docs__legacy-style-name")
    assert row is not None
    # The fallback evaluates the live regex; "docs__legacy-style-name"
    # is not conformant under the current regex, so the projector
    # marks it legacy.
    assert row["legacy_grandfathered"] == 1, (
        "pre-7m8n event (legacy_grandfathered=None) must fall back to "
        "the live regex; a non-conformant name should project as legacy."
    )


def test_v0_collection_superseded_replay_is_deterministic(catalog):
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
    """
    catalog.register_collection("docs__nexus-571b8edd")
    catalog.register_collection("docs__1-1__voyage-context-3__v1")

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

    catalog._projector.apply(blank_ts_event)
    catalog._db.commit()
    first_ts = catalog.get_collection("docs__nexus-571b8edd")["superseded_at"]

    # Reset the row's superseded_at column without re-emitting an event,
    # then replay the same event. Deterministic projector means same ts.
    catalog._db.execute(  # epsilon-allow: test resets a single column to observe deterministic projector replay
        "UPDATE collections SET superseded_at = '' WHERE name = ?",
        ("docs__nexus-571b8edd",),
    )
    catalog._db.commit()

    catalog._projector.apply(blank_ts_event)
    catalog._db.commit()
    second_ts = catalog.get_collection("docs__nexus-571b8edd")["superseded_at"]

    assert first_ts == second_ts, (
        f"replay must be deterministic; got first={first_ts!r}, "
        f"second={second_ts!r}. The pre-fix fallback was "
        f"datetime.now(UTC).isoformat() which changed per call."
    )


def test_update_document_collection_returns_false_on_unknown_tumbler(catalog):
    """update_document_collection must return False (no-op) when the
    document is not registered. Documented contract; pass-#2 review
    found no direct test.
    """
    assert catalog.update_document_collection(
        "1.99.99", "knowledge__1-1__voyage-context-3__v1",
    ) is False


def test_update_document_collection_idempotent_on_same_target(catalog):
    """Re-pointing a doc to its current physical_collection is a no-op
    (returns False; no event written).
    """
    catalog._db.execute(  # epsilon-allow: fixture seeds a documents row with caller-pinned tumbler; Catalog.register mints its own owner-prefixed tumbler
        "INSERT INTO documents "
        "(tumbler, title, author, year, content_type, file_path, "
        "corpus, physical_collection, chunk_count, head_hash, indexed_at, "
        "metadata, source_mtime, alias_of, source_uri) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "1.5.1", "doc-1.5.1", "", 0, "text", "/tmp/x.md",
            "", "knowledge__delos", 1, "", "", "{}", 0.0, "", "",
        ),
    )
    catalog._db.commit()
    assert catalog.update_document_collection(
        "1.5.1", "knowledge__delos",  # already at this collection
    ) is False


def test_idempotent_supersede_skipped_due_to_already_superseded(catalog):
    """A second supersede on the same name must NOT silently extend
    the chain; the test_supersede_already_superseded_raises test covers
    the raise. This case also confirms no extra event lands in the
    log if the call raised.
    """
    catalog.register_collection("docs__nexus-571b8edd")
    catalog.register_collection(
        "docs__1-1__voyage-context-3__v1",
        content_type="docs", owner_id="1-1",
        embedding_model="voyage-context-3", model_version="v1",
    )
    catalog.register_collection(
        "docs__1-1__voyage-context-3__v2",
        content_type="docs", owner_id="1-1",
        embedding_model="voyage-context-3", model_version="v2",
    )
    catalog.supersede_collection(
        "docs__nexus-571b8edd", "docs__1-1__voyage-context-3__v1",
    )
    with pytest.raises(ValueError):
        catalog.supersede_collection(
            "docs__nexus-571b8edd", "docs__1-1__voyage-context-3__v2",
        )
    events = [
        e for e in EventLog(catalog._dir).replay()
        if e.type == TYPE_COLLECTION_SUPERSEDED
    ]
    assert len(events) == 1  # second call raised, did not write


# ── Projector replay ─────────────────────────────────────────────────────


def test_register_collection_replay_produces_same_row(catalog, tmp_path):
    """Replaying the events.jsonl into a fresh Catalog produces the
    same projected row.

    Tests the projector's CollectionCreated handler in isolation, not
    the convenience writer.
    """
    catalog.register_collection(
        "code__1-1__voyage-code-3__v1",
        content_type="code",
        owner_id="1-1",
        embedding_model="voyage-code-3",
        model_version="v1",
    )
    expected = catalog.get_collection("code__1-1__voyage-code-3__v1")

    # Fresh catalog over the same dir but a new sqlite path,
    # then replay events into it. The original catalog stays open;
    # SQLite handles concurrent connections to separate paths.
    fresh_db = tmp_path / "fresh.sqlite"
    fresh = Catalog(catalog_dir=catalog._dir, db_path=fresh_db)
    for event in EventLog(catalog._dir).replay():
        fresh._projector.apply(event)
    fresh._db.commit()

    actual = fresh.get_collection("code__1-1__voyage-code-3__v1")
    assert actual is not None
    assert actual["content_type"] == expected["content_type"]
    assert actual["owner_id"] == expected["owner_id"]
    assert actual["embedding_model"] == expected["embedding_model"]
    assert actual["model_version"] == expected["model_version"]
    assert actual["legacy_grandfathered"] == expected["legacy_grandfathered"]
