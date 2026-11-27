# SPDX-License-Identifier: AGPL-3.0-or-later
"""A run confined to one document binds a shared chunk to that document (nexus-z1d0l.1.12, RDR-088 amendment (v)).

Split from ``test_witness_runner_records`` (a test file may not pass 1000 lines): same storage-free fixtures,
imported from it. The confinement is the real :class:`DocumentConfinement` under ``confinement_scope``; the catalog
and T3 are the stubs of the neighbouring witness tests."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

import nexus.operators.dispatch as dmod
from nexus.plans import runner
from nexus.plans.document_confinement import DocumentConfinement, confinement_scope
from nexus.plans.repair_stream import RepairSupplement, SupplementItem
from tests.test_6pfgb11_provenance_from_catalog import COLL, X, _position
from tests.test_6pfgb50_runner_chash_reuses_rows import Y, _CountingCatalog, _plain
from tests.test_witness_runner_records import (
    BODY_X, BODY_Y, CHECK_PLAN, MODEL_REPLY, SHARED, Z, _bound_item, _env, _hydrate_check, _plan_match,
    _source, _supplement_hydrate,
)


def _confined_to(tumbler: str, *, collection: str = COLL) -> DocumentConfinement:
    return DocumentConfinement(tumbler, collection, "engine_filter", (X,), manifest_rows=1)


class TestConfinedStepBindsASharedChunkToItsDocument:
    """nexus-z1d0l.1.12 (RDR-088 amendment (v)): a run confined to one document knows its parent, as a tumbler-id
    hydration does. A chunk that document shares with another binds to it when it is one of the chunk's live owners,
    in the chunk's collection (else the confinement's own)."""

    def _bind(self, positions, confinement, *, source=None):
        catalog = _CountingCatalog([_plain(d) for d in ("1.1.1", "1.1.2", "1.1.3")], positions)
        with confinement_scope(confinement):
            _, args = _hydrate_check(catalog, [X], {X: {"content": SHARED}}, source=source or _source([X]))
        return _bound_item(args)

    def test_a_chunk_shared_by_the_confined_document_and_another_binds_to_the_confined_document(self) -> None:
        # the other owner first, so a first-owner guess cannot pass
        item = self._bind([_position(X, "1.1.2"), _position(X, "1.1.1")], _confined_to("1.1.1"))
        assert (item["state"], item["tumbler"], item["chash"], item["collection"]) == ("bound", "1.1.1", X, COLL)

    def test_the_binding_follows_the_confined_document_not_the_owner_order(self) -> None:
        item = self._bind([_position(X, "1.1.1"), _position(X, "1.1.2")], _confined_to("1.1.2"))
        assert (item["state"], item["tumbler"]) == ("bound", "1.1.2")

    def test_a_chunk_the_confined_document_does_not_own_stays_ambiguous(self) -> None:
        # also the dead-owner case: chash_positions answers live rows only, so a tombstoned document's row is absent
        item = self._bind([_position(X, "1.1.2"), _position(X, "1.1.3")], _confined_to("1.1.1"))
        assert (item["state"], item["tumbler"], item["chash"]) == ("ambiguous", None, X)

    def test_an_owner_row_in_another_collection_does_not_count(self) -> None:
        # the confined document owns X only in knowledge__other; the chunk here is in COLL, owned by 1.1.2 and 1.1.3
        item = self._bind(
            [_position(X, "1.1.1", "knowledge__other"), _position(X, "1.1.2"), _position(X, "1.1.3")],
            _confined_to("1.1.1"),
        )
        assert (item["state"], item["tumbler"]) == ("ambiguous", None)

    def test_an_owner_row_naming_no_collection_does_not_count_for_a_chunk_whose_collection_is_known(self) -> None:
        stale = {"chash": X, "doc_id": "1.1.1", "position": 0, "chunk_count": 1}  # a pre-catalog-025 row
        item = self._bind([stale, _position(X, "1.1.2"), _position(X, "1.1.3")], _confined_to("1.1.1"))
        assert (item["state"], item["tumbler"]) == ("ambiguous", None)

    def _bind_alias(self, positions, confinement):
        """The step names the alias ``knowledge`` and its output gives no per-chunk collection: no collection known."""
        catalog = _CountingCatalog([_plain(d) for d in ("1.1.1", "1.1.2", "1.1.3")], positions)
        with confinement_scope(confinement):
            _, args = _hydrate_check(
                catalog, [X], {X: {"content": SHARED}}, source=_source([X], collections=[""]),
                extra={"collections": "knowledge"},
            )
        return _bound_item(args)

    def test_with_no_chunk_collection_a_parent_owning_only_in_another_collection_stays_ambiguous(self) -> None:
        item = self._bind_alias(
            [_position(X, "1.1.1", "knowledge__other"), _position(X, "1.1.2"), _position(X, "1.1.3")],
            _confined_to("1.1.1"),
        )
        assert (item["state"], item["tumbler"], item["collection"]) == ("ambiguous", None, None)

    def test_with_no_chunk_collection_a_parent_owning_in_the_confinement_collection_binds(self) -> None:
        item = self._bind_alias(
            [_position(X, "1.1.2", "knowledge__other"), _position(X, "1.1.1"), _position(X, "1.1.3")],
            _confined_to("1.1.1"),
        )
        assert (item["state"], item["tumbler"]) == ("bound", "1.1.1")

    def test_with_no_chunk_collection_a_parent_row_naming_no_collection_does_not_bind(self) -> None:
        stale = {"chash": X, "doc_id": "1.1.1", "position": 0, "chunk_count": 1}
        item = self._bind_alias([stale, _position(X, "1.1.2"), _position(X, "1.1.3")], _confined_to("1.1.1"))
        assert (item["state"], item["tumbler"]) == ("ambiguous", None)

    def test_an_unconfined_step_with_two_owners_stays_ambiguous(self) -> None:
        item = self._bind([_position(X, "1.1.1"), _position(X, "1.1.2")], None)
        assert (item["state"], item["tumbler"]) == ("ambiguous", None)

    def test_a_confined_step_binds_a_one_owner_chunk_as_before(self) -> None:
        item = self._bind([_position(X, "1.1.1")], _confined_to("1.1.1"))
        assert (item["state"], item["tumbler"]) == ("bound", "1.1.1")

    def test_a_repaired_shared_chunk_binds_to_the_confined_document(self) -> None:
        catalog = _CountingCatalog(
            [_plain("1.1.1"), _plain("1.1.5"), _plain("1.1.6")],
            [_position(X, "1.1.1"), _position(Z, "1.1.6"), _position(Z, "1.1.5")],
        )
        supp = RepairSupplement(items=(SupplementItem(text="repair body", chash=Z, collection=COLL, tumbler="1.1.6"),))
        with confinement_scope(_confined_to("1.1.5")):
            _, args = _supplement_hydrate(catalog, {X: {"content": "plan body"}}, [X], supp)
        rec = next(r for r in args["_evidence_records"] if r.text == "repair body")
        assert (rec.tumbler, rec.chash) == ("1.1.5", Z)

    def _repair(self, positions, confinement, *, collection: str = COLL):
        catalog = _CountingCatalog([_plain(d) for d in ("1.1.1", "1.1.5", "1.1.6")], [_position(X, "1.1.1"), *positions])
        supp = RepairSupplement(items=(SupplementItem(text="repair body", chash=Z, collection=collection, tumbler="1.1.6"),))
        with confinement_scope(confinement):
            _, args = _supplement_hydrate(catalog, {X: {"content": "plan body"}}, [X], supp)
        return next(r for r in args["_evidence_records"] if r.text == "repair body")

    def test_a_repaired_shared_chunk_the_confined_document_does_not_own_stays_ambiguous(self) -> None:
        rec = self._repair([_position(Z, "1.1.5"), _position(Z, "1.1.6")], _confined_to("1.1.1"))
        assert (rec.tumbler, rec.chash) == (None, Z)

    def test_a_repaired_chunk_with_no_collection_binds_the_parent_only_on_the_confinement_collection(self) -> None:
        other_only = [_position(Z, "1.1.5", "knowledge__other"), _position(Z, "1.1.6")]
        assert self._repair(other_only, _confined_to("1.1.5"), collection="").tumbler is None
        own = [_position(Z, "1.1.5"), _position(Z, "1.1.6", "knowledge__other")]
        assert self._repair(own, _confined_to("1.1.5"), collection="").tumbler == "1.1.5"


@pytest.mark.asyncio
async def test_a_confined_plan_run_binds_a_shared_chunk_to_the_confined_document_end_to_end() -> None:
    # plan_run(document_confinement=...) -> dispatcher -> hydration: X is shared by 1.1.1 and 1.1.2, Y is 1.1.2's only
    catalog = _CountingCatalog(
        [_plain("1.1.1"), _plain("1.1.2")], [_position(X, "1.1.1"), _position(X, "1.1.2"), _position(Y, "1.1.2")],
    )

    async def fake_dispatch(prompt, schema, **kw):
        return json.loads(json.dumps(MODEL_REPLY))

    confinement = DocumentConfinement("1.1.2", COLL, "engine_filter", (X, Y), manifest_rows=2)
    with _env(catalog, {X: {"content": BODY_X}, Y: {"content": BODY_Y}}), patch.object(dmod, "claude_dispatch", fake_dispatch):
        result = await runner.plan_run(
            _plan_match(CHECK_PLAN), {}, bundle_operators=False, document_confinement=confinement,
        )
    assert [(i["item_id"], i["state"], i["tumbler"], i["chash"]) for i in result.steps[-1]["evidence"]] == [
        ("e1", "bound", "1.1.2", X), ("e2", "bound", "1.1.2", Y),
    ]
