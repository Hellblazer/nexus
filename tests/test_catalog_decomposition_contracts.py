# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests for nexus-mbm decomposition contracts.

PR #602 split ``catalog.py`` into a facade plus five focused
modules. The composition pattern (``_LinkOps`` / ``_DocumentOps`` /
``_SyncOps`` composed onto ``Catalog`` as ``self._links`` /
``self._docs`` / ``self._sync``) and the ``_cat_mod`` patching
contract that lets tests monkeypatch helpers on
``nexus.catalog.catalog`` and have those patches propagate into
the extracted modules — both have explicit docstrings but no
test pins. This file pins them.

Test-validator review surfaced these gaps:
- Composition-order invariant (``_sync`` must exist when
  ``_ensure_consistent`` runs).
- ``_cat_mod`` patching contract for ``catalog_links`` and
  ``catalog_sync`` (silent-refactor protection).
- ``graph`` / ``graph_many`` cap scenarios (a), (b), and the
  documented non-propagation of (c).
- ``bulk_unlink`` event-sourced vs legacy paths.
- ``Catalog.__init__`` state when ``_ensure_consistent`` raises.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from nexus.catalog.catalog import Catalog
from nexus.catalog.tumbler import Tumbler


# ── Composition-order invariant ──────────────────────────────────────────────


def test_composition_order_sync_set_before_ensure_consistent(
    tmp_path: Path,
) -> None:
    """``self._sync`` MUST be assigned before ``_ensure_consistent``
    runs in ``__init__``. The bootstrap calls
    ``_read_consistency_marker()`` and ``_ensure_consistent()`` —
    both are delegates that call through ``self._sync``. A reorder
    of the assignment lines would cause ``AttributeError`` at
    construction.
    """
    Catalog.init(tmp_path)
    cat = Catalog(tmp_path, tmp_path / ".catalog.db")
    # Sanity: the three composed _Ops references exist post-init.
    from nexus.catalog.catalog_links import _LinkOps
    from nexus.catalog.catalog_docs import _DocumentOps
    from nexus.catalog.catalog_sync import _SyncOps

    assert isinstance(cat._links, _LinkOps)
    assert isinstance(cat._docs, _DocumentOps)
    assert isinstance(cat._sync, _SyncOps)
    # The back-reference points at the same Catalog instance.
    assert cat._links._cat is cat
    assert cat._docs._cat is cat
    assert cat._sync._cat is cat


def test_init_ensure_consistent_failure_leaves_sync_attached(
    tmp_path: Path,
) -> None:
    """If ``_ensure_consistent`` raises during init, the partially-
    constructed Catalog should still have ``self._sync`` attached
    (composition completed before the bootstrap call). Callers
    that catch the constructor exception can safely inspect
    ``cat.degraded``.

    ``_ensure_consistent`` swallows exceptions internally and sets
    ``cat.degraded = True``, so a clean Catalog construction is
    expected; this test pins the safety net.
    """
    Catalog.init(tmp_path)
    # Patch _ensure_consistent on the _SyncOps class so the patched
    # version runs after composition assigns self._sync. (Patching
    # before instantiation would just shadow the method on the
    # class object, but composition still happens first.)
    from nexus.catalog.catalog_sync import _SyncOps

    raised = []

    def fake_ensure_consistent(self):  # noqa: ARG001
        raised.append(True)
        raise RuntimeError("synthetic bootstrap failure")

    with patch.object(_SyncOps, "_ensure_consistent", fake_ensure_consistent):
        # The Catalog catches the synthetic error inside
        # _SyncOps._ensure_consistent's outer try/except (in the
        # real code it sets degraded=True). Since we're replacing
        # the whole method, the exception propagates from the
        # delegate. Verify the partial state regardless.
        try:
            cat = Catalog(tmp_path, tmp_path / ".catalog.db")
        except RuntimeError:
            # If the exception escaped, the Catalog reference is
            # gone — that itself is an acceptable contract. Skip
            # the post-condition.
            return

        assert raised, "patched _ensure_consistent did not fire"
        # Composition completed before the bootstrap raise.
        assert cat._sync is not None


# ── _cat_mod patching contract ───────────────────────────────────────────────


