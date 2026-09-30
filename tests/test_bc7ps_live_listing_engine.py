"""nexus-bc7ps, the cross-boundary pin: against the real engine, a quarantine
collection produced by the real prune path is in the full listing and absent
from the routing listing on BOTH clients, and taxonomy discovery never
enumerates it.

The quarantine row is made the way production makes it: chunks with no
manifest row (inserted with substrate SQL, since the engine refuses the
ownerless route) are orphans, ``_prune_deleted_files`` moves them
through ``gc_quarantine_orphans`` into ``quarantine-<name>``, which the engine
registers with ``lifecycle_state = 'quarantine'`` (hygiene-005). No fixture
inserts the row by hand, so the pin holds against the registration path the
defect came from.
"""
from __future__ import annotations

import hashlib

import pytest

import nexus.db.http_vector_client as hvc
from nexus.catalog.chunk_quarantine import quarantine_collection_name
from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.commands.taxonomy_cmd import _enumerate_discoverable_collections
from nexus.indexer import _prune_deleted_files
from tests._catalog_fixture_ops import ActiveCatalog
from tests._chunk_seed import seed_chunks_direct

# Deliberately in the default suite, not integration-marked: it is the one pin
# on the class this bead closed, it needs only the engine substrate every
# `pytest -n auto` already boots, and it runs in about six seconds.


def _seed_orphans(cat, db, coll_name: str, owner: str, n_live: int, n_orphan: int) -> None:
    ids, docs, metas = [], [], []
    for i in range(n_live + n_orphan):
        chash = hashlib.sha256(f"{coll_name}:{i}".encode()).hexdigest()
        ids.append(chash)
        docs.append(f"def bc7ps_probe_{i}(): return {i}\n")
        metas.append({"chunk_text_hash": chash, "title": f"bc7ps_probe_{i}.py:1-1"})
    # Substrate SQL: the orphans are this test's subject, and the engine refuses
    # an ownerless upsert-chunks write from RDR-223 Phase 3 on.
    seed_chunks_direct(coll_name, ids, docs, metas)
    for i in range(n_live):
        tumbler = str(cat.register(
            owner, f"bc7ps_probe_{i}.py", content_type="code",
            file_path=f"/tmp/{coll_name}/bc7ps_probe_{i}.py",
            physical_collection=coll_name, chunk_count=1,
        ))
        cat.write_manifest(tumbler, [{"chash": ids[i], "position": 0}], collection=coll_name)


def test_quarantine_collection_is_inventory_but_never_routing(t2_service_env) -> None:
    tenant = t2_service_env
    cat = ActiveCatalog()
    db = hvc.HttpVectorClient(tenant=tenant)
    coll = "code__bc7ps-owner__bge-base-en-v15-768__v1"
    owner = cat.register_owner("bc7ps-owner", "curator")
    # 6 live and 6 orphans: both the origin and the quarantine sibling clear
    # discovery's >=5-chunk floor, so the sibling's absence below is the filter,
    # not the floor.
    _seed_orphans(cat, db, coll, owner, n_live=6, n_orphan=6)
    _prune_deleted_files(coll, "docs__bc7ps-unused", db, catalog=cat)
    qname = quarantine_collection_name(coll)

    # Non-vacuity: the sibling exists, holds chunks, and is registered non-live.
    # A quarantine sibling never has manifests (RDR-192 Step 5 amendment), so
    # its live count is 0; `stored_count` is the physical-inventory figure
    # that proves the 6 orphans actually landed there.
    full = {r["name"]: r for r in db.list_collections()}
    assert qname in full and full[qname]["stored_count"] == 6, full.get(qname)
    assert full[qname]["count"] == 0, full.get(qname)
    assert full[qname]["lifecycle_state"] == "quarantine"
    assert full[coll]["lifecycle_state"] == "live"

    # Vector side: the routing view excludes it, an explicit state selects it.
    live_names = {r["name"] for r in db.list_live_collections()}
    assert coll in live_names and qname not in live_names
    assert {r["name"] for r in db.list_collections(lifecycle_state="quarantine")} >= {qname}

    # Catalog side: same three shapes on the registry route.
    catalog = HttpCatalogClient(tenant=tenant)
    reg_full = {r["name"] for r in catalog.list_collections()}
    assert {coll, qname} <= reg_full
    reg_live = {r["name"] for r in catalog.list_collections("live")}
    assert coll in reg_live and qname not in reg_live
    assert qname in {r["name"] for r in catalog.list_collections("quarantine")}

    # An unknown filter is refused by name, never an empty list.
    with pytest.raises(hvc.VectorServiceError, match="lifecycle_state"):
        db.collection_stats("quarantined")

    # Consumer 3, the one that died on this name: discovery never sees it.
    discoverable = _enumerate_discoverable_collections(db, exclude=[])
    assert coll in discoverable and qname not in discoverable
