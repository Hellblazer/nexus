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

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from nexus.upgrade_ladder.rungs.rdr192_manifest_backfill import _default_census

_TABLE = (
    Path(__file__).resolve().parents[2]
    / "service" / "src" / "test" / "resources" / "parity" / "rdr192_empty_tenant_shapes.json"
)
_SHAPES: list[dict] = json.loads(_TABLE.read_text())["shapes"]
_MODEL = "bge-base-en-v15-768"


def _lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _psql(sql: str) -> None:
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


def _register_collection(tenant: str, collection: str, state: str) -> None:
    _psql(
        "INSERT INTO nexus.catalog_collections "
        "(tenant_id, name, content_type, owner_id, embedding_model, lifecycle_state) "
        f"VALUES ({_lit(tenant)}, {_lit(collection)}, 'knowledge', 'test-seed', {_lit(_MODEL)}, "
        f"{_lit(state)}) ON CONFLICT DO NOTHING"
    )


def _seed_chunk(tenant: str, collection: str, text: str, metadata: dict) -> str:
    """One ``nexus.chunks`` row by direct SQL (the catalog row must already exist); returns its chash."""
    chash = hashlib.sha256(text.encode()).hexdigest()
    vec = "[" + ",".join(["0"] * 768) + "]"
    _psql(
        "INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, embedding_768, metadata) "
        f"VALUES ({_lit(tenant)}, {_lit(collection)}, decode({_lit(chash)}, 'hex'), {_lit(text)}, "
        f"{_lit(vec)}::nexus.vector, {_lit(json.dumps({'chunk_text_hash': chash, **metadata}))}::jsonb) "
        "ON CONFLICT DO NOTHING"
    )
    return chash


def _seed(shape: dict, tenant: str, cat, owner) -> None:
    documents = shape["documents"]
    for index, spec in enumerate(shape["collections"]):
        prefix = "quarantine-" if spec["state"] == "quarantine" else ""
        collection = f"{prefix}knowledge__wbfpw76{index}__{_MODEL}__v1"
        _register_collection(tenant, collection, spec["state"])
        for n in range(spec["chunks"]):
            if documents == "none":
                _seed_chunk(tenant, collection, f"{collection} chunk {n}", {})
                continue
            title = f"{shape['name']} note {index}.{n}"
            tumbler = str(cat.register(
                owner, title, content_type="knowledge", physical_collection=collection,
                chunk_count=1 if documents == "owned-with-manifest" else 0,
            ))
            chash = _seed_chunk(
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
        "content-owned-with-manifest",
    }
    assert len(names) == len(_SHAPES), "shape names are tenant-name inputs and must be unique"
    for shape in _SHAPES:
        assert not shape["engine_empty"] or shape["client_census_clean"], (
            f"{shape['name']}: the engine would call it empty where the client census would not converge"
        )


@pytest.mark.parametrize("shape", _SHAPES, ids=[s["name"] for s in _SHAPES])
def test_the_client_census_reads_each_shape_as_the_table_says(shape, t2_service_env, catalog) -> None:
    cat, owner = catalog
    _seed(shape, t2_service_env, cat, owner)

    reading = _default_census()  # a CensusUnavailable here is "does not converge" too, and fails the test

    assert reading.clean is shape["client_census_clean"], (shape["name"], reading.describe_residual())
