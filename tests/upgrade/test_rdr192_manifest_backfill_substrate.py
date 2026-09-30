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

import hashlib
import json
import subprocess
from pathlib import Path

import click
import pytest

import nexus.db.http_vector_client as hvc
from nexus.commands.upgrade import _run_ladder
from nexus.upgrade_ladder.http_store import HttpLadderStore
from nexus.upgrade_ladder.registry import LadderRegistry
from nexus.upgrade_ladder.rungs.rdr192_manifest_backfill import (
    RUNG_NAME,
    BackfillIncompleteError,
    CensusUnavailable,
    Rdr192ManifestBackfillRung,
    rdr192_backfill_complete,
    require_rdr192_backfill_complete,
)
from nexus.upgrade_ladder.runner import LadderRunner, RungOutcome

_MODEL = "bge-base-en-v15-768"


def _coll(tag: str) -> str:
    return f"knowledge__wbfpw41{tag}__{_MODEL}__v1"


def _chash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _psql(sql: str) -> str:
    from tests._engine_substrate import ensure_engine  # noqa: PLC0415 — substrate boots lazily

    state = ensure_engine()
    proc = subprocess.run(
        [
            str(Path(state["pg_bin"]) / "psql"), "-h", "127.0.0.1", "-p", str(state["pg_port"]),
            "-U", state["pg_user"], "-d", state["pg_dbname"],
            "-v", "ON_ERROR_STOP=1", "-At", "-c", sql,
        ],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"psql failed: {proc.stderr}\nSQL: {sql}"
    return proc.stdout


def _register_collection(tenant: str, collection: str) -> None:
    _psql(
        "INSERT INTO nexus.catalog_collections "
        "(tenant_id, name, content_type, owner_id, embedding_model, lifecycle_state) "
        f"VALUES ({_lit(tenant)}, {_lit(collection)}, 'knowledge', 'test-seed', {_lit(_MODEL)}, 'live') "
        "ON CONFLICT DO NOTHING"
    )


def _seed_chunk(tenant: str, collection: str, text: str, metadata: dict) -> str:
    """Insert one ``nexus.chunks`` row by direct SQL and return its chash."""
    chash = _chash(text)
    _register_collection(tenant, collection)
    vec = "[" + ",".join(["0"] * 768) + "]"
    _psql(
        "INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, embedding_768, metadata) "
        f"VALUES ({_lit(tenant)}, {_lit(collection)}, decode({_lit(chash)}, 'hex'), {_lit(text)}, "
        f"{_lit(vec)}::nexus.vector, {_lit(json.dumps({'chunk_text_hash': chash, **metadata}))}::jsonb) "
        "ON CONFLICT DO NOTHING"
    )
    return chash


def _stored(tenant: str, collection: str) -> set[str]:
    out = _psql(
        "SELECT encode(chash, 'hex') FROM nexus.chunks "
        f"WHERE tenant_id = {_lit(tenant)} AND collection = {_lit(collection)}"
    )
    return {line for line in out.splitlines() if line}


def _census_totals(tenant: str, collection: str) -> dict[str, int]:
    import nexus.db.http_vector_client as hvc  # noqa: PLC0415

    return hvc.HttpVectorClient(tenant=tenant).manifest_less_census(collection, limit=1)["totals"]


def _live(tenant: str, collection: str, chash: str) -> bool:
    import nexus.db.http_vector_client as hvc  # noqa: PLC0415

    col = hvc.HttpVectorClient(tenant=tenant).get_collection(collection)
    return chash in col.get(ids=[chash], include=["metadatas"])["ids"]


def _legacy_note(cat, owner, collection: str, title: str, tenant: str, *, doc_collection: str | None = None):
    """A note-shaped catalog document plus its chunk, with NO manifest row."""
    tumbler = str(cat.register(
        owner, title, content_type="knowledge",
        physical_collection=doc_collection or collection,
    ))
    chash = _seed_chunk(
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


def test_second_walk_is_a_no_op_and_sends_no_census_request(
    t2_service_env, catalog, monkeypatch,
) -> None:
    """`nx upgrade --auto` walks the ladder at every SessionStart, so once the
    completion is on file the walk must not census the tenant again."""

    tenant = t2_service_env
    cat, owner = catalog
    coll = _coll("idem")
    tumbler, chash = _legacy_note(cat, owner, coll, "idempotent note", tenant)

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
    assert "1 legacy-unmanifested" in out
    assert cat.get_manifest(tumbler) == []
    assert rdr192_backfill_complete() is False


def test_other_buckets_are_left_alone_and_nothing_is_deleted(t2_service_env, catalog) -> None:
    """superseded, dead-owner and no-owner rows are the REAPER's input. The
    rung neither heals them nor blocks on them, and it never deletes a chunk."""
    tenant = t2_service_env
    cat, owner = catalog
    coll = _coll("buckets")

    _legacy_tumbler, legacy = _legacy_note(cat, owner, coll, "legacy in mixed collection", tenant)

    no_owner = _seed_chunk(tenant, coll, "no owner body", {"title": "no-owner"})

    dead_doc = str(cat.register(owner, "dead doc", content_type="knowledge", physical_collection=coll))
    dead_owner = _seed_chunk(tenant, coll, "dead owner body", {"catalog_doc_id": dead_doc})
    cat.delete_document(dead_doc)

    live_doc = str(cat.register(owner, "live doc", content_type="knowledge", physical_collection=coll))
    current = _seed_chunk(tenant, coll, "current body", {"catalog_doc_id": live_doc})
    superseded = _seed_chunk(tenant, coll, "superseded body", {"catalog_doc_id": live_doc})
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


def test_an_unhealable_residual_refuses_the_record_and_resumes_when_fixed(
    t2_service_env, catalog,
) -> None:
    """A legacy chunk whose owner is registered under ANOTHER collection is
    invisible to the collection-scoped backfill. The census keeps naming it, so
    verify() refuses, the walk fails visibly with the remedy, no completion is
    recorded and the reaper gate stays closed. Once the operator manifests it,
    the next walk records."""
    tenant = t2_service_env
    cat, owner = catalog
    coll = _coll("stuck")
    elsewhere = _coll("elsewhere")
    _register_collection(tenant, elsewhere)
    tumbler, chash = _legacy_note(cat, owner, coll, "stranded note", tenant, doc_collection=elsewhere)

    with pytest.raises(click.ClickException) as excinfo:
        _run_ladder(dry_run=False, auto_mode=False)
    message = excinfo.value.format_message()
    assert RUNG_NAME in message and coll in message
    assert "nx t3 census-manifest-less" in message

    assert _census_totals(tenant, coll)["legacy-unmanifested"] == 1
    assert chash in _stored(tenant, coll), "a residual is reported, never deleted"
    assert rdr192_backfill_complete() is False
    with pytest.raises(BackfillIncompleteError):
        require_rdr192_backfill_complete()

    cat.write_manifest(tumbler, [{"chash": chash, "position": 0}], collection=coll)

    _run_ladder(dry_run=False, auto_mode=False)
    assert _census_totals(tenant, coll)["legacy-unmanifested"] == 0
    assert rdr192_backfill_complete() is True


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
        assert [r.outcome for r in report.runs] == [RungOutcome.VERIFY_FAILED]
        assert RUNG_NAME not in ledger.verified_rungs()

    assert _census_totals(tenant, coll)["legacy-unmanifested"] == 1
    assert cat.get_manifest(tumbler) == []
    assert not _live(tenant, coll, chash)
    assert rdr192_backfill_complete() is False
