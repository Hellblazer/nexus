# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-192 (nexus-wbfpw.41): the upgrade-ladder rung against the real engine.

The legacy-unmanifested state (a catalog document that owns a chunk but never
got a manifest row, the shape of a note stored before nexus-b6enc) is seeded
with DIRECT SQL into ``nexus.chunks``, not through ``/v1/vectors/upsert-chunks``:
that route is about to refuse ownerless writes, and this state is by
definition ownerless-at-the-manifest. The catalog side (owner, documents,
tombstones, manifest rows) goes through the catalog API, which is how those
rows come to exist in production.

Driven through ``nexus.commands.upgrade._run_ladder`` with its production
wiring (``default_registry`` and the engine-backed ``HttpLadderStore`` behind
``DeferredLadderLedger``), so the walk, the verify-before-record guard, the
durable completion record in ``nexus.ladder_completions`` and the reaper gate
are exercised together.

Not integration-marked (see ``tests/test_rdr192_pins_not_integration_marked_lint.py``):
the substrate provisions itself.
"""
from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

import nexus.db.http_vector_client as hvc
from nexus.commands.t3 import t3
from nexus.commands.upgrade import _run_ladder
from nexus.upgrade_ladder.http_store import HttpLadderStore
from nexus.upgrade_ladder.registry import LadderRegistry
from nexus.upgrade_ladder.rungs.rdr192_manifest_backfill import (
    MEMO_PROJECT,
    MEMO_TITLE,
    RETRY_ENV,
    RUNG_NAME,
    BackfillIncompleteError,
    CensusUnavailable,
    Rdr192ManifestBackfillRung,
    rdr192_backfill_complete,
    require_rdr192_backfill_complete,
)
from nexus.upgrade_ladder.runner import LadderRunner, RungOutcome
from tests.upgrade._substrate_sql import MODEL, lit, psql, register_collection, seed_chunk


def _coll(tag: str) -> str:
    return f"knowledge__wbfpw41{tag}__{MODEL}__v1"


def _stored(tenant: str, collection: str) -> set[str]:
    out = psql(
        "SELECT encode(chash, 'hex') FROM nexus.chunks "
        f"WHERE tenant_id = {lit(tenant)} AND collection = {lit(collection)}"
    )
    return {line for line in out.splitlines() if line}


def _census_totals(tenant: str, collection: str) -> dict[str, int]:
    import nexus.db.http_vector_client as hvc  # noqa: PLC0415

    return hvc.HttpVectorClient(tenant=tenant).manifest_less_census(collection, limit=1)["totals"]


def _live(tenant: str, collection: str, chash: str) -> bool:
    import nexus.db.http_vector_client as hvc  # noqa: PLC0415

    col = hvc.HttpVectorClient(tenant=tenant).get_collection(collection)
    return chash in col.get(ids=[chash], include=["metadatas"])["ids"]


def _legacy_note(
    cat, owner, collection: str, title: str, tenant: str, *,
    doc_collection: str | None = None, chunk_count: int = 0,
):
    """A note-shaped catalog document plus its chunk, with NO manifest row."""
    tumbler = str(cat.register(
        owner, title, content_type="knowledge",
        physical_collection=doc_collection or collection,
        chunk_count=chunk_count,
    ))
    chash = seed_chunk(
        tenant, collection, f"{title} body",
        {"catalog_doc_id": tumbler, "title": title, "chunk_index": 0, "chunk_count": 1},
    )
    return tumbler, chash


@pytest.fixture
def catalog(t2_service_env):
    from tests._catalog_fixture_ops import ActiveCatalog  # noqa: PLC0415

    cat = ActiveCatalog()
    return cat, cat.register_owner("wbfpw41", "curator")


def test_walk_backfills_a_legacy_note_and_records_completion(t2_service_env, catalog, capsys) -> None:
    tenant = t2_service_env
    cat, owner = catalog
    coll = _coll("heal")
    tumbler, chash = _legacy_note(cat, owner, coll, "legacy note", tenant)

    # The state this bead exists for. Non-vacuity: the chunk is physically
    # stored, the census names it, live(c) hides it, and the reaper gate is
    # closed. None of this is inferred from the fix.
    assert chash in _stored(tenant, coll)
    assert _census_totals(tenant, coll)["legacy-unmanifested"] == 1
    assert not _live(tenant, coll, chash)
    assert cat.get_manifest(tumbler) == []
    assert rdr192_backfill_complete() is False
    with pytest.raises(BackfillIncompleteError):
        require_rdr192_backfill_complete()

    _run_ladder(dry_run=False, auto_mode=False)

    assert f"rung '{RUNG_NAME}' converged and verified" in capsys.readouterr().out
    assert _census_totals(tenant, coll)["legacy-unmanifested"] == 0
    assert [row.chash for row in cat.get_manifest(tumbler)] == [chash]
    assert _live(tenant, coll, chash), "the backfilled note is visible to search and get again"
    assert chash in _stored(tenant, coll)
    assert rdr192_backfill_complete() is True
    require_rdr192_backfill_complete()  # does not raise
    with HttpLadderStore() as ledger:
        record = ledger.completions()[RUNG_NAME]
    assert record.detail.startswith("census: collections=1 "), record.detail
    assert "legacy-unmanifested=0" in record.detail and "unclassified=0" in record.detail


def test_second_walk_is_a_no_op_and_sends_no_census_request(
    t2_service_env, catalog, monkeypatch,
) -> None:
    """`nx upgrade --auto` walks the ladder at every SessionStart, so once the
    completion is on file the walk must not census the tenant again."""

    tenant = t2_service_env
    cat, owner = catalog
    coll = _coll("idem")
    tumbler, chash = _legacy_note(cat, owner, coll, "idempotent note", tenant, chunk_count=1)

    census_calls: list[str] = []
    real = hvc.HttpVectorClient.manifest_less_census

    def counting(self, collection, limit=100, offset=0):
        census_calls.append(collection)
        return real(self, collection, limit=limit, offset=offset)

    monkeypatch.setattr(hvc.HttpVectorClient, "manifest_less_census", counting)

    _run_ladder(dry_run=False, auto_mode=True)
    manifest_after_first = [(r.chash, r.position) for r in cat.get_manifest(tumbler)]
    assert manifest_after_first == [(chash, 0)]
    assert census_calls, "control: the first walk did census"
    calls_after_first = len(census_calls)

    _run_ladder(dry_run=False, auto_mode=True)
    assert len(census_calls) == calls_after_first
    assert [(r.chash, r.position) for r in cat.get_manifest(tumbler)] == manifest_after_first
    assert rdr192_backfill_complete() is True
    assert _census_totals(tenant, coll)["legacy-unmanifested"] == 0  # a fresh read agrees


def test_dry_run_reports_pending_and_changes_nothing(t2_service_env, catalog, capsys) -> None:
    tenant = t2_service_env
    cat, owner = catalog
    coll = _coll("dry")
    tumbler, _chash_ = _legacy_note(cat, owner, coll, "dry-run note", tenant)

    _run_ladder(dry_run=True, auto_mode=False)

    out = capsys.readouterr().out
    assert f"rung '{RUNG_NAME}' pending" in out
    assert "no completion recorded" in out
    assert cat.get_manifest(tumbler) == []
    assert rdr192_backfill_complete() is False


def test_other_buckets_are_left_alone_and_nothing_is_deleted(t2_service_env, catalog) -> None:
    """superseded, dead-owner and no-owner rows are the REAPER's input. The
    rung neither heals them nor blocks on them, and it never deletes a chunk."""
    tenant = t2_service_env
    cat, owner = catalog
    coll = _coll("buckets")

    _legacy_tumbler, legacy = _legacy_note(cat, owner, coll, "legacy in mixed collection", tenant)

    no_owner = seed_chunk(tenant, coll, "no owner body", {"title": "no-owner"})

    dead_doc = str(cat.register(owner, "dead doc", content_type="knowledge", physical_collection=coll))
    dead_owner = seed_chunk(tenant, coll, "dead owner body", {"catalog_doc_id": dead_doc})
    cat.delete_document(dead_doc)

    live_doc = str(cat.register(owner, "live doc", content_type="knowledge", physical_collection=coll))
    current = seed_chunk(tenant, coll, "current body", {"catalog_doc_id": live_doc})
    superseded = seed_chunk(tenant, coll, "superseded body", {"catalog_doc_id": live_doc})
    cat.write_manifest(live_doc, [{"chash": current, "position": 0}], collection=coll)

    before = _census_totals(tenant, coll)
    assert (before["legacy-unmanifested"], before["no-owner"], before["dead-owner"], before["superseded"]) == (1, 1, 1, 1)
    stored_before = _stored(tenant, coll)
    assert {legacy, no_owner, dead_owner, current, superseded} <= stored_before

    _run_ladder(dry_run=False, auto_mode=True)

    after = _census_totals(tenant, coll)
    assert after["legacy-unmanifested"] == 0
    assert (after["no-owner"], after["dead-owner"], after["superseded"]) == (1, 1, 1)
    assert _stored(tenant, coll) == stored_before, "the rung must never delete a chunk"
    assert rdr192_backfill_complete() is True


def _memo_note():
    from nexus.db.t2.http_memory_store import HttpMemoryStore  # noqa: PLC0415 — substrate env is set by the fixture

    row = HttpMemoryStore().get(project=MEMO_PROJECT, title=MEMO_TITLE)
    return json.loads(row["content"]) if row else None


def test_an_unhealable_residual_defers_loudly_records_nothing_and_is_not_retried(
    t2_service_env, catalog, capsys, monkeypatch,
) -> None:
    """A legacy chunk whose owner is registered under ANOTHER collection is
    invisible to the collection-scoped backfill. The walk DEFERS (it does not
    raise, so the rest of `nx upgrade` still runs), says which collection and
    what to do, records nothing, and the reaper gate stays closed. A second
    walk over the same residual does not run the backfill again. Once the
    operator manifests the chunk, the next walk records and clears the memo."""
    import nexus.catalog.manifest_backfill as manifest_backfill

    tenant = t2_service_env
    cat, owner = catalog
    coll = _coll("stuck")
    elsewhere = _coll("elsewhere")
    register_collection(tenant, elsewhere)
    tumbler, chash = _legacy_note(cat, owner, coll, "stranded note", tenant, doc_collection=elsewhere)

    backfill_calls: list[str] = []
    real = manifest_backfill.backfill_manifest_for_collection

    def counting(catalog_, t3, collection_name, **kw):
        backfill_calls.append(collection_name)
        return real(catalog_, t3, collection_name, **kw)

    monkeypatch.setattr(manifest_backfill, "backfill_manifest_for_collection", counting)

    _run_ladder(dry_run=False, auto_mode=False)  # does not raise
    out = capsys.readouterr().out
    assert f"rung '{RUNG_NAME}' deferred" in out
    assert coll in out and "nx store put" in out and "cannot heal" in out
    assert "hidden from `nx store get`" in out and "your own copy" in out

    assert _census_totals(tenant, coll)["legacy-unmanifested"] == 1
    assert chash in _stored(tenant, coll), "a residual is reported, never deleted"
    assert rdr192_backfill_complete() is False
    with pytest.raises(BackfillIncompleteError):
        require_rdr192_backfill_complete()
    note = _memo_note()
    assert note is not None and coll in note["detail"], "the residual is remembered in T2"
    calls_after_first = len(backfill_calls)
    assert calls_after_first >= 1  # control: the first walk did try to backfill

    _run_ladder(dry_run=False, auto_mode=False)
    assert f"rung '{RUNG_NAME}' deferred" in capsys.readouterr().out
    assert len(backfill_calls) == calls_after_first, "a residual inside the gate is not retried"
    assert rdr192_backfill_complete() is False

    cat.write_manifest(tumbler, [{"chash": chash, "position": 0}], collection=coll)

    # Inside the 24h gate the fix is not even looked for ...
    _run_ladder(dry_run=False, auto_mode=False)
    assert "not re-examined" in capsys.readouterr().out
    assert rdr192_backfill_complete() is False
    # ... until the operator forces a retry (or the gate opens).
    monkeypatch.setenv(RETRY_ENV, "1")
    _run_ladder(dry_run=False, auto_mode=False)
    assert _census_totals(tenant, coll)["legacy-unmanifested"] == 0
    assert rdr192_backfill_complete() is True
    assert _memo_note() is None, "a recorded completion clears the residual memo"


def test_the_remedy_works_a_re_put_heals_a_stranded_legacy_note(t2_service_env, catalog, monkeypatch) -> None:
    """The remedy the deferred walk prints for a skipped legacy note is
    `nx store put` under the same title. Prove it: strand a legacy note the
    collection-scoped backfill cannot reach (its owner is registered under a
    different collection), re-put it through the real MCP store_put, and the
    census reads zero legacy chunks; the old chunk becomes the reaper's
    (superseded); the next walk records."""
    from unittest.mock import patch

    from nexus.aspect_readers import uri_for
    from nexus.corpus import t3_collection_name
    from nexus.mcp.core import store_put

    tenant = t2_service_env
    cat, owner = catalog
    client = hvc.HttpVectorClient(tenant=tenant)
    subject = "wbfpw41-reput"
    title = "wbfpw41 stranded note"
    coll = t3_collection_name(subject, t3=client)
    elsewhere = _coll("reput-elsewhere")
    register_collection(tenant, elsewhere)
    tumbler = str(cat.register(
        owner, title, content_type="knowledge", physical_collection=elsewhere,
        source_uri=uri_for(coll, title), chunk_count=1,
    ))
    old = seed_chunk(
        tenant, coll, "wbfpw41 the old text of a stranded note",
        {"catalog_doc_id": tumbler, "title": title, "chunk_index": 0, "chunk_count": 1},
    )
    assert _census_totals(tenant, coll)["legacy-unmanifested"] == 1  # control

    _run_ladder(dry_run=False, auto_mode=True)
    assert rdr192_backfill_complete() is False  # the walk cannot heal it

    with patch("nexus.mcp.core._get_t3", return_value=client):
        result = store_put(content="wbfpw41 the rewritten text of a stranded note", collection=subject, title=title)
    assert "Stored" in result, result

    totals = _census_totals(tenant, coll)
    assert totals["legacy-unmanifested"] == 0, totals
    manifest = [r.chash for r in cat.get_manifest(tumbler)]
    assert len(manifest) == 1 and old not in manifest, "the new chunk is manifested under the SAME document"
    assert old in _stored(tenant, coll), "the old chunk is not deleted; it is the reaper's now"
    assert totals["superseded"] == 1, totals

    monkeypatch.setenv(RETRY_ENV, "1")  # inside the 24h gate; the operator re-put, so retry now
    _run_ladder(dry_run=False, auto_mode=True)
    assert rdr192_backfill_complete() is True


def test_a_note_with_an_old_and_a_current_chunk_is_reported_not_manifested(
    t2_service_env, catalog,
) -> None:
    """Review S2. A legacy note whose old and current text both still carry its
    tumbler: the census calls EVERY such chunk legacy-unmanifested. Manifesting
    them would publish superseded text or be refused outright, so the backfill
    reports the document and writes nothing. Two registered counts cover both
    branches: one that disagrees with the two matched chunks, and an unknown
    (0) count where the two chunks collide on position 0."""
    from nexus.catalog.factory import make_catalog_reader
    from nexus.catalog.manifest_backfill import backfill_manifest_for_collection

    tenant = t2_service_env
    cat, owner = catalog
    for registered in (1, 0):
        coll = _coll(f"oldnew{registered}")
        tumbler = str(cat.register(
            owner, f"revised note {registered}", content_type="knowledge",
            physical_collection=coll, chunk_count=registered,
        ))
        meta = {"catalog_doc_id": tumbler, "chunk_index": 0, "chunk_count": 1}
        old = seed_chunk(tenant, coll, f"old text {registered}", {**meta, "title": "old"})
        current = seed_chunk(tenant, coll, f"current text {registered}", {**meta, "title": "current"})
        assert _census_totals(tenant, coll)["legacy-unmanifested"] == 2  # control

        result = backfill_manifest_for_collection(
            make_catalog_reader(), hvc.HttpVectorClient(tenant=tenant), coll,
            dry_run=False, only_gapped=True,
        )
        assert result.docs_skipped_chunk_count_mismatch == 1, registered
        assert result.docs_processed == 0 and result.chunks_written == 0
        assert cat.get_manifest(tumbler) == [], "neither chunk may be manifested"
        assert {old, current} <= _stored(tenant, coll)


def test_write_manifest_refuses_two_chunks_at_one_position_atomically(t2_service_env, catalog) -> None:
    """What the engine does with the request the guard above prevents: the
    manifest PRIMARY KEY is (tenant, doc_id, position), so two rows at one
    position fail the whole REPLACE. Nothing is written."""
    import httpx

    tenant = t2_service_env
    cat, owner = catalog
    coll = _coll("dup")
    tumbler, first = _legacy_note(cat, owner, coll, "dup note", tenant)
    second = seed_chunk(tenant, coll, "dup note second", {"catalog_doc_id": tumbler})
    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        cat.write_manifest(
            tumbler, [{"chash": first, "position": 0}, {"chash": second, "position": 0}],
            collection=coll,
        )
    assert excinfo.value.response.status_code == 409
    assert cat.get_manifest(tumbler) == []


def test_backfill_re_checks_the_manifest_immediately_before_writing(
    t2_service_env, catalog, monkeypatch,
) -> None:
    """Review S2: a note re-put between the pre-pass and the write already has
    its manifest, and write_manifest is an atomic REPLACE. The fresh read sees
    it and the backfill leaves the document alone."""
    from nexus.catalog.factory import make_catalog_reader
    from nexus.catalog.http_catalog_client import HttpCatalogClient
    from nexus.catalog.manifest_backfill import backfill_manifest_for_collection

    tenant = t2_service_env
    cat, owner = catalog
    coll = _coll("race")
    tumbler, _chash_ = _legacy_note(cat, owner, coll, "raced note", tenant, chunk_count=1)

    seen: list[str] = []

    def appeared(self, doc_id):
        seen.append(doc_id)
        return [object()]  # "a manifest row exists now"

    with monkeypatch.context() as scoped:
        scoped.setattr(HttpCatalogClient, "get_manifest", appeared)
        result = backfill_manifest_for_collection(
            make_catalog_reader(), hvc.HttpVectorClient(tenant=tenant), coll,
            dry_run=False, only_gapped=True,
        )
    assert seen == [tumbler], "the re-check ran, once, for the one gapped document"
    assert result.docs_skipped_has_manifest == 1 and result.chunks_written == 0
    assert cat.get_manifest(tumbler) == [], "the real manifest was never written"


def test_a_skipped_document_reaches_the_deferred_detail(t2_service_env, catalog, capsys) -> None:
    """The BackfillResult skip counters travel through the production
    `_default_backfill` into the walk's deferred detail (verify round item 6)."""
    tenant = t2_service_env
    cat, owner = catalog
    coll = _coll("skiplist")
    tumbler = str(cat.register(
        owner, "two-chunk note", content_type="knowledge", physical_collection=coll, chunk_count=1,
    ))
    meta = {"catalog_doc_id": tumbler, "chunk_index": 0}
    seed_chunk(tenant, coll, "skiplist old", {**meta, "title": "old"})
    seed_chunk(tenant, coll, "skiplist current", {**meta, "title": "current"})

    _run_ladder(dry_run=False, auto_mode=False)

    out = capsys.readouterr().out
    assert f"The backfill skipped documents: {coll}: chunk_count_mismatch=1." in out
    assert cat.get_manifest(tumbler) == []