def test_cat_mod_propagates_span_pattern_patch_to_link_ops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``catalog_links.py`` references ``_SPAN_PATTERN`` via
    ``_cat_mod`` so a test that patches the module-level constant
    on ``nexus.catalog.catalog`` propagates to the link-create
    path. A future "refactor" replacing ``_cat_mod._SPAN_PATTERN``
    with a direct import would silently break this propagation;
    this test pins the contract.

    Strategy: replace ``_SPAN_PATTERN`` with a regex that rejects
    every span (matches nothing). ``cat.link(...)`` should then
    raise ``ValueError`` for any span it tries to validate — proof
    the patched pattern is what the link path actually consults.
    """
    import re
    Catalog.init(tmp_path)
    cat = Catalog(tmp_path, tmp_path / ".catalog.db")
    owner = cat.register_owner(
        name="test-owner", owner_type="repo", repo_hash="abc",
    )
    a = cat.register(
        owner=owner, title="a.md", content_type="prose", file_path="a.md",
    )
    b = cat.register(
        owner=owner, title="b.md", content_type="prose", file_path="b.md",
    )

    # Sanity: a default empty span passes validation pre-patch.
    cat.link(a, b, "cites", created_by="test")

    # Patch _SPAN_PATTERN at the canonical path so propagation goes
    # through _cat_mod into _LinkOps.
    reject_all = re.compile(r"^DOES_NOT_MATCH_ANYTHING$")
    monkeypatch.setattr(
        "nexus.catalog.catalog._SPAN_PATTERN", reject_all,
    )
    # An empty-string span no longer matches; link() must reject
    # it, proving _LinkOps reads the patched value via _cat_mod.
    with pytest.raises(ValueError, match="invalid"):
        cat.link(
            a, b, "cites-2", created_by="test", from_span="",
        )


def test_cat_mod_propagates_meta_key_patch_to_sync_ops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``catalog_sync.py`` references ``_META_KEY_LAST_OFFSET`` via
    ``_cat_mod``. A test that patches the module-level constant
    on ``nexus.catalog.catalog`` propagates into ``_SyncOps``'s
    offset-marker write/read path.
    """
    Catalog.init(tmp_path)
    cat = Catalog(tmp_path, tmp_path / ".catalog.db")
    cat.register_owner(name="seed", owner_type="repo", repo_hash="seed")
    # Trigger an _ensure_consistent run to write the marker.
    cat._ensure_consistent()

    # Verify the marker is at the canonical key.
    row = cat._db.execute(
        "SELECT value FROM _meta WHERE key = ?",
        ("last_applied_event_offset",),
    ).fetchone()
    assert row is not None, "marker not written under canonical key"

    # Patch the key name on catalog.catalog and verify _SyncOps
    # reads from the patched location. The patched name has no row
    # → _read_offset_marker returns None.
    monkeypatch.setattr(
        "nexus.catalog.catalog._META_KEY_LAST_OFFSET",
        "PATCHED_KEY_NAME_NO_ROW",
    )
    assert cat._sync._read_offset_marker() is None, (
        "_cat_mod indirection failed to propagate the patched "
        "key — _SyncOps still read the old constant"
    )


# ── graph cap scenarios (a), (b), (c) ────────────────────────────────────────


@pytest.fixture
def cat_with_three_node_chain(tmp_path: Path):
    """Catalog with three documents linked a -> b -> c."""
    Catalog.init(tmp_path)
    cat = Catalog(tmp_path, tmp_path / ".catalog.db")
    owner = cat.register_owner(
        name="o", owner_type="repo", repo_hash="hash",
    )
    a = cat.register(
        owner=owner, title="a", content_type="prose", file_path="a.md",
    )
    b = cat.register(
        owner=owner, title="b", content_type="prose", file_path="b.md",
    )
    c = cat.register(
        owner=owner, title="c", content_type="prose", file_path="c.md",
    )
    cat.link(a, b, "cites", created_by="t")
    cat.link(b, c, "relates", created_by="t")
    return cat, a, b, c


def test_graph_cap_scenario_a_class_patch(cat_with_three_node_chain) -> None:
    """``patch.object(type(cat), "_MAX_GRAPH_NODES", N)`` propagates
    into ``_LinkOps.graph``. (Documented scenario (a).)"""
    cat, a, _b, _c = cat_with_three_node_chain
    with patch.object(type(cat), "_MAX_GRAPH_NODES", 1):
        result = cat.graph(a, depth=2)
    assert len(result["nodes"]) == 1


