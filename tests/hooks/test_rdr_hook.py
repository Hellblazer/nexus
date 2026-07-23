# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the RDR SessionStart hook.

RDR-103 Phase 3b + Phase 5: ``rdr_hook.py`` resolves the indexed
collection name through the catalog (via
``Catalog.collection_for_repo``) when both the catalog and the owner
exist. Without a catalog or owner row, the helper falls back to
:func:`nexus.indexer._repo_collection_or_legacy` which synthesises a
conformant 4-segment name from the path-derived identity (Phase 5
tightening; pre-Phase-5 the fallback was the legacy 2-segment shape).
The test surface pins:

  - The hook helper returns the conformant ``CollectionName.render()``
    when the catalog is initialized and the repo has a registered owner.
  - The helper synthesises a conformant 4-segment name when the catalog
    is absent (operator workstations that have not run
    ``nx catalog setup``).
  - The helper synthesises a conformant 4-segment name when the catalog
    is initialized but the repo has no owner row.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

# RDR-109 Phase 2: this file asserts cloud-mode canonical behavior
# (voyage-* embedder names, canonical-set defaults). The cloud_mode
# fixture sets credentials and forces ``is_local_mode()`` to False so
# the assertions hold regardless of the host environment.
pytestmark = pytest.mark.usefixtures("cloud_mode")

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK_PATH = REPO_ROOT / "conexus" / "hooks" / "scripts" / "rdr_hook.py"


