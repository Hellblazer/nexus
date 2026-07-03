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


# ── nx doctor --fix-paths (update_source_path, nexus-h8rf6.6) ────────────────


class TestFixPathsRealClient:
    @pytest.fixture(autouse=True)
    def _git_identity(self, monkeypatch):
        monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
        monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@test.invalid")
        monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
        monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@test.invalid")

    def test_fix_paths_service_mode_real_client(
        self, tmp_path, runner, real_client, monkeypatch,
    ):
        from nexus.catalog.catalog import Catalog

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        cat_dir = tmp_path / "catalog"
        cat = Catalog.init(cat_dir)
        owner = cat.register_owner(
            "test-abc12345", "repo", repo_hash="abc12345", repo_root=str(repo_dir),
        )
        abs_path = str(repo_dir / "src" / "foo.py")
        cat.register(
            owner, "test-doc", content_type="code",
            file_path=abs_path, physical_collection=_CODE,
        )

        posted = []

        def fake_post(path, body, **kw):
            posted.append((path, body))
            if path == "/v1/vectors/get":
                if body["offset"] > 0:
                    return {"ids": [], "metadatas": []}
                return {
                    "ids": ["c1", "c2"],
                    "metadatas": [
                        {"source_path": abs_path, "title": "foo"},
                        {"source_path": abs_path},
                    ],
                }
            if path == "/v1/vectors/update-metadata":
                return {}
            raise AssertionError(f"unexpected path {path}")

        monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)
        with (
            patch("nexus.config.catalog_path", return_value=cat_dir),
            patch("nexus.db.make_t3", return_value=real_client),
        ):
            result = runner.invoke(main, ["doctor", "--fix-paths"])
        assert result.exit_code == 0, result.output
        assert "Fixed 1" in result.output
        assert "2 T3 chunks updated" in result.output
        updates = [b for p, b in posted if p == "/v1/vectors/update-metadata"]
        assert updates and updates[0]["ids"] == ["c1", "c2"]
        assert all(
            m["source_path"] == "src/foo.py" for m in updates[0]["metadatas"]
        )


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

    fake_cat = MagicMock()
    fake_cat.chashes_for_collection.return_value = set()
    fake_cat._dir = tmp_path  # EventLog(cat._dir) needs a real directory

    monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)
    with (
        patch("nexus.db.make_t3", return_value=real_client),
        patch("nexus.commands.t3._make_catalog", return_value=fake_cat),
    ):
        result = runner.invoke(
            main, ["t3", "gc", "-c", _KNOWLEDGE, "--no-dry-run", "--yes"],
        )
    assert result.exit_code == 0, result.output
    assert "deleted 1 chunk(s)" in result.output
    deletes = [b for p, b in posted if p == "/v1/vectors/store-delete"]
    assert deletes == [{"collection": _KNOWLEDGE, "ids": ["orphan1"]}]


# ── nx t3 prune-stale (list_unique_source_paths, nexus-h8rf6.7) ──────────────


def test_t3_prune_stale_service_mode_real_client(
    tmp_path, runner, real_client, monkeypatch,
):
    """Stale-path sweep through the real client: one live file, one ghost;
    the ghost's chunks are deleted via delete_by_source."""
    real_file = tmp_path / "real.md"
    real_file.write_text("hello")
    ghost = tmp_path / "ghost.md"  # never created

    def fake_post(path, body, **kw):
        if path == "/v1/vectors/get":
            if body.get("where"):
                # delete_by_source's id resolution for the ghost path
                assert body["where"] == {"source_path": str(ghost)}
                if body["offset"] > 0:
                    return {"ids": [], "metadatas": []}
                return {"ids": ["g1", "g2"]}
            if body["offset"] > 0:
                return {"ids": [], "metadatas": []}
            return {
                "ids": ["r1", "g1", "g2"],
                "metadatas": [
                    {"source_path": str(real_file)},
                    {"source_path": str(ghost)},
                    {"source_path": str(ghost)},
                ],
            }
        if path == "/v1/vectors/store-delete":
            return {"deleted": len(body["ids"])}
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)
    with patch("nexus.db.make_t3", return_value=real_client):
        result = runner.invoke(
            main,
            ["t3", "prune-stale", "-c", _KNOWLEDGE, "--no-dry-run", "--confirm"],
        )
    assert result.exit_code == 0, result.output
    assert "deleted 2 chunk(s)" in result.output
    assert str(real_file) not in result.output


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