def test_a_repeated_piece_note_is_still_manifested(t2_service_env, catalog) -> None:
    """Verify round N2: identical chunk text collapses to ONE T3 row by
    design, so a note registered with 3 pieces whose 2nd repeats the 1st has
    2 stored chunks, at positions 0 and 2. That is not the old-and-current
    shape (more matched than registered); the verb always healed it, with a
    position gap, and must keep doing so."""
    from nexus.catalog.factory import make_catalog_reader
    from nexus.catalog.manifest_backfill import backfill_manifest_for_collection

    tenant = t2_service_env
    cat, owner = catalog
    coll = _coll("repeat")
    tumbler = str(cat.register(
        owner, "repeated piece note", content_type="knowledge",
        physical_collection=coll, chunk_count=3,
    ))
    first = seed_chunk(tenant, coll, "piece one", {"catalog_doc_id": tumbler, "chunk_index": 0})
    third = seed_chunk(tenant, coll, "piece three", {"catalog_doc_id": tumbler, "chunk_index": 2})
    assert _census_totals(tenant, coll)["legacy-unmanifested"] == 2  # control

    result = backfill_manifest_for_collection(
        make_catalog_reader(), hvc.HttpVectorClient(tenant=tenant), coll,
        dry_run=False, only_gapped=True,
    )
    assert result.docs_skipped_chunk_count_mismatch == 0
    assert result.docs_processed == 1 and result.chunks_written == 2
    assert [(r.position, r.chash) for r in cat.get_manifest(tumbler)] == [(0, first), (2, third)]


