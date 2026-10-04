# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-wbfpw.76: the client half of the empty-tenant parity pin.

The engine reads a tenant as empty with ``ReaperRepository.holdsNothing`` (behind
``Rdr192BackfillGate``, nexus-wbfpw.73): no row in ``nexus.chunks``, quarantine siblings
included. The client rung converges a tenant with
``rdr192_manifest_backfill._default_census`` plus ``_cross_check_empty_listing``. The engine
must never call a tenant empty where the client's census would not converge.

The engine probe cannot be reached per tenant from here: ``reaper.last_pass.tenants_empty`` is a
count over every tenant a pass visits, and the pass runs on its own schedule. So the two sides are
pinned to ONE table, ``service/src/test/resources/parity/rdr192_empty_tenant_shapes.json``:

* ``Rdr192EmptyTenantParityIntegrationTest`` (Java) seeds each shape into real Postgres and asserts
  the table's ``engine_empty`` against the real ``holdsNothing``;
* this file seeds the same shapes through the engine substrate and asserts the table's
  ``client_census_clean`` against the real ``_default_census``, and checks the parity invariant
  ``engine_empty implies client_census_clean`` over the table itself.

Each shape runs under its own minted tenant. Not integration-marked: the substrate provisions
itself.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexus.upgrade_ladder.rungs.rdr192_manifest_backfill import _default_census
from tests.upgrade._substrate_sql import MODEL, lit, psql, register_collection, seed_chunk

_TABLE = (
    Path(__file__).resolve().parents[2]
    / "service" / "src" / "test" / "resources" / "parity" / "rdr192_empty_tenant_shapes.json"
)
_SHAPES: list[dict] = json.loads(_TABLE.read_text())["shapes"]


def _seed(shape: dict, tenant: str, cat, owner) -> None:
    documents = shape["documents"]
    for index, spec in enumerate(shape["collections"]):
        prefix = "quarantine-" if spec["state"] == "quarantine" else ""
        collection = f"{prefix}knowledge__wbfpw76{index}__{MODEL}__v1"
        register_collection(tenant, collection, spec["state"])
        for n in range(spec["chunks"]):
            if documents == "none":
                seed_chunk(tenant, collection, f"{collection} chunk {n}", {})
                continue
            title = f"{shape['name']} note {index}.{n}"
            tumbler = str(cat.register(
                owner, title, content_type="knowledge", physical_collection=collection,
                chunk_count=1 if documents == "owned-with-manifest" else 0,
            ))
            chash = seed_chunk(
                tenant, collection, f"{title} body",
                {"catalog_doc_id": tumbler, "title": title, "chunk_index": 0, "chunk_count": 1},
            )
            if documents == "owned-with-manifest":
                cat.write_manifest(tumbler, [{"chash": chash, "position": 0}], collection=collection)


@pytest.fixture
def catalog(t2_service_env):
    from tests._catalog_fixture_ops import ActiveCatalog  # noqa: PLC0415

    cat = ActiveCatalog()
    return cat, cat.register_owner("wbfpw76", "curator")


def test_the_table_holds_the_parity_invariant_and_the_shapes_the_bead_names() -> None:
    names = {s["name"] for s in _SHAPES}
    assert names >= {
        "empty", "chunk-only", "quarantine-only", "registered-collection-no-chunks",
        "content-owned-with-manifest", "legacy-unmanifested-note",
    }
    assert len(names) == len(_SHAPES), "shape names are tenant-name inputs and must be unique"
    for shape in _SHAPES:
        assert not shape["engine_empty"] or shape["client_census_clean"], (
            f"{shape['name']}: the engine would call it empty where the client census would not converge"
        )
    # The invariant is vacuous over a table that never says "not clean" or never says "empty".
    assert any(not s["client_census_clean"] for s in _SHAPES), "no shape the client census refuses"
    assert {s["engine_empty"] for s in _SHAPES} == {True, False}, "both engine verdicts must occur"


@pytest.mark.parametrize("shape", _SHAPES, ids=[s["name"] for s in _SHAPES])
def test_the_client_census_reads_each_shape_as_the_table_says(shape, t2_service_env, catalog) -> None:
    cat, owner = catalog
    _seed(shape, t2_service_env, cat, owner)

    # The seeded state, not the table's claim about it: the engine probe's verdict follows from
    # whether the tenant holds a nexus.chunks row, and an engine "empty" must be a clean census.
    stored = int(psql(f"SELECT count(*) FROM nexus.chunks WHERE tenant_id = {lit(t2_service_env)}").strip())
    assert shape["engine_empty"] == (stored == 0), (shape["name"], stored)

    reading = _default_census()  # a CensusUnavailable here is "does not converge" too, and fails the test

    assert reading.clean is shape["client_census_clean"], (shape["name"], reading.describe_residual())
    assert not shape["engine_empty"] or reading.clean, shape["name"]

    # What the census saw, derived from the table's collection spec, so a census that read nothing
    # cannot pass as clean. list_collections is driven by collection_vector_stats: a registered
    # collection holding no chunks is not listed, so that shape reaches the client as an empty
    # listing (the _cross_check_empty_listing branch) and reads zero collections, like "empty".
    held = [c for c in shape["collections"] if c["chunks"] > 0]
    census = [c for c in held if c["state"] != "quarantine"]
    quarantine = [c for c in held if c["state"] == "quarantine"]
    assert len(reading.collections) == len(census), (shape["name"], reading.summary())
    assert sum(c.scope_chunks for c in reading.collections) == sum(c["chunks"] for c in census), (
        shape["name"], reading.summary(),
    )
    assert reading.quarantine_skipped == len(quarantine), (shape["name"], reading.summary())
