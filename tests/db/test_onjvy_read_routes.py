# SPDX-License-Identifier: AGPL-3.0-or-later
"""The three formerly write-only surfaces round-trip through the client (nexus-onjvy).

REPLACES tests/db/test_onjvy_write_only_surfaces.py, which CHARACTERIZED the gap
— wrote a value, swept every read route, and asserted it was unreachable. Those
tests existed because a "the value round-trips" test was not available: no route
returned these values, so such a test could only fail. Their own failure messages
said to delete them and restore the real assertions when a read route landed.

engine-service-v0.1.58 landed the routes; this is that restoration. What each
test asserts is taken from the bead's record of what the twelve deleted
SQLite-era tests proved, not re-invented:

  * the prefer-higher UPSERT keeps the HIGHER similarity and refreshes
    source_collection / assigned_at only when it wins
  * a hook failure lands with (doc_id, collection, hook_name, error), batch
    variant included
  * warn_stale reports staleness instead of silently returning False

The RDR-077 C-2 rule (MAX across ALL contributing collections, not a single
row) is NOT re-proved here — it is pinned engine-side in TaxonomyRepositoryTest,
where the discover timestamps are settable. See the scope note above the gap-3
tests for why the client cannot own it.

Requires an engine at >= v0.1.58 (REQUIRED_ENGINE_VERSION), which the suite's
substrate provides.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from nexus.db.t2 import T2Database
from tests._t2_fixture_ops import canonical_chunk_id
from tests.test_taxonomy import _seed_chunk


@pytest.fixture()
def db(tmp_path: Path) -> Any:
    database = T2Database(tmp_path / "memory.db")
    yield database
    database.close()


def _unique_topic_id() -> int:
    """A topic id disjoint from every other test's.

    ``topics_pk`` is PRIMARY KEY (id) and GLOBAL across tenants while the whole
    session shares one engine, so literal ids collide with the BIGSERIAL ids
    other tests have claimed.
    """
    return 1_000_000 + (time.time_ns() // 1_000) % 10**12


def _seed_topic(db: T2Database, topic_id: int, collection: str) -> None:
    db.taxonomy.import_topic(
        src_id=topic_id, label=f"onjvy-{topic_id}", parent_id=None,
        collection=collection, centroid_hash=None, doc_count=0,
        created_at="2026-04-14T00:00:00Z", review_status="pending", terms=None,
    )


# ── gap 1: assignment quality columns are READABLE ──────────────────────────


def test_assignment_quality_columns_round_trip(db: T2Database) -> None:
    """similarity / source_collection / assigned_at survive write -> read."""
    topic_id = _unique_topic_id()
    _seed_topic(db, topic_id, "code__src_a")
    doc_id = canonical_chunk_id(f"onjvy-detail-{topic_id}")
    _seed_chunk(topic_id, "code__onjvy_src", doc_id)

    db.taxonomy.assign_topic(
        doc_id, topic_id, assigned_by="projection",
        similarity=0.8712345, source_collection="code__onjvy_src",
        assigned_at="2026-04-14T10:00:00Z",
    )

    rows = db.taxonomy.get_assignment_details([doc_id])

    assert len(rows) == 1
    row = rows[0]
    assert row["doc_id"] == doc_id
    assert row["assigned_by"] == "projection"
    assert row["similarity"] == pytest.approx(0.8712345)
    assert row["source_collection"] == "code__onjvy_src"
    assert row["assigned_at"] == "2026-04-14T10:00:00Z", (
        "timestamps must cross as UTC ISO-8601 with explicit seconds — "
        "OffsetDateTime.toString() renders in the server's LOCAL offset and "
        "elides zero seconds, which breaks string comparison"
    )


def test_prefer_higher_upsert_keeps_the_higher_similarity(db: T2Database) -> None:
    """SC-2 prefer-higher: the LOWER later write must not clobber.

    One of the six deleted quality-column tests, restored. It could only be
    written once the columns became readable — the assertion IS the read.
    """
    topic_id = _unique_topic_id()
    _seed_topic(db, topic_id, "code__src_a")
    doc_id = canonical_chunk_id(f"onjvy-upsert-{topic_id}")
    _seed_chunk(topic_id, "code__src_a", doc_id)
    _seed_chunk(topic_id, "code__src_b", doc_id)

    db.taxonomy.assign_topic(
        doc_id, topic_id, assigned_by="projection", similarity=0.9,
        source_collection="code__src_a", assigned_at="2026-04-14T10:00:00Z",
    )
    db.taxonomy.assign_topic(
        doc_id, topic_id, assigned_by="projection", similarity=0.7,
        source_collection="code__src_b", assigned_at="2026-04-14T11:00:00Z",
    )

    row = db.taxonomy.get_assignment_details([doc_id])[0]

    assert row["similarity"] == pytest.approx(0.9), "the higher similarity must win"
    assert row["source_collection"] == "code__src_a", (
        "source/at are refreshed ONLY when the new similarity wins — the "
        "losing write must not drag its provenance in"
    )
    assert row["assigned_at"] == "2026-04-14T10:00:00Z"


def test_assignment_details_is_empty_for_unknown_docs(db: T2Database) -> None:
    assert db.taxonomy.get_assignment_details([canonical_chunk_id("no-such-doc")]) == []
    assert db.taxonomy.get_assignment_details([]) == []


# ── gap 2: hook_failures are READABLE ───────────────────────────────────────


def test_hook_failure_round_trips_with_its_fields(db: T2Database) -> None:
    """A recorded failure comes back with the fields an operator needs."""
    db.telemetry.record_hook_failure(
        doc_id="onjvy-doc", collection="knowledge__onjvy",
        hook_name="onjvy_probe_hook", error="simulated failure",
        chain="single",
    )

    resp = db.telemetry.list_hook_failures(days=1, limit=50)

    assert resp["total"] >= 1
    mine = [r for r in resp["rows"] if r["doc_id"] == "onjvy-doc"]
    assert len(mine) == 1, "the recorded failure must be readable"
    row = mine[0]
    assert row["collection"] == "knowledge__onjvy"
    assert row["hook_name"] == "onjvy_probe_hook"
    assert row["error"] == "simulated failure"
    assert row["is_batch"] is False
    assert row["occurred_at"].endswith("Z")


def test_hook_failure_batch_variant_carries_its_doc_ids(db: T2Database) -> None:
    """The batch chain's payload survives — `nx taxonomy status` counts docs
    affected from it, so losing it silently under-reports the blast radius."""
    # nexus-cefa1.3: hook_failures.batch_doc_ids is jsonb now — must be valid
    # JSON (this was always the real production shape: hook_registry.py writes
    # json.dumps(doc_ids)). A raw comma-joined string is no longer valid input.
    db.telemetry.record_hook_failure(
        doc_id="onjvy-batch-doc", collection="knowledge__onjvy",
        hook_name="onjvy_batch_hook", error="batch boom", chain="batch",
        batch_doc_ids='["d1", "d2", "d3"]', is_batch=True,
    )

    rows = db.telemetry.list_hook_failures(days=1, limit=50)["rows"]

    row = next(r for r in rows if r["doc_id"] == "onjvy-batch-doc")
    assert row["is_batch"] is True
    # PG's jsonb canonical text output matches json.dumps' default separators
    # (space after each comma), so the real writer round-trips byte-identical.
    assert row["batch_doc_ids"] == '["d1", "d2", "d3"]'
    assert row["chain"] == "batch"


def test_hook_failure_filters_and_exact_total(db: T2Database) -> None:
    """hook_name filter works, and `total` counts the WHOLE filtered set.

    The exact-total property is what `nx doctor` reports; deriving it from a
    limited page would under-report the moment failures exceed the page.
    """
    for i in range(4):
        db.telemetry.record_hook_failure(
            doc_id=f"onjvy-filter-{i}", collection="knowledge__onjvy",
            hook_name="onjvy_filtered", error="boom", chain="single",
        )

    filtered = db.telemetry.list_hook_failures(
        days=1, hook_names=["onjvy_filtered"], limit=100,
    )
    assert filtered["total"] == 4
    assert all(r["hook_name"] == "onjvy_filtered" for r in filtered["rows"])

    capped = db.telemetry.list_hook_failures(
        days=1, hook_names=["onjvy_filtered"], limit=1,
    )
    assert len(capped["rows"]) == 1, "the page honours limit"
    assert capped["total"] == 4, "the total ignores it"


def test_hook_failure_window_excludes_older_rows(db: T2Database) -> None:
    old = (datetime.now(UTC) - timedelta(days=90)).isoformat()
    db.telemetry.record_hook_failure(
        doc_id="onjvy-aged", collection="knowledge__onjvy",
        hook_name="onjvy_aged_hook", error="boom", chain="single",
        occurred_at=old,
    )

    recent = db.telemetry.list_hook_failures(
        days=1, hook_names=["onjvy_aged_hook"], limit=50,
    )
    unbounded = db.telemetry.list_hook_failures(
        days=0, hook_names=["onjvy_aged_hook"], limit=50,
    )

    assert recent["total"] == 0, "a 90-day-old row is outside the 24h window"
    assert unbounded["total"] == 1, "days=0 means no time bound"


# ── gap 3: warn_stale actually warns ────────────────────────────────────────
#
# SCOPE NOTE. The RDR-077 C-2 semantics — MAX(last_discover_at) across ALL
# contributing source collections rather than a single row — is pinned
# ENGINE-side in TaxonomyRepositoryTest, where recordDiscoverCount takes an
# explicit discoveredAt and the two collections can be given genuinely different
# timestamps. The CLIENT cannot: record_discover_count(collection, doc_count)
# stamps now() on both twins by contract. Re-proving MAX here would mean
# widening a store API for a test, or sleeping between writes and asserting on
# sub-second ordering. Neither is worth it, so these tests own what they can
# actually own: that the aggregates cross the wire and that warn_stale gates
# them.


def _seed_hub(db: T2Database, topic_id: int, sources: tuple[str, ...],
              assigned_at: str) -> None:
    _seed_topic(db, topic_id, "code__src_a")
    for i, src in enumerate(sources):
        doc_id = canonical_chunk_id(f"onjvy-hub-{topic_id}-{i}")
        _seed_chunk(topic_id, src, doc_id)
        db.taxonomy.assign_topic(
            doc_id, topic_id, assigned_by="projection",
            similarity=0.5, source_collection=src, assigned_at=assigned_at,
        )


def _hub(db: T2Database, topic_id: int, *, warn_stale: bool):
    hubs = db.taxonomy.detect_hubs(min_collections=2, warn_stale=warn_stale)
    match = [h for h in hubs if h.topic_id == topic_id]
    assert match, (
        "the seeded 2-collection topic did not surface as a hub, so the "
        "staleness assertions would be vacuous"
    )
    return match[0]


def test_warn_stale_surfaces_staleness_over_http(db: T2Database) -> None:
    """The aggregates reach the client and is_stale is TRUE when it should be.

    Before engine-service-v0.1.58 this returned is_stale=False for every row
    with no warning and no error — a flag the client accepted and the backend
    dropped (nexus-onjvy, same shape as nexus-ybj1b's include_heuristic).

    Assignments are dated in the FUTURE so they postdate the discover, which
    record_discover_count stamps at now(). That is the only lever the client
    has over the comparison, and it is enough to prove the wire.
    """
    topic_id = _unique_topic_id()
    srcs = (f"code__hub_a_{topic_id}", f"code__hub_b_{topic_id}")
    _seed_hub(db, topic_id, srcs, "2099-01-01T00:00:00Z")
    for s in srcs:
        db.taxonomy.record_discover_count(s, 100)

    hub = _hub(db, topic_id, warn_stale=True)

    assert hub.is_stale is True, (
        "assignments postdate every discover — the hub is stale and the flag "
        "must say so"
    )
    assert hub.max_last_discover_at is not None, (
        "max_last_discover_at must cross the wire, not stay hardcoded None"
    )
    assert hub.max_last_discover_at.endswith("Z"), (
        "timestamps cross as UTC ISO-8601, not a locale-dependent offset"
    )
    assert hub.never_discovered_count == 0


def test_warn_stale_not_stale_when_discover_postdates(db: T2Database) -> None:
    """The negative case: old assignments against a fresh discover are NOT stale.

    Without this, the test above would pass against an implementation that
    hardcoded is_stale=True — the mirror of the bug being fixed.
    """
    topic_id = _unique_topic_id()
    srcs = (f"code__hub_a_{topic_id}", f"code__hub_b_{topic_id}")
    _seed_hub(db, topic_id, srcs, "2020-01-01T00:00:00Z")
    for s in srcs:
        db.taxonomy.record_discover_count(s, 100)

    hub = _hub(db, topic_id, warn_stale=True)

    assert hub.is_stale is False
    assert hub.never_discovered_count == 0


def test_never_discovered_contributor_forces_stale(db: T2Database) -> None:
    """A contributing collection with no taxonomy_meta row counts and forces stale."""
    topic_id = _unique_topic_id()
    srcs = (f"code__hub_a_{topic_id}", f"code__hub_b_{topic_id}")
    _seed_hub(db, topic_id, srcs, "2020-01-01T00:00:00Z")
    db.taxonomy.record_discover_count(srcs[0], 100)
    # srcs[1] deliberately never recorded.

    hub = _hub(db, topic_id, warn_stale=True)

    assert hub.never_discovered_count == 1
    assert hub.is_stale is True, (
        "a never-discovered contributor is stale even though the assignments "
        "predate the discover that DID happen"
    )


def test_warn_stale_false_reports_nothing(db: T2Database) -> None:
    """The OFF case stays zeroed, matching the retired oracle.

    The engine computes the aggregates unconditionally, so without the
    client-side gate this would report staleness where the oracle reported
    zeros — a divergence introduced while fixing the ON case.
    """
    topic_id = _unique_topic_id()
    srcs = (f"code__hub_a_{topic_id}", f"code__hub_b_{topic_id}")
    _seed_hub(db, topic_id, srcs, "2099-01-01T00:00:00Z")
    db.taxonomy.record_discover_count(srcs[0], 100)

    hub = _hub(db, topic_id, warn_stale=False)

    assert hub.is_stale is False
    assert hub.max_last_discover_at is None
    assert hub.never_discovered_count == 0


# ── the consumer: `nx taxonomy status` surfaces failures in service mode ────


def test_taxonomy_status_surfaces_hook_failures_in_service_mode(
    db: T2Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`nx taxonomy status` reports recent hook failures on the engine substrate.

    THE GAP THIS CLOSES. taxonomy_cmd's failure block was guarded by
    ``_has_raw_access(db.taxonomy)`` with no else — so on the service backend,
    the DEFAULT substrate since the aqbrk flip, `nx taxonomy status` silently
    reported nothing. The log that exists to surface SILENT hook failures was
    itself silent, and there was no read route to fix it with until
    engine-service-v0.1.58.

    The SQLite half of this coverage
    (test_cli_taxonomy_status_surfaces_recent_hook_failures) was deleted with
    the dies-roster in nexus-i711w Stage 1b, so without this test the verb has
    none at all.
    """
    from unittest.mock import patch

    from click.testing import CliRunner

    from nexus.commands.taxonomy_cmd import taxonomy

    # status early-returns on "No taxonomy data" before it ever reaches the
    # failure block, so a topic has to exist for this test to be about what it
    # claims to be about.
    topic_id = _unique_topic_id()
    _seed_topic(db, topic_id, "docs__status")
    status_doc_id = canonical_chunk_id(f"status-projected-{topic_id}")
    _seed_chunk(topic_id, "docs__status", status_doc_id)
    db.taxonomy.assign_topic(
        status_doc_id, topic_id, assigned_by="projection",
        similarity=0.5, source_collection="docs__status",
        assigned_at="2026-04-14T10:00:00Z",
    )

    db.telemetry.record_hook_failure(
        doc_id="status-doc", collection="knowledge__status",
        hook_name="status_probe_hook", error="boom", chain="single",
    )
    db.telemetry.record_hook_failure(
        doc_id="status-batch", collection="knowledge__status",
        hook_name="status_batch_hook", error="boom", chain="batch",
        batch_doc_ids='["a", "b", "c"]', is_batch=True,
    )

    runner = CliRunner()
    with patch(
        "nexus.commands.taxonomy_cmd._default_db_path",
        return_value=tmp_path / "memory.db",
    ):
        result = runner.invoke(taxonomy, ["status"])

    assert result.exit_code == 0, result.output
    assert "post-store hook failure(s)" in result.output, (
        "service mode reported NOTHING — the raw-access guard has no else "
        "branch again, or list_hook_failures is not reaching the engine:\n"
        + result.output
    )
    assert "status_probe_hook" in result.output
    assert "status_batch_hook" in result.output
    assert "affecting" in result.output, (
        "the batch payload must expand into a docs-affected count — that is "
        "what tells an operator the blast radius exceeds the failure count"
    )
