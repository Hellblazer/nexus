# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-101 Phase 3 follow-up (nexus-o6aa.9.4): dedupe.apply_remove_plan
must travel through the projector under ``NEXUS_EVENT_SOURCED=1``.

Pre-fix, ``apply_remove_plan`` direct-DELETEs SQLite and appends
tombstones only to legacy JSONL — which the event-sourced rebuild
ignores. After dedupe, the next ``_ensure_consistent`` rebuild replays
the events.jsonl log (which still carries the original
``OwnerRegistered`` / ``DocumentRegistered`` / ``LinkCreated`` events)
and silently RESURRECTS the orphan rows the operator just deleted.

WITH TEETH: this test exercises the rebuild step explicitly. Pre-fix
the test FAILS — the orphan owner / docs / links reappear after the
rebuild. Post-fix the test PASSES because dedupe emits
``LinkDeleted`` / ``DocumentDeleted`` / ``OwnerDeleted`` events into
events.jsonl, and the projector applies them on rebuild.

Blocks ζ (flip ``NEXUS_EVENT_SOURCED=1`` default) — without this fix,
``nx catalog dedupe`` becomes silently lossy under ζ.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus.catalog.catalog import Catalog
from nexus.catalog.dedupe import OrphanPlan, apply_remove_plan


def _make_catalog_es(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Catalog:
    """Construct a Catalog with NEXUS_EVENT_SOURCED=1 in the env at
    construction time. The gate is read once in ``__init__``.
    """
    monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
    d = tmp_path / "catalog"
    d.mkdir()
    return Catalog(d, d / ".catalog.db")


def _populate_with_orphan(cat: Catalog) -> tuple[str, list[str]]:
    """Register a canonical owner + docs and an orphan owner + docs +
    cross-links. Returns ``(orphan_prefix, orphan_doc_tumblers)``.
    """
    canonical = cat.register_owner(
        "nexus", "repo", repo_hash="571b8edd",
        repo_root="/tmp/nexus",
    )
    canonical_doc = cat.register(
        canonical, "main.py", content_type="code", file_path="main.py",
    )

    orphan = cat.register_owner("nexus-571b8edd", "curator")
    orphan_a = cat.register(
        orphan, "stowaway-a.py", content_type="code",
        file_path="stowaway-a.py",
    )
    orphan_b = cat.register(
        orphan, "stowaway-b.py", content_type="code",
        file_path="stowaway-b.py",
    )

    # Cross-link the orphan documents to themselves and to the canonical
    # owner so the link-delete path is exercised on both endpoints.
    cat.link(orphan_a, orphan_b, "cites", "test", from_span="10-20")
    cat.link(orphan_a, canonical_doc, "relates", "test", from_span="5-15")

    return str(orphan), [str(orphan_a), str(orphan_b)]


def _force_rebuild_from_events(cat: Catalog) -> None:
    """Force ``_ensure_consistent`` to run an event-sourced rebuild from
    events.jsonl. Resets the mtime watermark so the next call replays
    the canonical log.
    """
    cat._last_consistency_mtime = 0.0
    cat.degraded = False
    cat._ensure_consistent()


def test_dedupe_under_es_survives_rebuild(tmp_path, monkeypatch):
    """Core invariant: after dedupe under ES mode, the next event-sourced
    rebuild MUST NOT resurrect the deleted orphan rows. Pre-fix this
    test fails because dedupe emits no events; the rebuild replays the
    OwnerRegistered/DocumentRegistered/LinkCreated events from before
    the dedupe and re-creates everything.
    """
    cat = _make_catalog_es(tmp_path, monkeypatch)
    assert cat._event_sourced_enabled, "test requires NEXUS_EVENT_SOURCED=1"

    orphan_prefix, orphan_docs = _populate_with_orphan(cat)

    # Pre-dedupe sanity: orphan rows are present.
    pre = cat._db.execute(
        "SELECT COUNT(*) FROM documents WHERE tumbler LIKE ?",
        (f"{orphan_prefix}.%",),
    ).fetchone()[0]
    assert pre == 2, f"expected 2 orphan docs pre-dedupe; got {pre}"

    plan = OrphanPlan(
        orphan_prefix=orphan_prefix,
        orphan_name="nexus-571b8edd",
        action="remove",
        doc_count=2,
    )
    deleted_docs, deleted_links = apply_remove_plan(cat, plan)
    assert deleted_docs == 2
    assert deleted_links == 2

    # Immediate post-dedupe: SQLite reflects the deletion (irrespective
    # of how dedupe achieved it).
    immediate = cat._db.execute(
        "SELECT COUNT(*) FROM documents WHERE tumbler LIKE ?",
        (f"{orphan_prefix}.%",),
    ).fetchone()[0]
    assert immediate == 0, "dedupe failed to remove orphan docs immediately"

    # The teeth: force an event-sourced rebuild and assert the deletion
    # SURVIVES. Pre-fix the orphans come back because no LinkDeleted /
    # DocumentDeleted / OwnerDeleted events live in events.jsonl, and
    # the projector replays the original Created events into a fresh
    # SQLite state.
    _force_rebuild_from_events(cat)

    post = cat._db.execute(
        "SELECT COUNT(*) FROM documents WHERE tumbler LIKE ?",
        (f"{orphan_prefix}.%",),
    ).fetchone()[0]
    assert post == 0, (
        "RDR-101 Phase 3 ζ blocker: event-sourced rebuild RESURRECTED "
        f"{post} orphan documents that dedupe deleted. dedupe must "
        "emit LinkDeleted/DocumentDeleted/OwnerDeleted events under "
        "NEXUS_EVENT_SOURCED=1."
    )

    owner_post = cat._db.execute(
        "SELECT COUNT(*) FROM owners WHERE tumbler_prefix = ?",
        (orphan_prefix,),
    ).fetchone()[0]
    assert owner_post == 0, (
        "event-sourced rebuild RESURRECTED the orphan owner row; "
        "dedupe must emit OwnerDeleted under NEXUS_EVENT_SOURCED=1."
    )

    link_post = cat._db.execute(
        "SELECT COUNT(*) FROM links WHERE from_tumbler IN (?, ?) "
        "OR to_tumbler IN (?, ?)",
        (*orphan_docs, *orphan_docs),
    ).fetchone()[0]
    assert link_post == 0, (
        f"event-sourced rebuild RESURRECTED {link_post} orphan links; "
        "dedupe must emit one LinkDeleted per removed link under "
        "NEXUS_EVENT_SOURCED=1."
    )


def test_dedupe_emits_events_under_es(tmp_path, monkeypatch):
    """Direct check: dedupe under ES appends the right events to
    events.jsonl. One ``LinkDeleted`` per link, one
    ``DocumentDeleted`` per orphan doc, one ``OwnerDeleted`` for the
    orphan owner.
    """
    import json

    cat = _make_catalog_es(tmp_path, monkeypatch)
    orphan_prefix, _ = _populate_with_orphan(cat)

    pre_events = cat._events_path.read_text().splitlines()
    pre_count = len(pre_events)

    plan = OrphanPlan(
        orphan_prefix=orphan_prefix,
        orphan_name="nexus-571b8edd",
        action="remove",
        doc_count=2,
    )
    apply_remove_plan(cat, plan)

    post_events = cat._events_path.read_text().splitlines()
    new_events = [json.loads(line) for line in post_events[pre_count:]]
    new_types = [e["type"] for e in new_events]

    # 2 LinkDeleted (the two links from the orphan) + 2
    # DocumentDeleted + 1 OwnerDeleted = 5 events. Order: links first
    # (so dependent rows go before their owners), then documents, then
    # the owner row last.
    assert new_types.count("LinkDeleted") == 2, (
        f"expected 2 LinkDeleted events; got {new_types}"
    )
    assert new_types.count("DocumentDeleted") == 2, (
        f"expected 2 DocumentDeleted events; got {new_types}"
    )
    assert new_types.count("OwnerDeleted") == 1, (
        f"expected 1 OwnerDeleted event; got {new_types}"
    )


def test_dedupe_legacy_mode_unchanged(tmp_path, monkeypatch):
    """Regression guard: NEXUS_EVENT_SOURCED=0 still drives the legacy
    direct-DELETE path. The function returns the same counts and the
    orphan rows are gone from SQLite immediately.
    """
    # PR ζ flipped the default to ON; opt back into legacy explicitly.
    monkeypatch.setenv("NEXUS_EVENT_SOURCED", "0")
    d = tmp_path / "catalog"
    d.mkdir()
    cat = Catalog(d, d / ".catalog.db")
    assert not cat._event_sourced_enabled, "legacy mode required"

    canonical = cat.register_owner(
        "nexus", "repo", repo_hash="571b8edd",
        repo_root="/tmp/nexus",
    )
    cat.register(canonical, "main.py", content_type="code", file_path="main.py")
    orphan = cat.register_owner("nexus-571b8edd", "curator")
    cat.register(
        orphan, "stowaway.py", content_type="code",
        file_path="stowaway.py",
    )

    plan = OrphanPlan(
        orphan_prefix=str(orphan),
        orphan_name="nexus-571b8edd",
        action="remove",
        doc_count=1,
    )
    docs, _links = apply_remove_plan(cat, plan)

    assert docs == 1
    remaining = cat._db.execute(
        "SELECT COUNT(*) FROM documents WHERE tumbler LIKE ?",
        (f"{orphan}.%",),
    ).fetchone()[0]
    assert remaining == 0


# ─────────────────────────────────────────────────────────────────────
# RDR-101 Phase 3 follow-up A (nexus-o6aa.9.6): atomicity tests.
#
# The pre-A implementation interleaved
# ``_write_to_event_log + _projector.apply`` per event with a single
# trailing commit. A mid-loop projector exception left events 1..N-1
# durable in events.jsonl while the pending SQLite mutations rolled
# back at the un-committed connection close — a split state with no
# operator-visible diagnostic.
#
# Post-A: events are written first (durable batch), then projected
# inside ``cat._db.transaction()``. A projector exception rolls the
# whole projection back to the pre-dedupe state while events.jsonl
# still carries the full batch; the next ``_ensure_consistent`` mtime
# tick replays the durable batch and converges on the post-dedupe
# state.
# ─────────────────────────────────────────────────────────────────────


def test_dedupe_acquires_flock(tmp_path, monkeypatch):
    """``apply_remove_plan`` must acquire the catalog directory flock
    at entry. Pre-fix every other ES mutator did but dedupe didn't, so
    a concurrent ``register`` call could interleave appends in
    events.jsonl and the legacy JSONL files.
    """
    cat = _make_catalog_es(tmp_path, monkeypatch)
    orphan_prefix, _ = _populate_with_orphan(cat)

    acquired: list[int] = []
    released: list[int] = []
    original_acquire = cat._acquire_lock
    original_release = cat._release_lock

    def tracked_acquire():
        fd = original_acquire()
        acquired.append(fd)
        return fd

    def tracked_release(fd):
        released.append(fd)
        return original_release(fd)

    monkeypatch.setattr(cat, "_acquire_lock", tracked_acquire)
    monkeypatch.setattr(cat, "_release_lock", tracked_release)

    plan = OrphanPlan(
        orphan_prefix=orphan_prefix, orphan_name="nexus-571b8edd",
        action="remove", doc_count=2,
    )
    apply_remove_plan(cat, plan)

    assert len(acquired) == 1, (
        f"apply_remove_plan must acquire flock exactly once; got {acquired}"
    )
    assert acquired == released, (
        f"every acquire must be paired with a release; got "
        f"acquired={acquired} released={released}"
    )


def test_projector_failure_rolls_back_sqlite_but_log_durable(
    tmp_path, monkeypatch,
):
    """Atomicity invariant: if ``_projector.apply`` raises mid-batch,
    SQLite reverts to the pre-dedupe state (transaction rollback)
    while events.jsonl carries the full event batch (durable). The
    next ``_ensure_consistent`` rebuild replays the batch and
    converges on the post-dedupe state.

    Pre-fix the per-event interleave produced events 1..N-1 durable
    plus uncommitted SQLite mutations 1..N-1 that rolled back at
    connection close — split state with no operator diagnostic.
    """
    cat = _make_catalog_es(tmp_path, monkeypatch)
    orphan_prefix, orphan_docs = _populate_with_orphan(cat)

    pre_doc_count = cat._db.execute(
        "SELECT COUNT(*) FROM documents WHERE tumbler LIKE ?",
        (f"{orphan_prefix}.%",),
    ).fetchone()[0]
    pre_event_size = (
        cat._events_path.read_text().count("\n")
        if cat._events_path.exists() else 0
    )

    # Poison the projector to raise on the 3rd apply call.
    original_apply = cat._projector.apply
    call_count = [0]

    def poisoned_apply(event):
        call_count[0] += 1
        if call_count[0] == 3:
            raise RuntimeError("simulated projector failure on event 3")
        return original_apply(event)

    monkeypatch.setattr(cat._projector, "apply", poisoned_apply)

    plan = OrphanPlan(
        orphan_prefix=orphan_prefix, orphan_name="nexus-571b8edd",
        action="remove", doc_count=2,
    )
    with pytest.raises(RuntimeError, match="simulated projector failure"):
        apply_remove_plan(cat, plan)

    # SQLite invariant: rolled back to pre-dedupe state.
    post_doc_count = cat._db.execute(
        "SELECT COUNT(*) FROM documents WHERE tumbler LIKE ?",
        (f"{orphan_prefix}.%",),
    ).fetchone()[0]
    assert post_doc_count == pre_doc_count, (
        "SQLite must roll back on projector failure; "
        f"pre={pre_doc_count} post={post_doc_count}"
    )

    # events.jsonl invariant: full batch durably written before
    # projection started.
    post_event_lines = cat._events_path.read_text().splitlines()
    new_event_count = len(post_event_lines) - pre_event_size
    # 2 LinkDeleted + 2 DocumentDeleted + 1 OwnerDeleted == 5 new events.
    assert new_event_count == 5, (
        f"events.jsonl must carry the full batch even when projection "
        f"failed; got {new_event_count} new events"
    )

    # Convergence invariant: after restoring the projector, force a
    # rebuild and assert orphans stay deleted (the durable events
    # replay correctly).
    monkeypatch.setattr(cat._projector, "apply", original_apply)
    _force_rebuild_from_events(cat)
    final_count = cat._db.execute(
        "SELECT COUNT(*) FROM documents WHERE tumbler LIKE ?",
        (f"{orphan_prefix}.%",),
    ).fetchone()[0]
    assert final_count == 0, (
        f"rebuild from durable events must converge on post-dedupe "
        f"state; got {final_count} surviving orphan docs"
    )
