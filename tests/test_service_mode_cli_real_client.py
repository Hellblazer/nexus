# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Real-client CLI tests for the nexus-h8rf6 service-mode method ports.

Wave-review CRITICAL (substantive-critic): nexus-umvh2's root cause was
CLI tests mocking the T3 handle with a bare ``MagicMock()`` (no ``spec=``),
which silently answers ANY method — so a method missing from
``HttpVectorClient`` ships to production undetected. cadae210 fixed that
for ``find_ids_by_title``/``batch_delete`` with a real ``HttpVectorClient``
over a faked transport (``_post``), exercised through the actual CLI
command; this module generalizes that pattern to the six sibling methods
ported in the same wave:

  - ``expire``                      (nx store expire,      nexus-h8rf6.5)
  - ``update_source_path``          (nx doctor --fix-paths, nexus-h8rf6.6)
  - ``list_chunks_with_metadata``   (nx t3 gc,             nexus-h8rf6.7)
  - ``delete_by_chunk_ids``         (nx t3 gc,             nexus-h8rf6.7)
  - ``list_unique_source_paths``    (nx t3 prune-stale,    nexus-h8rf6.7)
  - ``collection_metadata``         (doctor model-drift probe, nexus-h8rf6.8)

Only the HTTP transport (``_post``/``_get``) is faked — the client object
is real, so a missing/renamed/broken method fails HARD here instead of
being absorbed by a mock.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tests._catalog_fixture_ops import ActiveCatalog
from click.testing import CliRunner

from nexus.cli import main
from nexus.db.http_vector_client import (
    HttpVectorClient,
    reset_http_vector_client_for_tests,
)

_KNOWLEDGE = "knowledge__nexus-1-1__voyage-context-3__v1"
_CODE = "code__nexus-1-1__voyage-code-3__v1"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def real_client():
    reset_http_vector_client_for_tests()
    yield HttpVectorClient()
    reset_http_vector_client_for_tests()


# ── nx store expire (expire, nexus-h8rf6.5) ──────────────────────────────────


def test_store_expire_service_mode_real_client(runner, real_client, monkeypatch):
    """End-to-end through the real client: one expired chunk found in the
    knowledge collection, deleted, count reported."""
    posted = []

    def fake_post(path, body, **kw):
        posted.append((path, body))
        if path == "/v1/vectors/get":
            if body["offset"] > 0:
                return {"ids": [], "metadatas": []}
            return {
                "ids": ["dead", "fresh"],
                "metadatas": [
                    {"ttl_days": 1, "indexed_at": "2020-01-01T00:00:00+00:00"},
                    {"ttl_days": 36500, "indexed_at": "2026-01-01T00:00:00+00:00"},
                ],
            }
        if path == "/v1/vectors/store-delete":
            return {"deleted": len(body["ids"])}
        raise AssertionError(f"unexpected path {path}")

    # list_collections goes through GET /v1/vectors/stats
    monkeypatch.setattr(
        "nexus.db.http_vector_client._get",
        lambda path, **kw: [
            {"name": _KNOWLEDGE, "dim": 1024, "count": 2},
            {"name": _CODE, "dim": 1024, "count": 5},
        ],
    )
    monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)
    monkeypatch.setattr("nexus.commands.store._t3", lambda: real_client)

    result = runner.invoke(main, ["store", "expire"])
    assert result.exit_code == 0, result.output
    assert "1" in result.output
    deletes = [b for p, b in posted if p == "/v1/vectors/store-delete"]
    assert deletes == [{"collection": _KNOWLEDGE, "ids": ["dead"]}]


# ── nx doctor --fix-paths (catalog-only since nexus-bm8dd) ──────────────────


