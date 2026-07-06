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

import chromadb
import pytest
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
from click.testing import CliRunner

from nexus.catalog.catalog import Catalog
from nexus.catalog.tumbler import Tumbler
from nexus.db.t3 import T3Database


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
        _client=chromadb.EphemeralClient(),
        _ef_override=DefaultEmbeddingFunction(),
    )


@pytest.fixture
def catalog_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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
