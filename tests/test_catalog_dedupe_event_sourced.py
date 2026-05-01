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
    """Regression guard: NEXUS_EVENT_SOURCED unset/0 still drives the
    legacy direct-DELETE path. The function returns the same counts
    and the orphan rows are gone from SQLite immediately.
    """
    monkeypatch.delenv("NEXUS_EVENT_SOURCED", raising=False)
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