class TestFixPathsRealClient:
    @pytest.fixture(autouse=True)
    def _git_identity(self, monkeypatch):
        monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
        monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@test.invalid")
        monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
        monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@test.invalid")

    def test_fix_paths_service_mode_repairs_the_catalog_without_touching_t3(
        self, tmp_path, runner, real_client, monkeypatch,
    ):
        """nexus-bm8dd. This test used to prove fix-paths rewrote chunk
        metadata, and it did so by having its own fake_post RETURN chunks
        carrying ``source_path`` — a key RDR-102 D2 removed from the schema, so
        the real server can never return it. It asserted "2 T3 chunks updated"
        against numbers it had manufactured.

        The repair is, and now only claims to be, the catalog row. The
        vector transport must not be touched at all.
        """
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        cat_dir = tmp_path / "catalog"
        # nexus-aqbrk: seed through the ACTIVE catalog — doctor --fix-paths reads
        # via reader.docs_with_absolute_paths(), so a local-only seed left the
        # service catalog empty ("No absolute file_path entries found").
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(cat_dir))
        cat = ActiveCatalog()
        owner = cat.register_owner(
            "test-abc12345", "repo", repo_hash="abc12345", repo_root=str(repo_dir),
        )
        abs_path = str(repo_dir / "src" / "foo.py")
        cat.register(
            owner, "test-doc", content_type="code",
            file_path=abs_path, physical_collection=_CODE,
        )

        def fake_post(path, body, **kw):
            raise AssertionError(
                f"fix-paths must not call the vector service; got {path}"
            )

        monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)
        with (
            patch("nexus.config.catalog_path", return_value=cat_dir),
            patch("nexus.db.make_t3", return_value=real_client),
        ):
            result = runner.invoke(main, ["doctor", "--fix-paths"])
        assert result.exit_code == 0, result.output
        assert "Fixed 1" in result.output
        # NON-VACUITY: the repair really landed on the catalog row.
        entry = ActiveCatalog().resolve(str(cat.list_by_collection(_CODE)[0].tumbler))
        assert entry is not None and entry.file_path == "src/foo.py"
        # And no chunk-level claim is made.
        assert "chunks updated" not in result.output


# ── nx t3 gc (list_chunks_with_metadata + delete_by_chunk_ids, h8rf6.7) ──────


def test_t3_gc_service_mode_real_client(tmp_path, runner, real_client, monkeypatch):
    """Orphan scan + batch delete through the real client. Catalog is
    faked (empty manifest -> everything with an old indexed_at is orphan)."""
    chash = "a" * 64
    posted = []

    def fake_post(path, body, **kw):
        posted.append((path, body))
        if path == "/v1/vectors/get":
            if body["offset"] > 0:
                return {"ids": [], "metadatas": []}
            return {
                "ids": ["orphan1"],
                "metadatas": [
                    {
                        "chunk_text_hash": chash,
                        "indexed_at": "2020-01-01T00:00:00+00:00",
                    },
                ],
            }
        if path == "/v1/vectors/store-delete":
            return {"deleted": len(body["ids"])}
        raise AssertionError(f"unexpected path {path}")

    # Spec'd against the REAL service-mode catalog client so attributes it
    # doesn't have (like the local catalog's _dir) raise instead of
    # auto-materializing. Live-shakeout finding #4: the original bare mock
    # set fake_cat._dir = tmp_path, masking that gc's EventLog(cat._dir)
    # crashed with AttributeError on every real service-mode --no-dry-run.
    from nexus.catalog.http_catalog_client import HttpCatalogClient
    fake_cat = MagicMock(spec=HttpCatalogClient)
    fake_cat.chashes_for_collection.return_value = set()

    # nexus-fduai: the audit write goes through the catalog WRITER proxy,
    # not the reader — fake it at the verb's own seam.
    fake_writer = MagicMock()
    fake_writer.record_gc_audit.return_value = 42

    monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)
    with (
        patch("nexus.db.make_t3", return_value=real_client),
        patch("nexus.commands.t3._make_catalog", return_value=fake_cat),
        patch("nexus.commands.t3._make_catalog_writer", return_value=fake_writer),
    ):
        result = runner.invoke(
            main,
            # --allow-empty-manifest-set: this fixture's manifest references
            # zero chashes by construction (the test pins the real-client
            # wiring, not manifest semantics), which the nexus-jqrtp guard
            # otherwise refuses — same override as the test_t3_gc.py siblings.
            ["t3", "gc", "-c", _KNOWLEDGE, "--no-dry-run", "--yes",
             "--allow-empty-manifest-set"],
        )
    assert result.exit_code == 0, result.output
    assert "deleted 1 chunk(s)" in result.output
    deletes = [b for p, b in posted if p == "/v1/vectors/store-delete"]
    assert deletes == [{"collection": _KNOWLEDGE, "ids": ["orphan1"]}]
    fake_writer.record_gc_audit.assert_called_once()
    audit = fake_writer.record_gc_audit.call_args.kwargs
    assert audit["operation"] == "t3_gc"
    assert audit["collection"] == _KNOWLEDGE
    assert audit["actor"] == "nx t3 gc"
    assert audit["dry_run"] is False
    assert audit["chashes"] == [chash]
    assert audit["details"]["deleted"] == 1
    fake_writer.close.assert_called_once()