def test_the_census_prints_the_owner_title_for_legacy_rows(t2_service_env, catalog) -> None:
    """A stranded note's text is hidden from get and search, so the owner
    document's title is the only handle an operator has to re-put it under."""
    tenant = t2_service_env
    cat, owner = catalog
    coll = _coll("titles")
    tumbler, chash = _legacy_note(cat, owner, coll, "The stranded note title", tenant)

    result = CliRunner().invoke(t3, ["census-manifest-less", "--collection", coll])
    assert result.exit_code == 0, result.output
    assert f'{chash}  owner={tumbler} (forward)  title="The stranded note title"' in result.output

    # Only legacy rows carry a title: a superseded row for the same document does not.
    live_doc = str(cat.register(owner, "Titled live doc", content_type="knowledge", physical_collection=coll))
    current = seed_chunk(tenant, coll, "current live body", {"catalog_doc_id": live_doc})
    stale = seed_chunk(tenant, coll, "stale live body", {"catalog_doc_id": live_doc})
    cat.write_manifest(live_doc, [{"chash": current, "position": 0}], collection=coll)
    again = CliRunner().invoke(t3, ["census-manifest-less", "--collection", coll])
    stale_line = next(line for line in again.output.splitlines() if stale in line)
    assert "title=" not in stale_line, stale_line


