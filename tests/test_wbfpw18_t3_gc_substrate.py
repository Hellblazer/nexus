# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-wbfpw.18 (RDR-192 Step 8, client half): ``nx t3 gc`` against a REAL engine.

The wire tests (``test_wbfpw18_t3_gc_wire.py``) pin which routes the verb uses. These pin what
the engine does with them: the move quarantines exactly the rows the reapable predicate selects,
a client write that lands between the listing and the act wins, the census gate holds, the floor
holds, and one pass leaves exactly one ``gc_audit`` row (the engine's).

Fixture: ``tests/_reapable_cli_fixture.py`` (S1b row shapes, aged with ``tests/_reapable_age.py``).
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.cli import main
from tests._catalog_fixture_ops import ActiveCatalog
from tests._chunk_seed import seed_chunks_direct
from tests._reapable_age import age_chunks_past_grace
from tests._reapable_cli_fixture import (
    MixedCollection,
    build_mixed_collection,
    chash_of,
    collection_name,
    write_chunks,
)

# Not integration-marked: the substrate provisions itself, and CI's default selection must run
# this RDR-192 pin (nexus-wbfpw.38).


@pytest.fixture
def env(t2_service_env, monkeypatch):
    import nexus.db.http_vector_client as hvc

    monkeypatch.delenv("NX_GC_FORCE", raising=False)
    monkeypatch.delenv("NX_GC_FLOOR_FRACTION", raising=False)
    cat = ActiveCatalog()
    return hvc.HttpVectorClient(tenant=t2_service_env), cat, CliRunner()


def _origin_ids(client, coll: str) -> set[str]:
    return set(client.get_collection(coll).get_all_metadata(include_non_live=True)["ids"])


def _quarantined_ids(client, coll: str) -> set[str]:
    from nexus.catalog.chunk_quarantine import quarantine_collection_name

    return _origin_ids(client, quarantine_collection_name(coll))


def _gc(runner, coll, *args):
    return runner.invoke(main, ["t3", "gc", "-c", coll, *args])


def _audit_rows(coll: str) -> list[dict]:
    from nexus.catalog.http_catalog_client import HttpCatalogClient

    return HttpCatalogClient().gc_audit_list(collection=coll, limit=50)


# ── the move ──────────────────────────────────────────────────────────────────


def test_the_move_quarantines_exactly_the_reapable_rows_and_the_engine_audits_it_once(env):
    client, cat, runner = env
    fx = build_mixed_collection(cat, "wbfpw18-move")

    result = _gc(runner, fx.name, "--no-dry-run", "--yes")
    assert result.exit_code == 0, result.output
    assert "quarantined 2 chunk(s)" in result.output

    # Exactly the reapable rows left origin; owned, tombstoned-owner, shared and fresh stayed.
    assert _origin_ids(client, fx.name) == fx.everything - fx.reapable
    assert _quarantined_ids(client, fx.name) == fx.reapable

    # One pass, one audit row, written by the engine. The verb's own t3_gc row is gone.
    rows = _audit_rows(fx.name)
    assert len(rows) == 1, rows
    assert rows[0]["operation"] in {"gc_quarantine_orphans", "gc_quarantine_orphans_bounded"}
    assert rows[0]["actor"] == "engine"
    assert {r["operation"] for r in rows}.isdisjoint({"t3_gc"})


def test_dry_run_moves_nothing_and_names_the_reapable_rows(env):
    client, cat, runner = env
    fx = build_mixed_collection(cat, "wbfpw18-dry")

    result = _gc(runner, fx.name, "--dry-run")
    assert result.exit_code == 0, result.output
    for chash in fx.reapable:
        assert chash in result.output
    for chash in fx.everything - fx.reapable:
        assert chash not in result.output
    assert _origin_ids(client, fx.name) == fx.everything
    assert _audit_rows(fx.name) == []


def test_dry_run_candidacy_equals_the_default_grace_listing_across_keyset_pages(env):
    client, cat, runner = env
    fx = build_mixed_collection(cat, "wbfpw18-list")
    # Enough extra aged orphans that a page size of 2 needs several keyset pages.
    extra = write_chunks(fx.name, [f"wbfpw18-list extra {i}: ownerless and old" for i in range(5)])
    age_chunks_past_grace(fx.name)  # ages `fresh` too, so it joins the reapable set here
    expected = fx.reapable | set(extra) | {fx.fresh}

    paged = {r["chash"] for r in client.reapable_chunks(fx.name, page_limit=2)}
    assert paged == expected
    assert len(paged) >= 5, "non-vacuity: several pages were needed"

    result = _gc(runner, fx.name, "--dry-run")
    assert result.exit_code == 0, result.output
    named = {c for c in paged if c in result.output}
    assert named == paged
    for chash in fx.everything - expected:
        assert chash not in result.output


# ── the engine route re-checks, the listing does not ──────────────────────────


def test_a_chunk_a_client_rewrites_between_the_listing_and_the_act_is_not_quarantined(env):
    """The route's own statement carries the predicate and evaluates it against the rows it locks, so
    a write that lands after the (lock-free) listing wins. A list-then-delete-by-id verb would have
    deleted the chunk the listing named."""
    import nexus.db.http_vector_client as hvc

    client, cat, runner = env
    fx = build_mixed_collection(cat, "wbfpw18-race")
    victim, other = sorted(fx.reapable)
    text = f"wbfpw18-race rewrite of {victim}"

    original = hvc.HttpVectorClient.reapable_chunks

    def listing_then_a_client_write(self, *a, **kw):
        rows = list(original(self, *a, **kw))
        assert victim in {r["chash"] for r in rows}, "non-vacuity: the listing named the victim"
        # A real client write path re-writes the chunk (restamps last_written_at), exactly the
        # shape of a re-index touching a chunk the listing just snapshotted.
        seed_chunks_direct(
            fx.name, ids=[victim], documents=[text], embed=True,
            metadatas=[{"chunk_text_hash": victim, "title": "rewritten"}],
        )
        return rows

    with patch.object(hvc.HttpVectorClient, "reapable_chunks", listing_then_a_client_write):
        result = _gc(runner, fx.name, "--no-dry-run", "--yes")

    assert result.exit_code == 0, result.output
    assert victim in _origin_ids(client, fx.name), "the rewritten chunk must stay"
    assert _quarantined_ids(client, fx.name) == {other}
    assert "engine moved 1" in result.output


# ── R8: the census gate ───────────────────────────────────────────────────────


def test_a_collection_with_a_legacy_unmanifested_note_is_refused_and_nothing_moves(env):
    client, cat, runner = env
    coll = collection_name("wbfpw18-r8")
    owner = cat.register_owner("wbfpw18-r8-owner", "curator")
    (orphan, legacy) = write_chunks(coll, [
        "wbfpw18-r8 orphan: nothing names it.",
        "wbfpw18-r8 legacy note: a live document names it by meta.doc_id, no manifest row.",
    ])
    # The pre-nexus-b6enc legacy note shape: a note-shaped document with NO manifest write.
    cat.register(owner, "wbfpw18-r8-legacy", content_type="knowledge",
                 physical_collection=coll, meta={"doc_id": legacy})
    age_chunks_past_grace(coll)

    dry = _gc(runner, coll, "--dry-run")
    assert dry.exit_code == 0, dry.output
    assert "legacy-unmanifested" in dry.output and "REFUSE" in dry.output

    result = _gc(runner, coll, "--no-dry-run", "--yes")
    assert result.exit_code != 0
    assert "legacy-unmanifested" in result.output
    assert _origin_ids(client, coll) == {orphan, legacy}, "nothing may move"
    assert _audit_rows(coll) == []


# ── the fraction floor ────────────────────────────────────────────────────────


def test_a_pass_over_the_floor_is_refused_and_nx_gc_force_overrides_it(env, monkeypatch):
    import nexus.indexer as indexer

    client, cat, runner = env
    fx = build_mixed_collection(cat, "wbfpw18-floor")
    # 2 of 6 chunks are reapable: a third of the collection, over the 25% default floor once the
    # collection is big enough for the floor to apply (the real minimum is 100 chunks).
    monkeypatch.setattr(indexer, "_GC_FLOOR_MIN_CHUNKS", 5)

    result = _gc(runner, fx.name, "--no-dry-run", "--yes")
    assert result.exit_code != 0
    assert "NX_GC_FLOOR_FRACTION" in result.output and "NX_GC_FORCE=1" in result.output
    assert _origin_ids(client, fx.name) == fx.everything, "nothing may move"
    assert _audit_rows(fx.name) == []

    monkeypatch.setenv("NX_GC_FORCE", "1")
    forced = _gc(runner, fx.name, "--no-dry-run", "--yes")
    assert forced.exit_code == 0, forced.output
    assert _quarantined_ids(client, fx.name) == fx.reapable


# ── the RUNFENCE breaker is unchanged ─────────────────────────────────────────


def test_a_document_mid_index_still_refuses_the_whole_collection(env):
    client, cat, runner = env
    coll = collection_name("wbfpw18-fence")
    owner = cat.register_owner("wbfpw18-fence-owner", "curator")
    (orphan,) = write_chunks(coll, ["wbfpw18-fence orphan: nothing names it."])
    tumbler = cat.register(
        owner, "wbfpw18-fence-doc", content_type="text", file_path="/tmp/wbfpw18-fence.md",
        physical_collection=coll,
    )
    cat.begin_index_run(str(tumbler), "content-hash-1", "run-1", coll)
    age_chunks_past_grace(coll)

    result = _gc(runner, coll, "--no-dry-run", "--yes")
    assert result.exit_code != 0
    assert "not index_state='complete'" in result.output
    assert _origin_ids(client, coll) == {orphan}

    # --allow-empty-manifest-set as well: this collection holds no owned chunk, which the nexus-jqrtp
    # guard (kept) refuses on its own; without it that refusal would mask the breaker under test.
    overridden = _gc(runner, coll, "--no-dry-run", "--yes",
                     "--allow-incomplete-index-state", "--allow-empty-manifest-set")
    assert overridden.exit_code == 0, overridden.output
    assert _quarantined_ids(client, coll) == {orphan}


def test_a_clean_collection_is_a_noop_with_no_audit_row(env):
    client, cat, runner = env
    coll = collection_name("wbfpw18-clean")
    owner = cat.register_owner("wbfpw18-clean-owner", "curator")
    (kept,) = write_chunks(coll, ["wbfpw18-clean owned chunk."])
    doc = cat.register(owner, "wbfpw18-clean-doc", content_type="knowledge",
                       physical_collection=coll, meta={"doc_id": kept})
    cat.append_manifest_chunks(str(doc), [{"chash": kept, "position": 0}], collection=coll)
    cat.resync_chunk_count_cache(str(doc))
    age_chunks_past_grace(coll)

    result = _gc(runner, coll, "--no-dry-run", "--yes")
    assert result.exit_code == 0, result.output
    assert "nothing to do" in result.output
    assert _origin_ids(client, coll) == {kept}
    assert _audit_rows(coll) == []


def test_chash_helper_is_the_sha256_the_engine_keys_on():
    # Non-vacuity for the fixture: a mismatch here would make every set comparison above vacuous.
    assert chash_of("x") == "2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881"