# ── nx t3 prune-stale (RETIRED, nexus-bm8dd) ─────────────────────────────────


def test_t3_prune_stale_service_mode_refuses(tmp_path, runner, real_client, monkeypatch):
    """nexus-bm8dd. The predecessor of this test drove a "real client" sweep
    whose own fake_post returned chunk metadata containing ``source_path`` —
    a key RDR-102 D2 removed, so the real server could never return it — and
    then asserted "deleted 2 chunk(s)". It passed while the verb deleted
    nothing on every real corpus.

    The verb is retired. It must refuse, and must not reach the transport.
    """
    def fake_post(path, body, **kw):
        raise AssertionError(f"retired verb must not call the service; got {path}")

    monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)
    with patch("nexus.db.make_t3", return_value=real_client):
        result = runner.invoke(
            main,
            ["t3", "prune-stale", "-c", _KNOWLEDGE, "--no-dry-run", "--confirm"],
        )
    assert result.exit_code != 0, result.output
    assert "RETIRED" in result.output
    assert "nx catalog prune-stale" in result.output
    # It must not read as a completed sweep.
    assert "chunk(s)" not in result.output


# ── doctor model-drift probe (collection_metadata, nexus-h8rf6.8) ────────────


def test_model_drift_probe_service_mode_real_client(real_client, monkeypatch):
    """The retrieval-quality probe resolves collection_metadata through the
    REAL client (default metadata_fn) — outcome must not be 'error'
    (the pre-fix service-mode symptom) and drift detection must work."""
    from nexus.doctor_search import run_retrieval_quality_probe
    from nexus.search_engine import SearchDiagnostics

    monkeypatch.setattr(HttpVectorClient, "count", lambda self, c: 7)

    def fake_search(query, cols, n, t3, *, diagnostics_out):
        diag = SearchDiagnostics()
        diag.per_collection[_CODE] = (3, 0, 0.45, 0.30)  # raw, dropped, thr, top
        diagnostics_out.append(diag)
        return [MagicMock()]

    results = run_retrieval_quality_probe(
        t3=real_client,
        collections=[_CODE],
        search_fn=fake_search,
    )
    assert len(results) == 1
    # 'error' is the pre-fix service-mode symptom; 'model_drift' would mean
    # collection_metadata resolved the wrong model for a conformant name.
    assert results[0].outcome == "matched", (results[0].outcome, results[0].error)


# ── nx collection re-embed (get_collection + stub.count, nexus-c9xr2) ────────


def test_collection_reembed_dry_run_service_mode_real_client(
    runner, real_client, monkeypatch,
):
    """nexus-c9xr2: re-embed reached db._client.get_collection — an attr NO
    production handle has post-RDR-155 — so every real invocation crashed
    with a raw AttributeError. The command now uses db.get_collection();
    this drives the dry-run end-to-end through the REAL HttpVectorClient
    (the _ServiceCollectionStub's new count() included) so the seam can
    never be mock-masked again."""
    coll = _KNOWLEDGE

    def fake_get(path, tenant="default"):
        if path.startswith("/v1/vectors/count"):
            return {"count": 7}
        if path.startswith("/v1/vectors/stats"):
            # list_collections' primary path: /stats returns a BARE LIST of
            # per-collection stat rows (collection_stats docstring).
            return [{"name": coll, "dim": 1024, "count": 7}]
        raise AssertionError(f"unexpected GET {path}")

    monkeypatch.setattr("nexus.db.http_vector_client._get", fake_get)

    with patch("nexus.commands.collection._t3", return_value=real_client):
        result = runner.invoke(
            main, ["collection", "re-embed", coll, "--to", "voyage-code-3"],
        )
    assert result.exit_code == 0, result.output
    assert "dry-run" in result.output
    assert "7" in result.output