def test_backfill_manifest_verb_reports_the_chunk_count_mismatch_class(
    t2_service_env, catalog, tmp_path, monkeypatch,
) -> None:
    """Verify round N5: the verb's per-collection line, the `--resume` state
    field and the summary all name the skip class. Deleting any of the three
    from t3.py turns this red."""
    tenant = t2_service_env
    cat, owner = catalog
    coll = _coll("verb")
    tumbler = str(cat.register(
        owner, "verb note", content_type="knowledge", physical_collection=coll, chunk_count=1,
    ))
    meta = {"catalog_doc_id": tumbler, "chunk_index": 0}
    seed_chunk(tenant, coll, "verb old", {**meta, "title": "old"})
    seed_chunk(tenant, coll, "verb current", {**meta, "title": "current"})
    state_file = tmp_path / "backfill_state.json"
    monkeypatch.setenv("NEXUS_BACKFILL_STATE_FILE", str(state_file))

    result = CliRunner().invoke(
        t3, ["backfill-manifest", "-c", coll, "--no-dry-run", "--only-gapped"],
    )
    assert result.exit_code == 0, result.output
    assert "1 skipped: more matched chunks than the document's chunk count" in result.output
    assert "NOT marked done" in result.output and "chunk_count_mismatch=1" in result.output
    assert "1 doc(s) skipped (chunk count mismatch)" in result.output
    state = json.loads(state_file.read_text())
    assert state[coll][0] == "__partial__" and "chunk_count_mismatch=1" in state[coll]
    assert cat.get_manifest(tumbler) == []


