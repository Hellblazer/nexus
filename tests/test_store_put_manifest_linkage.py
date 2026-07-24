# SPDX-License-Identifier: AGPL-3.0-or-later
"""GH #1370 Defect 4b: ``nx store put`` (CLI) must write real
catalog ``document_chunks`` manifest linkage, not just register the
catalog entry.

Pre-fix, ``put_cmd`` called ``hooks.fire_store_chains(..., catalog_doc_id=...)``
without a ``metadatas`` argument, so it defaulted to ``None`` and
``manifest_write_batch_hook`` short-circuited on its
``if not metadatas: return`` guard — the catalog document shipped with
``chunk_count=0`` and no ``document_chunks`` rows forever. Mirrors
``test_mcp_store_put_doc_id.py``'s MCP-side coverage for the same root
cause (``nexus.catalog.store_hook.single_chunk_manifest_metadata``).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
from click.testing import CliRunner

from nexus import mcp_infra
from nexus.catalog.catalog import Catalog
from nexus.catalog.tumbler import Tumbler
from nexus.db.t3 import T3Database
from nexus.mcp_infra import (
    get_manifest_identity_drops,
    manifest_write_batch_hook,
    reset_manifest_identity_drops,
)
from tests.conftest import make_vector_test_client


@pytest.fixture(autouse=True)
def git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in [
        ("GIT_AUTHOR_NAME", "Test"),
        ("GIT_AUTHOR_EMAIL", "test@test.invalid"),
        ("GIT_COMMITTER_NAME", "Test"),
        ("GIT_COMMITTER_EMAIL", "test@test.invalid"),
    ]:
        monkeypatch.setenv(k, v)


@pytest.fixture
def local_t3() -> T3Database:
    return T3Database(
        _client=make_vector_test_client(),
        _ef_override=DefaultEmbeddingFunction(),
    )


@pytest.fixture
def catalog_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # nexus-b6enc: pin sqlite/local so the CLI store-put path targets
    # THIS seeded local catalog even under the NX_TEST_T2_SUBSTRATE=engine
    # flip (which sets NX_STORAGE_BACKEND=service globally and re-routed
    # the catalog hooks at the engine tenant — pre-existing engine-run
    # failure of test_cli_store_put_writes_manifest_linkage).
    monkeypatch.setenv("NX_STORAGE_BACKEND", "sqlite")
    catalog_dir = tmp_path / "catalog"
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
    Catalog.init(catalog_dir)
    return catalog_dir


def test_cli_store_put_writes_manifest_linkage(
    tmp_path: Path,
    local_t3: T3Database,
    catalog_env: Path,
) -> None:
    from nexus.cli import main

    f = tmp_path / "doc.md"
    f.write_text("body for CLI manifest-linkage regression test")

    with patch("nexus.commands.store._t3", lambda: local_t3):
        runner = CliRunner()
        result = runner.invoke(main, [
            "store", "put", str(f),
            "--collection", "knowledge",
            "--title", "cli-manifest-linkage",
        ])
    assert result.exit_code == 0, result.output

    cat = Catalog(catalog_env, catalog_env / ".catalog.db")
    rows = cat._db.execute(
        "SELECT tumbler FROM documents WHERE title = 'cli-manifest-linkage'"
    ).fetchall()
    assert rows, "expected a catalog entry for the CLI-stored doc"
    tumbler = rows[0][0]

    entry = cat.resolve(Tumbler.parse(tumbler))
    assert entry is not None
    assert entry.chunk_count >= 1, (
        "manifest_write_batch_hook must populate chunk_count for CLI "
        "nx store put; got chunk_count=0 (pre-fix regression)"
    )

    manifest_rows = cat.get_manifest(tumbler)
    assert manifest_rows, (
        "expected document_chunks manifest rows for the CLI-stored doc"
    )


# ── nexus-94fxl / GH #1397: identity-drop collector ──────────────────────────


def test_hook_records_identity_drop_when_no_doc_id():
    """A batch with metadatas but NO document identity (no catalog_doc_id, no
    meta doc_id) previously vanished through a zero-log early return — the
    GH #1397 mechanism-1 signature. It must be recorded for the end-of-run
    summary and logged, so a clean '0 failed' run can no longer hide it."""
    reset_manifest_identity_drops()
    manifest_write_batch_hook(
        ["id1", "id2"], "rdr__nexus", ["c1", "c2"], None,
        [{"chunk_text_hash": "h1"}, {"chunk_text_hash": "h2"}],
    )
    drops = get_manifest_identity_drops()
    assert drops == [{"collection": "rdr__nexus", "batch_size": 2}]
    reset_manifest_identity_drops()
    assert get_manifest_identity_drops() == []


def test_hook_no_identity_drop_when_doc_id_present(tmp_path, monkeypatch):
    """Sanity inverse: a batch WITH catalog_doc_id records no drop."""
    reset_manifest_identity_drops()
    # Stop before any catalog I/O: an uninitialised catalog (gate None) exits
    # after the identity grouping, which is all this test asserts on.
    monkeypatch.setattr(mcp_infra, "get_catalog", lambda: None)
    manifest_write_batch_hook(
        ["id1"], "rdr__nexus", ["c1"], None,
        [{"chunk_text_hash": "h1"}],
        catalog_doc_id="1.3.142",
    )
    assert get_manifest_identity_drops() == []
