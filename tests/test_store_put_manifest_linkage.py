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

import pytest

from nexus import mcp_infra
from nexus.mcp_infra import (
    get_manifest_identity_drops,
    manifest_write_batch_hook,
    reset_manifest_identity_drops,
)


@pytest.fixture(autouse=True)
def git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in [
        ("GIT_AUTHOR_NAME", "Test"),
        ("GIT_AUTHOR_EMAIL", "test@test.invalid"),
        ("GIT_COMMITTER_NAME", "Test"),
        ("GIT_COMMITTER_EMAIL", "test@test.invalid"),
    ]:
        monkeypatch.setenv(k, v)


# test_cli_store_put_writes_manifest_linkage (and its catalog_env fixture)
# RETIRED (nexus-i711w terminal deletion): it was sqlite-pinned by design
# (nexus-b6enc forced NX_STORAGE_BACKEND=sqlite so the CLI store-put path hit
# a seeded LOCAL catalog and read it back via raw ``cat._db.execute``) — the
# substrate is deleted. The store-put manifest-linkage contract stays pinned
# live by tests/test_mcp_store_put_doc_id.py (MCP side, same
# single_chunk_manifest_metadata root cause) and
# tests/test_b6enc_store_put_ghost_compensation.py (service-arm store_put
# catalog compensation, the live P0 pin).


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