@pytest.fixture()
def rdr_hook_module():
    """Import ``rdr_hook.py`` as a module so we can call its helpers
    directly. The script is not on the import path by default — load
    it via spec_from_file_location."""
    spec = importlib.util.spec_from_file_location("rdr_hook_under_test", HOOK_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def catalog_with_owner(tmp_path, monkeypatch):
    """Set up a catalog at tmp_path/catalog with one registered owner."""
    from nexus.catalog.catalog import Catalog

    cat_dir = tmp_path / "catalog"
    cat = Catalog.init(cat_dir)
    cat.register_owner(
        name="myproject",
        owner_type="repo",
        repo_hash="cafef00d",
        repo_root=str(tmp_path / "myproject"),
    )
    monkeypatch.setattr("nexus.config.catalog_path", lambda: cat_dir)
    monkeypatch.setattr(
        "nexus.repo_identity._repo_identity",
        lambda r: ("myproject", "cafef00d"),
    )
    return cat_dir


def test_resolve_rdr_collection_uses_catalog_when_initialized(
    rdr_hook_module, catalog_with_owner, tmp_path,
):
    repo = tmp_path / "myproject"
    repo.mkdir()
    name = rdr_hook_module._resolve_rdr_collection(repo)
    # Owner 1.1 to owner_segment 1-1; canonical model voyage-context-3;
    # new tuple lands at v1.
    assert name == "rdr__1-1__voyage-context-3__v1"


def test_resolve_rdr_collection_synthesises_conformant_when_catalog_absent(
    rdr_hook_module, tmp_path, monkeypatch,
):
    """No catalog at the configured path: helper falls back to the
    indexer's path-derived conformant synthesis. Keeps SessionStart
    functional on workstations that have not initialized the catalog
    while still emitting a 4-segment name that satisfies T3's
    strict-naming guard (RDR-103 Phase 5).
    """
    repo = tmp_path / "isolated"
    repo.mkdir()
    monkeypatch.setattr(
        "nexus.config.catalog_path",
        lambda: tmp_path / "no_such_catalog",
    )
    monkeypatch.setattr(
        "nexus.repo_identity._repo_identity",
        lambda r: ("isolated", "abcdef12"),
    )
    name = rdr_hook_module._resolve_rdr_collection(repo)
    assert name == "rdr__isolated-abcdef12__voyage-context-3__v1"


def test_resolve_rdr_collection_synthesises_conformant_when_owner_unregistered(
    rdr_hook_module, tmp_path, monkeypatch,
):
    """Catalog initialized but no owner registered for this repo:
    helper falls back to the indexer's path-derived conformant
    synthesis (Phase 5; pre-Phase-5 returned the legacy 2-segment
    shape).
    """
    from nexus.catalog.catalog import Catalog

    cat_dir = tmp_path / "catalog"
    Catalog.init(cat_dir)
    monkeypatch.setattr("nexus.config.catalog_path", lambda: cat_dir)

    repo = tmp_path / "fresh"
    repo.mkdir()
    monkeypatch.setattr(
        "nexus.repo_identity._repo_identity",
        lambda r: ("fresh", "deadbeef"),
    )
    name = rdr_hook_module._resolve_rdr_collection(repo)
    assert name == "rdr__fresh-deadbeef__voyage-context-3__v1"


# ── nexus-e2sim: `open` is a pre-accept synonym for `draft` (GH #1409) ──────


def _write_rdr(tmp_path: Path, status: str) -> Path:
    f = tmp_path / "001-test-decision.md"
    f.write_text(f"---\nstatus: {status}\ntitle: test\n---\n\n# RDR-001\n")
    return f


def test_reconcile_open_file_vs_draft_t2_is_no_op(
    rdr_hook_module, tmp_path, monkeypatch
) -> None:
    """nexus-e2sim (GH #1409 follow-through): rdr-create seeds T2 at
    'draft', so an RDR file legitimately using 'open' (the qsryj-accepted
    pre-accept synonym) must NOT be silently rewritten back to 'draft' by
    the SessionStart reconcile — the exact revert the fix was filed
    against. Equal rank means neither side wins: pure no-op."""
    mod = rdr_hook_module
    f = _write_rdr(tmp_path, "open")
    file_writes: list = []
    t2_writes: list = []
    monkeypatch.setattr(
        mod, "_update_file_status", lambda *a: file_writes.append(a) or True
    )
    monkeypatch.setattr(
        mod, "_update_t2_status", lambda *a: t2_writes.append(a) or True
    )

    reconciled = mod._reconcile(
        tmp_path, "myrepo", [f], {"001": "draft"}
    )

    assert reconciled == 0
    assert file_writes == [], (
        "the hook must not rewrite an 'open' file back to 'draft' "
        "(the GH #1409 revert this test pins)"
    )
    assert t2_writes == []


def test_reconcile_draft_file_vs_open_t2_is_no_op(
    rdr_hook_module, tmp_path, monkeypatch
) -> None:
    mod = rdr_hook_module
    f = _write_rdr(tmp_path, "draft")
    file_writes: list = []
    t2_writes: list = []
    monkeypatch.setattr(
        mod, "_update_file_status", lambda *a: file_writes.append(a) or True
    )
    monkeypatch.setattr(
        mod, "_update_t2_status", lambda *a: t2_writes.append(a) or True
    )

    reconciled = mod._reconcile(
        tmp_path, "myrepo", [f], {"001": "open"}
    )

    assert reconciled == 0
    assert file_writes == [] and t2_writes == []


def test_reconcile_open_file_still_advances_to_accepted_t2(
    rdr_hook_module, tmp_path, monkeypatch
) -> None:
    """'open' ranks WITH 'draft', not above the lifecycle: a T2 record at
    'accepted' still wins and updates the file, same as it would for
    'draft'."""
    mod = rdr_hook_module
    f = _write_rdr(tmp_path, "open")
    file_writes: list = []
    monkeypatch.setattr(
        mod, "_update_file_status", lambda *a: file_writes.append(a) or True
    )
    monkeypatch.setattr(mod, "_update_t2_status", lambda *a: True)

    reconciled = mod._reconcile(
        tmp_path, "myrepo", [f], {"001": "accepted"}
    )

    assert reconciled == 1
    assert file_writes == [(f, "accepted")]
