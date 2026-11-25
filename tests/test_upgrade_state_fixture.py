# SPDX-License-Identifier: AGPL-3.0-or-later
"""The upgrade-state fixture (``tests/_upgrade_state.py``) builds the state it names and puts the CHECK back.

RDR-229 P3.1 (nexus-p2m1x.17). Pinned here: the seeded document really is name-set / id-NULL / no registry row
(the fixture's own non-vacuity check cannot pass on a row the CHECK would have refused), the CHECK is enforced
again afterwards and is ``NOT VALID`` (the state an upgraded install holds), and a changeset that does not exist
or has no text raises rather than replaying nothing.
"""
from __future__ import annotations

import pytest

from tests._catalog_fixture_ops import ActiveCatalog, _run_psql
from tests._chunk_seed import _lit, _psql_superuser, _pg_state, ambient_tenant
from tests._upgrade_state import (
    CATALOG_DOCUMENTS,
    assert_pre_p31_document,
    changeset_sql,
    seed_pre_p31_document,
)

pytestmark = pytest.mark.t2_tenant

_ORPHAN = "quarantine-code__1-41__bge-base-en-v15-768__v1"
_CHECK = "catalog_documents_physical_collection_id_check"


def _check_state() -> str:
    """``f`` when the constraint is NOT VALID (convalidated false), ``t`` when validated, empty when absent."""
    return _psql_superuser(
        _pg_state(),
        "SELECT convalidated FROM pg_constraint WHERE conname = " + _lit(_CHECK)
        + " AND conrelid = 'nexus.catalog_documents'::regclass",
    )


def test_seed_builds_the_pre_p31_document_and_restores_the_check_not_valid():
    owner = ActiveCatalog().register_owner("upgrade-state-seed", "curator", repo_hash="")
    assert _check_state() == "f", "the CHECK is not NOT VALID before the seed (the changeset did not run)"

    tumbler = seed_pre_p31_document(owner, title="pre-P3.1 doc", physical_collection=_ORPHAN)

    assert tumbler.startswith(f"{owner}.")
    assert_pre_p31_document(tumbler, _ORPHAN)
    assert _check_state() == "f", "the seed left the CHECK lifted or validated"
    # The restored CHECK is enforced on a new row again: the same insert, without a lift, is refused.
    with pytest.raises(RuntimeError, match=_CHECK):
        _run_psql(
            "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title, physical_collection) VALUES "
            f"({_lit(ambient_tenant())}, {_lit(tumbler + '9')}, 'refused', {_lit(_ORPHAN)});"
        )


def test_the_seeded_ordinals_are_distinct_per_owner():
    owner = ActiveCatalog().register_owner("upgrade-state-seed", "curator", repo_hash="")
    first = seed_pre_p31_document(owner, title="a", physical_collection=_ORPHAN)
    second = seed_pre_p31_document(owner, title="b", physical_collection=_ORPHAN)
    assert first != second


def test_changeset_sql_selects_the_part_asked_for_and_never_replays_nothing():
    rollback = changeset_sql(CATALOG_DOCUMENTS, rollback=True)
    forward = changeset_sql(CATALOG_DOCUMENTS, rollback=False)
    assert f"DROP CONSTRAINT IF EXISTS {_CHECK}" in rollback and "ADD CONSTRAINT" not in rollback
    assert f"ADD CONSTRAINT {_CHECK}" in forward and "NOT VALID" in forward and "DROP CONSTRAINT" not in forward
    with pytest.raises(LookupError):
        changeset_sql("vectors-068-999", rollback=False)