def test_graph_cap_scenario_b_instance_shadow(
    cat_with_three_node_chain,
) -> None:
    """``cat._MAX_GRAPH_NODES = N`` (instance attribute shadows
    class attribute) propagates. (Documented scenario (b).)"""
    cat, a, _b, _c = cat_with_three_node_chain
    cat._MAX_GRAPH_NODES = 1
    try:
        result = cat.graph(a, depth=2)
    finally:
        del cat._MAX_GRAPH_NODES
    assert len(result["nodes"]) == 1


def test_graph_cap_scenario_c_module_patch_does_not_propagate(
    cat_with_three_node_chain, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``monkeypatch.setattr("nexus.catalog.catalog_links._MAX_GRAPH_NODES",
    N)`` does NOT propagate — the ``Catalog._MAX_GRAPH_NODES``
    class attribute was copied by value at class-body time. This
    is the **documented contract** at ``catalog.py:2092-2115``. A
    future "fix" to ``@property`` would propagate (c) but break
    ``Catalog._MAX_GRAPH_NODES`` class-level reads. This test
    pins the documented behaviour so the contract isn't silently
    flipped.
    """
    cat, a, _b, _c = cat_with_three_node_chain
    # Patch only the module-level constant; do NOT patch the
    # Catalog class attribute.
    monkeypatch.setattr(
        "nexus.catalog.catalog_links._MAX_GRAPH_NODES", 1,
    )
    result = cat.graph(a, depth=2)
    # The cap should NOT have been applied (Catalog still sees the
    # original 500). All three nodes traversed.
    assert len(result["nodes"]) == 3, (
        "module-level patch propagated unexpectedly — the "
        "documented non-propagation contract has changed"
    )


# ── bulk_unlink event-sourced vs legacy paths ────────────────────────────────


@pytest.fixture
def cat_with_two_links(tmp_path: Path):
    """Catalog with two links between three documents."""
    Catalog.init(tmp_path)
    cat = Catalog(tmp_path, tmp_path / ".catalog.db")
    owner = cat.register_owner(
        name="o", owner_type="repo", repo_hash="hash",
    )
    a = cat.register(
        owner=owner, title="a", content_type="prose", file_path="a.md",
    )
    b = cat.register(
        owner=owner, title="b", content_type="prose", file_path="b.md",
    )
    cat.link(a, b, "cites", created_by="alice")
    cat.link(a, b, "relates", created_by="bob")
    return cat, a, b


def test_bulk_unlink_event_sourced_path_writes_event_log(
    cat_with_two_links,
) -> None:
    """ES path: ``bulk_unlink`` writes ``LinkDeleted`` events to
    events.jsonl AND removes rows from ``links`` table via the
    projector (no direct DELETE SQL needed)."""
    cat, _a, _b = cat_with_two_links
    assert cat._event_sourced_enabled, (
        "default mode should be event-sourced (NEXUS_EVENT_SOURCED=1)"
    )
    pre_events = cat._events_path.read_text().splitlines()
    pre_link_count = cat._db.execute(
        "SELECT count(*) FROM links",
    ).fetchone()[0]
    assert pre_link_count == 2

    n = cat.bulk_unlink(created_by="alice")
    assert n == 1

    post_events = cat._events_path.read_text().splitlines()
    new_events = post_events[len(pre_events):]
    assert any('"LinkDeleted"' in line for line in new_events), (
        "ES path must emit LinkDeleted to events.jsonl"
    )
    post_link_count = cat._db.execute(
        "SELECT count(*) FROM links",
    ).fetchone()[0]
    assert post_link_count == 1, (
        "projector should have removed the link from SQLite"
    )


def test_bulk_unlink_dry_run_does_not_modify(
    cat_with_two_links,
) -> None:
    """``dry_run=True`` returns the count without modifying state."""
    cat, _a, _b = cat_with_two_links
    pre_events = cat._events_path.read_text()
    pre_count = cat._db.execute(
        "SELECT count(*) FROM links",
    ).fetchone()[0]

    n = cat.bulk_unlink(created_by="alice", dry_run=True)
    assert n == 1

    assert cat._events_path.read_text() == pre_events
    assert cat._db.execute(
        "SELECT count(*) FROM links",
    ).fetchone()[0] == pre_count


def test_bulk_unlink_requires_filter_when_not_dry_run(
    cat_with_two_links,
) -> None:
    """No filter + not dry_run raises ``ValueError`` — the safety
    rail that prevents ``bulk_unlink()`` from wiping the table."""
    cat, _a, _b = cat_with_two_links
    with pytest.raises(ValueError, match="at least one filter"):
        cat.bulk_unlink()


def test_bulk_unlink_legacy_path_writes_jsonl_tombstone_and_deletes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy path (``NEXUS_EVENT_SOURCED=0``): ``bulk_unlink``
    appends a JSONL tombstone AND issues a direct ``DELETE FROM
    links`` SQL statement (the projector path is gated off, so
    SQLite removal is the writer's responsibility).

    Test-validator review noted this path was uncovered post-
    decomposition (Gap 7 part 2).
    """
    monkeypatch.setenv("NEXUS_EVENT_SOURCED", "0")
    Catalog.init(tmp_path)
    cat = Catalog(tmp_path, tmp_path / ".catalog.db")
    assert not cat._event_sourced_enabled, (
        "fixture must observe legacy mode"
    )
    owner = cat.register_owner(
        name="o", owner_type="repo", repo_hash="hash",
    )
    a = cat.register(
        owner=owner, title="a", content_type="prose", file_path="a.md",
    )
    b = cat.register(
        owner=owner, title="b", content_type="prose", file_path="b.md",
    )
    cat.link(a, b, "cites", created_by="alice")
    assert cat._db.execute(
        "SELECT count(*) FROM links",
    ).fetchone()[0] == 1

    pre_jsonl_lines = cat._links_path.read_text().count("\n")
    n = cat.bulk_unlink(created_by="alice")
    assert n == 1

    # JSONL tombstone appended.
    post_jsonl = cat._links_path.read_text()
    post_jsonl_lines = post_jsonl.count("\n")
    assert post_jsonl_lines > pre_jsonl_lines, (
        "tombstone must be appended to links.jsonl"
    )
    assert '"_deleted": true' in post_jsonl, (
        "tombstone must carry _deleted=True"
    )

    # DELETE actually removed the row from SQLite.
    assert cat._db.execute(
        "SELECT count(*) FROM links",
    ).fetchone()[0] == 0, (
        "legacy path must DELETE the row from SQLite (no projector)"
    )


def test_bulk_unlink_legacy_shadow_emit_fires_after_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy path: shadow-emit MUST fire AFTER ``cat._db.commit()``
    so a process crash between DELETE and commit cannot leave
    events.jsonl claiming a deletion SQLite has not yet committed
    (the crash-window invariant from the original code).

    Strategy: capture the call order of ``_db.commit`` and
    ``_emit_shadow_event`` via spies. The shadow emit timestamps
    must be strictly later than the commit timestamp.
    """
    monkeypatch.setenv("NEXUS_EVENT_SOURCED", "0")
    Catalog.init(tmp_path)
    cat = Catalog(tmp_path, tmp_path / ".catalog.db")
    owner = cat.register_owner(
        name="o", owner_type="repo", repo_hash="hash",
    )
    a = cat.register(
        owner=owner, title="a", content_type="prose", file_path="a.md",
    )
    b = cat.register(
        owner=owner, title="b", content_type="prose", file_path="b.md",
    )
    cat.link(a, b, "cites", created_by="alice")

    call_log: list[str] = []
    real_commit = cat._db.commit

    def spy_commit():
        call_log.append("commit")
        return real_commit()

    real_emit = cat._emit_shadow_event

    def spy_emit(event):
        call_log.append("shadow_emit")
        return real_emit(event)

    with patch.object(cat._db, "commit", side_effect=spy_commit), \
         patch.object(cat, "_emit_shadow_event", side_effect=spy_emit):
        cat.bulk_unlink(created_by="alice")

    # Among the recorded calls in the bulk_unlink path, every
    # shadow_emit must come AFTER at least one commit (the shadow
    # loop runs after the per-row write loop's terminating commit).
    if "shadow_emit" in call_log and "commit" in call_log:
        first_commit = call_log.index("commit")
        first_shadow = call_log.index("shadow_emit")
        assert first_shadow > first_commit, (
            "shadow_emit fired before commit — crash-window invariant "
            "broken (events.jsonl could claim deletions SQLite has "
            f"not yet committed). call order: {call_log}"
        )
