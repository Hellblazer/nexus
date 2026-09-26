# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-wbfpw.29 (RDR-192 Step 3b): a manifest-hook EXCEPTION must feed
the same per-document collectors the hook's own short-circuit path
(GH #1397 / nexus-94fxl) already feeds, so ``nx index``'s existing
exit-code check (nexus-7lw6a) catches it too.

``HookRegistry.fire_batch`` catches every hook's exception generically
(logged + persisted to T2 ``hook_failures``, never propagated) — that
part is unchanged and is not what these tests pin. What these tests pin
is the ADDITIONAL, manifest-hook-specific routing this bead adds:
:func:`nexus.hook_registry._record_manifest_hook_batch_exception`, called
from inside that same except block.

Pure unit tests: a real ``HookRegistry`` and the real
``nexus.mcp_infra.manifest_write_batch_hook`` reference (patched via
monkeypatch to raise, never mocked at the ``fire_batch``/HookRegistry
level under test), no substrate needed — the collectors this bead reads
and writes are process-local Python state, not T2/T3-backed. The
substrate-backed end-to-end acceptance test (fault-inject through a real
``nx index repo`` run) lives in
``tests/integration/test_wbfpw29_manifest_hook_exception_index_run.py``.
"""
from __future__ import annotations

import pytest

import nexus.mcp_infra as mcp_infra
from nexus.hook_registry import HookRegistry, LockedHookRegistry


@pytest.fixture(autouse=True)
def _clean_collectors():
    """The three collectors are process-global (nexus.mcp_infra module
    state) — zero them before and after every test in this file so a
    leftover entry from one test can never leak into the next (same
    precedent as test_commands_helpers_identity_drop.py's fixture)."""
    from nexus.commands._helpers import reset_identity_drop_collectors

    reset_identity_drop_collectors()
    yield
    reset_identity_drop_collectors()


def test_manifest_hook_exception_records_write_failure_by_catalog_doc_id(monkeypatch):
    """The common shape: fire_batch is called with an explicit
    catalog_doc_id (the RDR-108 Phase 3 identity for the batch's single
    document) and the manifest hook raises. The failure must land in
    get_manifest_write_failures(), keyed on that catalog_doc_id -- the
    exact collector nx index's exit check (nexus-7lw6a) reads."""
    def faulty(*args, **kwargs):
        raise RuntimeError("nexus-wbfpw.29 fault injection")

    monkeypatch.setattr(mcp_infra, "manifest_write_batch_hook", faulty)

    reg = HookRegistry()
    reg.register_batch(mcp_infra.manifest_write_batch_hook)
    reg.fire_batch(
        ["chash-1"], "code__x", ["content"],
        metadatas=[{"chunk_text_hash": "chash-1"}],
        catalog_doc_id="1.2.3",
    )

    assert mcp_infra.get_manifest_write_failures() == ["1.2.3"]
    assert mcp_infra.get_manifest_identity_drops() == []


def test_manifest_hook_exception_records_write_failure_by_legacy_meta_doc_id(monkeypatch):
    """Pre-Phase-3 shape: no catalog_doc_id on the call, doc identity
    comes from each chunk's own metadata (mcp_infra's ``by_doc``
    fallback). Multiple docs in one failing batch all get recorded --
    over-work, never under-report, the same convention
    mcp_infra._apply_combined_write_response's mismatch handling uses."""
    def faulty(*args, **kwargs):
        raise RuntimeError("nexus-wbfpw.29 fault injection")

    monkeypatch.setattr(mcp_infra, "manifest_write_batch_hook", faulty)

    reg = HookRegistry()
    reg.register_batch(mcp_infra.manifest_write_batch_hook)
    reg.fire_batch(
        ["chash-1", "chash-2"], "code__x", ["c1", "c2"],
        metadatas=[{"doc_id": "1.9.0"}, {"doc_id": "1.9.1"}],
    )

    assert sorted(mcp_infra.get_manifest_write_failures()) == ["1.9.0", "1.9.1"]
    assert mcp_infra.get_manifest_identity_drops() == []


def test_manifest_hook_exception_with_no_doc_identity_records_identity_drop(monkeypatch):
    """A batch with no recoverable document identity at all (no
    catalog_doc_id, no legacy meta doc_id) is the exact shape nexus-94fxl's
    short-circuit already closes for the RETURN path; reaching it via an
    exception instead must not be silently lost either."""
    def faulty(*args, **kwargs):
        raise RuntimeError("nexus-wbfpw.29 fault injection")

    monkeypatch.setattr(mcp_infra, "manifest_write_batch_hook", faulty)

    reg = HookRegistry()
    reg.register_batch(mcp_infra.manifest_write_batch_hook)
    reg.fire_batch(
        ["chash-1"], "code__x", ["content"],
        metadatas=[{}],
    )

    assert mcp_infra.get_manifest_write_failures() == []
    assert mcp_infra.get_manifest_identity_drops() == [
        {"collection": "code__x", "batch_size": 1}
    ]


def test_non_manifest_hook_exception_does_not_touch_manifest_collectors(monkeypatch):
    """A DIFFERENT hook raising (taxonomy-assign shape) must stay
    best-effort exactly as before -- it must never populate the
    manifest-specific collectors nx index's exit check reads, so it must
    never change the exit code by way of this bead's new routing."""
    def faulty_other_hook(*args, **kwargs):
        raise RuntimeError("some other hook broke")

    reg = HookRegistry()
    reg.register_batch(faulty_other_hook)
    reg.fire_batch(
        ["chash-1"], "code__x", ["content"],
        metadatas=[{"doc_id": "1.2.3"}],
        catalog_doc_id="1.2.3",
    )

    assert mcp_infra.get_manifest_write_failures() == []
    assert mcp_infra.get_manifest_identity_drops() == []


def test_manifest_hook_exception_via_locked_registry_still_records(monkeypatch):
    """nx index repo wraps HookRegistry in LockedHookRegistry
    (indexer.py); its invoke seam must not bypass this bead's routing --
    the except block that calls _record_manifest_hook_batch_exception
    lives in HookRegistry.fire_batch, wrapping the invoke(...) call
    itself, so this must hold under the locked proxy exactly as it does
    for a direct HookRegistry."""
    def faulty(*args, **kwargs):
        raise RuntimeError("nexus-wbfpw.29 fault injection")

    monkeypatch.setattr(mcp_infra, "manifest_write_batch_hook", faulty)

    reg = HookRegistry()
    reg.register_batch(mcp_infra.manifest_write_batch_hook)
    locked = LockedHookRegistry(reg)
    locked.fire_batch(
        ["chash-1"], "code__x", ["content"],
        metadatas=[{"chunk_text_hash": "chash-1"}],
        catalog_doc_id="1.2.3",
    )

    assert mcp_infra.get_manifest_write_failures() == ["1.2.3"]
