# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-oq0tk: T3-backed shape-parity leg for resolve_chash / resolve_span.

``tests/catalog/test_shape_parity_tripwire.py`` (nexus-8y1tm) excludes
``Catalog.resolve_chash`` and ``Catalog.resolve_span`` from its main
REGISTRY because both need a live T3 ``ClientAPI`` — ``catalog_spans.py``
queries a real vector collection (``col.get(where={"chunk_text_hash": ...})``),
which the catalog-metadata-only harness (SQLite + a fake HTTP server) does
not stand up. This module is the follow-up leg: it wires a real
``chromadb.EphemeralClient`` alongside the same paired-assertion pattern
(reusing ``shape()`` from the tripwire module rather than duplicating it),
so these two methods get real shape-parity coverage instead of shipping
their return-shape drift silently.

Two scenarios:

- ``resolve_span``: a chunk seeded directly into an EphemeralClient
  collection (mirrors ``resolve_span_in_t3``'s ``col.get(where=...)``
  query) vs ``HttpCatalogClient.resolve_span`` against
  ``FakeCatalogHandler``'s ``/resolve_span`` route.
- ``resolve_chash``: a chunk seeded into T3 *and* registered in a real
  ``ChashIndex`` (T2), plus a real ``Catalog`` doc + manifest referencing
  the same chash (the "real Catalog scenario" a resolve_chash caller
  would actually see) vs ``HttpCatalogClient.resolve_chash`` against the
  ``/resolve_chash`` route.

Both fixture chashes are chosen to land on ``FakeCatalogHandler``'s
non-empty, non-404 response branches — see the literals' comments below
for exactly which server-side branch each one drives.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator

import chromadb
import pytest

from nexus.catalog.catalog import Catalog
from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.db.t2.chash_index import ChashIndex
from tests.catalog.test_http_catalog_client import start_fake_server
from tests.catalog.test_shape_parity_tripwire import shape
from tests.conftest import make_vector_test_client

# ── fixture literals ─────────────────────────────────────────────────────────

# 64-hex chash for the resolve_span scenario. FakeCatalogHandler's /resolve_span
# route special-cases span_chash == "deadbeef" * 4 (the [:32] prefix the client
# sends) AND collection == _SPAN_COLLECTION to return a non-empty metadata dict
# ({"lang": "en"}) — every OTHER chash hits the generic branch, which returns
# metadata={} (empty dict), a shape MISMATCH against a genuinely-populated local
# chroma metadata dict. This literal must stay in lockstep with that branch.
_SPAN_CHASH = "deadbeef" * 8
_SPAN_COLLECTION = "knowledge__o__bge-768__v1"

# 64-hex chash for the resolve_chash scenario. Any value other than
# FakeCatalogHandler's 404 sentinel ("00000000" * 4, the [:32] prefix) hits its
# generic /resolve_chash 200 branch, so this literal is not otherwise special.
_CHASH_GLOBAL = "c" * 64
_CHASH_COLLECTION = "code__t3leg-chash__v1"


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def env(tmp_path: Path) -> Iterator[tuple[Catalog, "chromadb.ClientAPI", ChashIndex]]:
    """Real Catalog (SQLite) + real EphemeralClient T3 + real ChashIndex (T2),
    seeded with a registered doc+manifest (resolve_chash's "real usage" shape)
    plus the two raw T3 chunks each method under test actually queries."""
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    cat = Catalog(catalog_dir=catalog_dir, db_path=tmp_path / "catalog.sqlite")

    # chromadb.EphemeralClient instances share an in-memory backend across the
    # test session — collections leak between tests in the same process.
    # Clear on entry (same pattern as test_chash_reconcile.py).
    client = make_vector_test_client()
    for c in list(client.list_collections()):
        name = c if isinstance(c, str) else c.name
        try:
            client.delete_collection(name)
        except Exception:  # noqa: BLE001 — best-effort cleanup of leaked ephemeral state
            pass

    chash_idx = ChashIndex(tmp_path / "t2-chash.db")

    # A real registered doc + manifest referencing the resolve_chash target —
    # the scenario a production resolve_chash caller actually sees, even
    # though the method itself resolves via T2/T3 directly, not the manifest.
    owner = cat.register_owner("nexus-t3leg", "repo", repo_hash="t3leg-hash")
    doc = cat.register(
        owner, "t3leg_doc.py", content_type="code",
        file_path="src/t3leg_doc.py", physical_collection=_CHASH_COLLECTION,
    )
    cat.write_manifest(str(doc), [{"chash": _CHASH_GLOBAL, "position": 0}])

    chash_col = client.get_or_create_collection(_CHASH_COLLECTION)
    chash_col.add(
        ids=["t3leg-chash-chunk"],
        documents=["chunk body text"],
        metadatas=[{"chunk_text_hash": _CHASH_GLOBAL, "source": "test"}],
    )
    chash_idx.upsert(chash=_CHASH_GLOBAL, collection=_CHASH_COLLECTION)

    span_col = client.get_or_create_collection(_SPAN_COLLECTION)
    span_col.add(
        ids=["t3leg-span-chunk"],
        documents=["hello span world"],
        metadatas=[{"chunk_text_hash": _SPAN_CHASH, "lang": "en"}],
    )

    yield cat, client, chash_idx

    chash_idx.close()
    cat.close()


@pytest.fixture()
def http_client() -> Iterator[HttpCatalogClient]:
    server, url = start_fake_server()
    try:
        with HttpCatalogClient(base_url=url, _token="t3-leg-tok") as c:
            yield c
    finally:
        server.shutdown()


# ── parity assertions ────────────────────────────────────────────────────────


def test_resolve_span_t3_parity(
    env: tuple[Catalog, "chromadb.ClientAPI", ChashIndex],
    http_client: HttpCatalogClient,
) -> None:
    cat, t3, _chash_idx = env

    local_result = cat.resolve_span(f"chash:{_SPAN_CHASH}", _SPAN_COLLECTION, t3)
    http_result = http_client.resolve_span(f"chash:{_SPAN_CHASH}", _SPAN_COLLECTION)

    assert local_result, "local resolve_span returned vacuously empty — strengthen the seed"
    assert http_result, "http resolve_span returned vacuously empty — strengthen the fake route"

    local_shape = shape(local_result)
    http_shape = shape(http_result)
    assert local_shape == http_shape, (
        f"resolve_span: shape mismatch — local={local_shape!r} http={http_shape!r} "
        f"(local value: {local_result!r}, http value: {http_result!r})"
    )


def test_resolve_chash_t3_parity(
    env: tuple[Catalog, "chromadb.ClientAPI", ChashIndex],
    http_client: HttpCatalogClient,
) -> None:
    cat, t3, chash_idx = env

    local_result = cat.resolve_chash(_CHASH_GLOBAL, t3, chash_idx)
    http_result = http_client.resolve_chash(f"chash:{_CHASH_GLOBAL}")

    assert local_result, "local resolve_chash returned vacuously empty — strengthen the seed"
    assert http_result, "http resolve_chash returned vacuously empty — strengthen the fake route"

    local_shape = shape(local_result)
    http_shape = shape(http_result)
    assert local_shape == http_shape, (
        f"resolve_chash: shape mismatch — local={local_shape!r} http={http_shape!r} "
        f"(local value: {local_result!r}, http value: {http_result!r})"
    )
