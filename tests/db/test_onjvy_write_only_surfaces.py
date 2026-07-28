# SPDX-License-Identifier: AGPL-3.0-or-later
"""The engine writes data no route can read (nexus-onjvy) — pinned, not assumed.

WHY THIS FILE EXISTS. nexus-i711w Stage 1b deleted the SQLite substrate, and
with it the 12 tests that covered these gaps: every one of them read the value
back through a raw connection, so none could survive on a substrate that has no
raw handle. Deleting them left nexus-onjvy with ZERO coverage on either arm —
an engine capability gap that nothing would report and nothing would fail on.

WHAT CAN AND CANNOT BE WRITTEN HERE. A "the value round-trips" test is not
available: the whole content of nexus-onjvy is that no route returns these
values, so such a test could only fail. What IS available is a CHARACTERIZATION
of the gap — write through the real service store, sweep every read route the
store exposes, and assert the value is unreachable.

That makes each test fail in BOTH directions, which is the point:

  * break the WRITE path and the write-side assertions fail;
  * land a READ route and the unreachability assertions fail, with a message
    saying so — at which point this file is deleted and the real round-trip
    assertions are restored from the bead's record of the deleted tests.

This is the same treatment the nexus-aqbrk port already applied to the fourth
instance of this class, ``tier_writes.target_title``: pin the broken value with
the bead named in the failure message rather than drop the assertion. See
``_assert_target_title`` in tests/test_memory_put_attribution.py.

These tests run on the engine substrate, which is the only substrate.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from nexus.db.t2 import T2Database

_BEAD = "nexus-onjvy"


@pytest.fixture()
def db(tmp_path: Path) -> Any:
    database = T2Database(tmp_path / "memory.db")
    yield database
    database.close()


def _unique_topic_id() -> int:
    """A topic id disjoint from every other test's.

    ``topics_pk`` is PRIMARY KEY (id) and GLOBAL across tenants, while the whole
    pytest session shares ONE engine — so literal ids collide with the BIGSERIAL
    ids other tests' discover_topics already claimed. Same microsecond-monotonic
    idiom as tests/test_projection_quality.py::_unique_topic_base.
    """
    return 1_000_000 + (time.time_ns() // 1_000) % 10**12


def _seed_topic(db: T2Database, topic_id: int, collection: str) -> None:
    db.taxonomy.import_topic(
        src_id=topic_id, label="onjvy-probe", parent_id=None,
        collection=collection, centroid_hash=None, doc_count=0,
        created_at="2026-04-14T00:00:00Z", review_status="pending", terms=None,
    )


# ── Gap 1: topic_assignments similarity / assigned_at / source_collection ────


def test_assignment_quality_columns_are_written_and_unreadable(db: T2Database) -> None:
    """The three quality columns are accepted by the write path and returned by
    no read route on the store.

    ``TaxonomyRepository.getAssignmentsForDocs`` selects DOC_ID and TOPIC_ID
    only; ``/assignments/docs`` and ``/assignments/by_label`` return bare
    doc_ids. So in service mode an operator cannot ask "how confident is this
    assignment", "when was it made", or "which collection did it come from".
    """
    topic_id = _unique_topic_id()
    _seed_topic(db, topic_id, "code__src_a")

    similarity = 0.8712345
    source_collection = "code__onjvy_source"
    assigned_at = "2026-04-14T10:00:00Z"
    doc_id = f"onjvy-doc-{topic_id}"

    # WRITE — must be accepted. If the engine ever rejects these, that is a
    # real regression and this assertion is the one that catches it.
    db.taxonomy.assign_topic(
        doc_id, topic_id, assigned_by="projection",
        similarity=similarity, source_collection=source_collection,
        assigned_at=assigned_at,
    )

    # The assignment itself IS readable — so the write demonstrably landed and
    # the sweep below is about the quality payload, not about a lost row.
    assert db.taxonomy.get_assignments_for_docs([doc_id]) == {doc_id: topic_id}, (
        "the assignment itself did not round-trip, so this test cannot say "
        "anything about its quality columns — fix the write path first"
    )

    # SWEEP every read route the store exposes for the three values.
    reads: dict[str, Any] = {
        "get_assignments_for_docs": db.taxonomy.get_assignments_for_docs([doc_id]),
        "get_topic_doc_ids":        db.taxonomy.get_topic_doc_ids(topic_id, limit=10),
        "get_all_topic_doc_ids":    db.taxonomy.get_all_topic_doc_ids(topic_id),
        "get_topic_docs":           db.taxonomy.get_topic_docs(topic_id, limit=10),
        "get_doc_ids_for_topic":    db.taxonomy.get_doc_ids_for_topic("onjvy-probe"),
    }

    for route, payload in reads.items():
        rendered = repr(payload)
        for label, value in (
            ("similarity", "0.87"),
            ("source_collection", source_collection),
            ("assigned_at", "2026-04-14T10:00"),
        ):
            assert value not in rendered, (
                f"{route} now returns {label} — the {_BEAD} write-only gap has "
                f"CLOSED for topic_assignments. Delete this test and restore the "
                f"real round-trip assertions: the bead records what the six "
                f"deleted tests proved (prefer-higher UPSERT keeps the higher "
                f"similarity and refreshes source/at only when it wins; the "
                f"HDBSCAN and manual paths keep INSERT-OR-IGNORE; assign_batch "
                f"populates similarity across collections).\n"
                f"  route returned: {rendered[:400]}"
            )


# ── Gap 2: hook_failures ────────────────────────────────────────────────────


def test_hook_failures_are_written_with_no_read_route(db: T2Database) -> None:
    """A hook failure can be recorded and trimmed, never inspected.

    ``TelemetryHandler`` exposes ``/hook_failures/record`` and
    ``/hook_failures/trim`` and no read route. The only readers in ``src/`` are
    raw SQL — ``nx taxonomy status`` (taxonomy_cmd.py) and ``nx doctor`` — both
    of which die with the SQLite stores in nexus-i711w Stage 2. So the failure
    log that exists to surface SILENT hook failures becomes permanently
    uninspectable, on exactly the path where silence is the failure mode.
    """
    old = (datetime.now(UTC) - timedelta(days=90)).isoformat()
    db.telemetry.record_hook_failure(
        doc_id="onjvy-doc", collection="knowledge__onjvy",
        hook_name="onjvy_probe_hook", error="simulated failure",
        chain="single", occurred_at=old,
    )

    # No read method exists. Structural, so ADDING one trips this immediately
    # rather than waiting for someone to notice the gap closed.
    readers = [
        name for name in dir(db.telemetry)
        if "hook_failure" in name
        and not name.startswith("_")
        and name not in {"record_hook_failure", "trim_hook_failures",
                         "import_hook_failure"}
    ]
    assert readers == [], (
        f"{db.telemetry.__class__.__name__} grew {readers}, so the {_BEAD} "
        f"hook_failures gap has CLOSED. Delete this test and restore the real "
        f"assertions: the bead records that the four deleted tests proved a "
        f"failure lands with (doc_id, collection, hook_name, error), including "
        f"the batch-chain variant."
    )

    # The ONLY observable that the write landed at all: trim reports what it
    # deleted. Without this the test above would pass just as happily against a
    # record_hook_failure that silently dropped every row — which is precisely
    # the bug nexus-9613q.3 fixed, so it is worth keeping a probe on.
    deleted = db.telemetry.trim_hook_failures(days=1)
    assert deleted >= 1, (
        "trim reported nothing to delete, so the 90-day-old hook failure "
        "recorded above never reached the engine — the write path is dropping "
        "rows silently, the nexus-9613q.3 class of bug"
    )


# ── Gap 3: detect_hubs(warn_stale=...) ──────────────────────────────────────


def test_detect_hubs_silently_drops_warn_stale(db: T2Database) -> None:
    """``warn_stale=True`` is accepted and ignored — no warning, no error.

    ``http_taxonomy_store.detect_hubs`` hardcodes ``max_last_discover_at=None``,
    ``never_discovered_count=0`` and ``is_stale=False`` regardless of the flag.
    Same shape as nexus-ybj1b (include_heuristic accepted and ignored): a flag
    the client takes and the backend drops is a silent contract break.
    """
    topic_id = _unique_topic_id()
    _seed_topic(db, topic_id, "code__src_a")

    # A hub needs df >= 2: the engine's /hubs counts DISTINCT source_collection
    # over assignments, so two collections on one topic is the minimum shape.
    for i, source in enumerate(("code__onjvy_a", "code__onjvy_b")):
        db.taxonomy.assign_topic(
            f"onjvy-hub-doc-{topic_id}-{i}", topic_id, assigned_by="projection",
            similarity=0.5, source_collection=source,
            assigned_at="2026-04-14T10:00:00Z",
        )

    stale_on = db.taxonomy.detect_hubs(min_collections=2, warn_stale=True)
    stale_off = db.taxonomy.detect_hubs(min_collections=2, warn_stale=False)

    # NON-VACUITY: comparing two empty lists would pass while proving nothing.
    ours = [h for h in stale_on if h.topic_id == topic_id]
    assert ours, (
        "the seeded 2-collection topic did not surface as a hub, so the "
        "warn_stale comparison below would be vacuous — check that "
        "/hubs still aggregates df over distinct source_collection"
    )

    assert stale_on == stale_off, (
        f"detect_hubs now returns something DIFFERENT for warn_stale=True, so "
        f"the {_BEAD} silent-drop has CLOSED. Delete this test and restore the "
        f"real assertions: the bead records that the two deleted tests proved "
        f"staleness compares against MAX(last_discover_at) aggregated over ALL "
        f"contributing source collections, not a single row."
    )
    hub = ours[0]
    assert hub.max_last_discover_at is None and hub.is_stale is False, (
        f"detect_hubs populated staleness fields, so the {_BEAD} gap has "
        f"CLOSED — see the message above.\n  got: {hub!r}"
    )
