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

  - The helper synthesises a conformant 4-segment name when no catalog
    resolution is available. (nexus-i711w terminal deletion: the
    local-catalog resolution leg and its two pins retired with the local
    catalog — see the tombstones below.)
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


# test_resolve_rdr_collection_uses_catalog_when_initialized (and its
# catalog_with_owner fixture) RETIRED (nexus-i711w terminal deletion): its
# subject was the hook's LOCAL-``Catalog.init`` resolution leg, which is dead
# code now (the hook's ``from nexus.catalog import Catalog`` raises and it
# always falls through to the indexer's path-derived conformant synthesis —
# the behaviour pinned by the surviving tests below).


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


# test_resolve_rdr_collection_synthesises_conformant_when_owner_unregistered
# RETIRED (nexus-i711w terminal deletion): its premise — a LOCAL catalog
# initialized via ``Catalog.init`` with no owner row — died with the local
# catalog. The surviving fallback contract (helper synthesises the conformant
# 4-segment name when no catalog resolution is available) is pinned by
# test_resolve_rdr_collection_synthesises_conformant_when_catalog_absent above.


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