def test_a_quarantine_collection_is_skipped_and_counted_in_the_record(t2_service_env, catalog) -> None:
    tenant = t2_service_env
    cat, owner = catalog
    coll = _coll("qcount")
    _legacy_note(cat, owner, coll, "healthy legacy", tenant)
    quarantine = "quarantine-wbfpw41-probe"
    register_collection(tenant, quarantine, lifecycle_state="quarantine")
    seed_chunk(tenant, quarantine, "quarantined body", {"title": "q"})

    _run_ladder(dry_run=False, auto_mode=True)

    assert rdr192_backfill_complete() is True
    with HttpLadderStore() as ledger:
        detail = ledger.completions()[RUNG_NAME].detail
    assert detail.startswith("census: collections=1 quarantine_skipped=1 "), detail
    assert "legacy-unmanifested=0" in detail


def test_an_empty_tenant_records_completion(t2_service_env) -> None:
    """The census succeeds and finds nothing. That must be recordable, or the
    reaper could never run on a tenant that has no legacy data at all."""
    assert rdr192_backfill_complete() is False
    _run_ladder(dry_run=False, auto_mode=True)
    assert rdr192_backfill_complete() is True


def test_an_unreachable_engine_defers_and_records_nothing(t2_service_env, monkeypatch) -> None:
    """A census that cannot run is 'unknown', and unknown is not reached."""

    def _down() -> None:
        raise CensusUnavailable("engine unreachable (test)")

    rung = Rdr192ManifestBackfillRung(census_fn=_down)
    with HttpLadderStore() as ledger:
        report = LadderRunner(LadderRegistry((rung,)), ledger).run()
        assert [r.outcome for r in report.runs] == [RungOutcome.DEFERRED]
        assert RUNG_NAME not in ledger.verified_rungs()
    assert rdr192_backfill_complete() is False


def test_control_the_heal_comes_from_the_backfill_not_from_the_walk(t2_service_env, catalog) -> None:
    """Same seeded state as the heal test, but the backfill seam does nothing.
    The census must keep naming the chunk, the walk must not record, and the
    reaper gate must stay closed: it is the backfill that heals, and the record
    depends on the census reading zero."""

    tenant = t2_service_env
    cat, owner = catalog
    coll = _coll("control")
    tumbler, chash = _legacy_note(cat, owner, coll, "control note", tenant)

    rung = Rdr192ManifestBackfillRung(backfill_fn=lambda collection: 0)
    with HttpLadderStore() as ledger:
        report = LadderRunner(LadderRegistry((rung,)), ledger).run()
        assert [r.outcome for r in report.runs] == [RungOutcome.DEFERRED]
        assert RUNG_NAME not in ledger.verified_rungs()

    assert _census_totals(tenant, coll)["legacy-unmanifested"] == 1
    assert cat.get_manifest(tumbler) == []
    assert not _live(tenant, coll, chash)
    assert rdr192_backfill_complete() is False
