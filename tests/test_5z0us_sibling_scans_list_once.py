# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-5z0us siblings: per-collection scans list the tenant once.

HttpVectorClient.get_collection re-lists every collection (a /v1/vectors/stats
pass, 3.5s measured on a 111-collection tenant) to check existence. The
name-vs-embed-dim probe paid that per collection (fixed in 62536f27c); the
review found the same loop in other doctor scans, the T3-orphan classifier
and `nx collection backfill-hash --all`. The consequence measured here is
the number of tenant-wide listings each check makes.
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

import nexus.db.http_vector_client as hvc

pytestmark = pytest.mark.integration

_N = 5


def _seed(client) -> list[str]:
    from nexus.catalog.factory import make_catalog_writer

    writer = make_catalog_writer(priority="interactive")
    owner = writer.register_owner("knowledge", "curator")
    names = [f"knowledge__z0us-sib-{i}__bge-base-en-v15-768__v1" for i in range(_N)]
    for i, name in enumerate(names):
        text = f"z0us sibling collection {i} " + "x" * 200
        chash = hashlib.sha256(text.encode()).hexdigest()
        client.upsert_chunks_with_embeddings(
            name, ids=[chash], documents=[text], embeddings=[],
            metadatas=[{"title": text[:40], "indexed_at": datetime.now(UTC).isoformat()}],
        )
        doc = str(writer.register(
            owner=owner, title=text[:40], content_type="knowledge", physical_collection=name,
        ))
        writer.write_manifest(doc, [{"chash": chash, "position": 0}], collection=name)
    return names


def _count_listings(client, fn):
    real_get = hvc._get
    calls: list[str] = []

    def _counting_get(path, *args, **kwargs):
        if str(path).startswith("/v1/vectors/stats"):
            calls.append(str(path))
        return real_get(path, *args, **kwargs)

    with patch.object(hvc, "_get", _counting_get), patch("nexus.db.make_t3", lambda: client):
        result = fn()
    return result, calls


@pytest.mark.parametrize("check", ["chunk_size", "chunk_text_dedup"])
def test_doctor_scans_list_the_tenant_once(t2_service_env, check):
    from nexus.commands.catalog_cmds import doctor

    client = hvc.HttpVectorClient(tenant=t2_service_env)
    names = _seed(client)
    fn = {"chunk_size": doctor._run_chunk_size_distribution,
          "chunk_text_dedup": doctor._run_chunk_text_dedup}[check]
    result, calls = _count_listings(client, fn)
    seen = result.get("tables") or result.get("within") or {}
    assert set(names) <= set(seen), sorted(seen)
    assert len(calls) == 1, f"{len(calls)} tenant-wide listings for {len(seen)} collections"


def test_orphan_classifier_lists_the_tenant_once(t2_service_env):
    from nexus.catalog.factory import make_catalog_reader
    from nexus.commands.catalog_cmds.t3_orphans import classify_t3_orphan_collections

    client = hvc.HttpVectorClient(tenant=t2_service_env)
    orphan = "knowledge__z0us-sib-orphan__bge-base-en-v15-768__v1"
    _seed(client)
    client.upsert_chunks_with_embeddings(
        orphan, ids=["b" * 64], documents=["z0us unowned chunk"], embeddings=[],
        metadatas=[{"title": "unowned", "indexed_at": datetime.now(UTC).isoformat()}],
    )
    reader = make_catalog_reader()
    rows, calls = _count_listings(client, lambda: classify_t3_orphan_collections(reader, client))

    assert [r["name"] for r in rows if r.get("class") == "orphan"] == [orphan], rows
    assert len(calls) == 1, f"{len(calls)} tenant-wide listings"