def test_collection_reembed_cross_model_rejected_service_mode(
    runner, real_client, monkeypatch,
):
    """nexus-u37lw: server-side embedding routes by the COLLECTION NAME's
    model segment, so a cross-model --to can never take effect in service
    mode — pre-fix it silently no-opped with the old model, stamped the new
    model into metadata, and printed success. Must fail loud, post nothing."""
    posted = []

    def fake_post(path, body, **kw):
        posted.append(path)
        if path == "/v1/vectors/collections":
            return {"collections": [{"name": _KNOWLEDGE}]}
        raise AssertionError(f"unexpected POST {path}")

    def fake_get(path, tenant="default"):
        if path.startswith("/v1/vectors/stats"):
            return [{"name": _KNOWLEDGE, "dim": 1024, "count": 3}]
        if path.startswith("/v1/vectors/count"):
            return {"count": 3}
        raise AssertionError(f"unexpected GET {path}")

    monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)
    monkeypatch.setattr("nexus.db.http_vector_client._get", fake_get)

    with patch("nexus.commands.collection._t3", return_value=real_client):
        # _KNOWLEDGE encodes voyage-context-3; ask for voyage-code-3.
        result = runner.invoke(
            main, ["collection", "re-embed", _KNOWLEDGE,
                   "--to", "voyage-code-3", "--no-dry-run", "--yes"],
        )
    assert result.exit_code != 0
    assert "cannot take effect" in result.output
    assert "rename" in result.output
    # Nothing written: no upsert route was ever posted.
    assert not any("upsert" in p for p in posted)


def test_collection_reembed_same_model_requests_server_side_re_embed(
    runner, real_client, monkeypatch,
):
    """nexus-u37lw / nexus-sghyo (2026-08-06): same-model service re-embed
    sends ``force_re_embed: True`` and NO client-computed ``embeddings`` —
    the server recomputes the vector.

    This used to assert the nexus-hxry2 "verbatim passthrough" (client
    embeds via Voyage, vectors ride the wire in the upsert body). That
    path is retired: the client no longer embeds via Voyage at all (Hal
    determination 2026-07-28), so ``_reembed_collection`` never has a
    client-computed vector to pass through — see that function's
    docstring in ``src/nexus/commands/collection.py``. The bar this test
    now pins is that the CLI's re-embed command asks the server to
    recompute (``force_re_embed``), not that it silently no-ops via the
    existence-partition skip.
    """
    coll = _CODE  # encodes voyage-code-3
    upserts = []

    def fake_post(path, body, **kw):
        if path == "/v1/vectors/get":
            if body.get("offset", 0) > 0:
                return {"ids": [], "documents": [], "metadatas": []}
            return {
                "ids": ["c1"],
                "documents": ["def f(): pass"],
                "metadatas": [{"embedding_model": "voyage-code-3"}],
            }
        if path == "/v1/vectors/upsert-chunks":
            upserts.append(body)
            return {"upserted": 1}
        raise AssertionError(f"unexpected POST {path}")

    def fake_get(path, tenant="default"):
        if path.startswith("/v1/vectors/stats"):
            return [{"name": coll, "dim": 1024, "count": 1}]
        if path.startswith("/v1/vectors/count"):
            return {"count": 1}
        raise AssertionError(f"unexpected GET {path}")

    monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)
    monkeypatch.setattr("nexus.db.http_vector_client._get", fake_get)

    with patch("nexus.commands.collection._t3", return_value=real_client):
        result = runner.invoke(
            main, ["collection", "re-embed", coll,
                   "--to", "voyage-code-3", "--no-dry-run", "--yes"],
        )
    assert result.exit_code == 0, result.output
    assert len(upserts) == 1
    # Server-side re-embed requested; no client-computed vectors sent.
    assert upserts[0].get("force_re_embed") is True
    assert upserts[0].get("embeddings") is None
