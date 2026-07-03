# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-h8rf6.3: RETURN-SHAPE conformance for HttpCatalogClient.docs_for_chashes.

Incident: the 2026-07-03 6.2.0 cloud shakeout found ``nx index repo`` in
service mode degrading to a FULL re-chunk + re-embed on every run (hours of
wall time + Voyage spend where an incremental run should have touched only
changed files). Root cause: ``HttpCatalogClient.docs_for_chashes`` returned a
flat ``list[str]`` where local ``Catalog.docs_for_chashes`` returns
``dict[str, list[str]]``. ``indexer_utils.build_staleness_cache`` calls
``by_chash.items()`` on the result, which raised ``AttributeError: 'list'
object has no attribute 'items'`` — silently swallowed to a WARNING log, so
the staleness cache came back empty and every file looked stale.

RDR-168's signature-conformance test (``test_catalog_conformance.py``) is a
PARAMETER-shape guard only (``inspect.signature`` name/kind comparison) — it
provably does not, and by design cannot, catch a RETURN-shape drift like this
one. This module is the dedicated recurrence guard for that gap:

  1. ``TestDocsForChashesReturnShapeParity`` — both the local ``Catalog`` and
     the real ``HttpCatalogClient`` (against a fake server mirroring the
     actual Java wire shape) return ``dict[str, list[str]]`` for the same
     chash, never a bare list.
  2. ``TestBuildStalenessCacheConsumesRealHttpClient`` — wires
     ``build_staleness_cache`` to a REAL ``HttpCatalogClient`` instance (not
     a ``MagicMock``) via the ``make_catalog_reader`` seam it actually uses
     in production. The existing MagicMock-stubbed regression in
     ``tests/test_indexer_utils_repo.py`` (``test_build_resolves_phase3_
     chunks_via_catalog_manifest``) only proves the CONSUMER handles a dict
     correctly — a ``MagicMock`` has no real return-type contract to
     violate, so it cannot catch a PRODUCER shape regression. This test can.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from nexus.catalog.catalog import Catalog
from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.indexer_utils import build_staleness_cache
from tests.catalog.test_http_catalog_client import (
    CHASH_A,
    CHUNK_SHA_A,
    FakeCatalogHandler,
    start_fake_server,
)

# ── local Catalog fixture (mirrors tests/test_catalog_manifest_read_api.py) ─


def _make_local_catalog(tmp_path: Path) -> Catalog:
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    db_path = tmp_path / "catalog.sqlite"
    return Catalog(catalog_dir=catalog_dir, db_path=db_path)


def _insert_doc(cat: Catalog, tumbler: str, collection: str) -> None:
    cat._db.execute(  # epsilon-allow: test fixture seeds documents row
        "INSERT OR IGNORE INTO documents "
        "(tumbler, title, author, year, content_type, file_path, "
        "corpus, physical_collection, chunk_count, head_hash, indexed_at, "
        "metadata, source_mtime, alias_of, source_uri) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            tumbler, f"doc-{tumbler}", "", 0, "code", f"/tmp/{tumbler}.py",
            "", collection, 0, "", "", "{}", 0.0, "", "",
        ),
    )
    cat._db.commit()


def _make_chunk(chash: str, position: int) -> dict[str, Any]:
    return {"chash": chash, "position": position}


@pytest.fixture
def local_catalog(tmp_path: Path) -> Catalog:
    cat = _make_local_catalog(tmp_path)
    _insert_doc(cat, "1.1.1", "code__shape__voyage-code-3__v1")
    cat.write_manifest("1.1.1", [_make_chunk(CHASH_A, 0)])
    return cat


# ── HttpCatalogClient fixture — real fake-server round trip ────────────────
# Reuses the FakeCatalogHandler wire fixtures from test_http_catalog_client.py:
#   POST /manifest/docs_for_chashes -> {"tumblers": ["1.1.1"]}   (flat, real shape)
#   POST /manifest/get_many         -> {"manifests": {"1.1.1": [{"chash": CHASH_A, ...}]}}
# so docs_for_chashes's client-side reshape has real per-doc manifest data to
# intersect against, exactly like the real Java service.


@pytest.fixture(scope="module")
def fake_server():
    server, url = start_fake_server()
    yield url
    server.shutdown()


@pytest.fixture
def http_client(fake_server: str):
    with HttpCatalogClient(
        base_url=fake_server, tenant="tenant_shape", _token="test_tok",
    ) as c:
        yield c


class TestDocsForChashesReturnShapeParity:
    def test_local_returns_dict_of_lists(self, local_catalog: Catalog) -> None:
        result = local_catalog.docs_for_chashes([CHASH_A])
        assert isinstance(result, dict)
        assert result == {CHASH_A: ["1.1.1"]}

    def test_http_returns_dict_of_lists(self, http_client: HttpCatalogClient) -> None:
        result = http_client.docs_for_chashes([CHASH_A])
        assert isinstance(result, dict)
        assert result == {CHASH_A: ["1.1.1"]}

    def test_both_implementations_agree_on_container_type(
        self, local_catalog: Catalog, http_client: HttpCatalogClient,
    ) -> None:
        """Paired assertion: neither implementation returns a bare list.

        This is the exact predicate the pre-fix code violated — local
        returned dict, service returned list, and no test compared them
        side by side.
        """
        local_result = local_catalog.docs_for_chashes([CHASH_A])
        http_result = http_client.docs_for_chashes([CHASH_A])
        assert type(local_result) is type(http_result) is dict
        assert set(local_result.keys()) == set(http_result.keys())

    def test_empty_input_returns_empty_dict_on_both(
        self, local_catalog: Catalog, http_client: HttpCatalogClient,
    ) -> None:
        assert local_catalog.docs_for_chashes([]) == {}
        assert http_client.docs_for_chashes([]) == {}

    def test_unknown_chash_omitted_on_both(
        self, local_catalog: Catalog, http_client: HttpCatalogClient,
    ) -> None:
        assert local_catalog.docs_for_chashes(["z" * 32]) == {}
        assert http_client.docs_for_chashes(["z" * 32]) == {}


class TestBuildStalenessCacheConsumesRealHttpClient:
    def test_no_raise_with_real_http_catalog_client(
        self, http_client: HttpCatalogClient,
    ) -> None:
        """Regression for the 6.2.0 shakeout AttributeError.

        Phase-3 metadata (``chunk_text_hash`` only, no ``doc_id``) forces
        ``build_staleness_cache`` down the ``docs_for_chashes`` resolution
        path against a REAL ``HttpCatalogClient`` reached via the
        ``make_catalog_reader`` seam ``indexer_utils.py`` actually uses.
        Pre-fix this raised inside the try/except (swallowed to a WARNING
        log) and ``by_doc_id`` came back empty; this test asserts the
        resolved doc_id lands in the cache, which only happens when the
        shape is correct end to end.
        """
        col = MagicMock()
        col.get.return_value = {
            "ids": ["c1"],
            "metadatas": [{
                "chunk_text_hash": CHUNK_SHA_A,  # full 64-char form: exercises the [:32] wire normalization
                "content_hash": "hash-a",
                "embedding_model": "voyage-code-3",
            }],
        }

        import nexus.catalog.factory as _factory_mod  # noqa: PLC0415 — mirrors indexer_utils.py's own deferred import of this seam
        with patch.object(_factory_mod, "make_catalog_reader", return_value=http_client):
            cache = build_staleness_cache(col)

        assert cache.by_doc_id == {"1.1.1": ("hash-a", "voyage-code-3")}
