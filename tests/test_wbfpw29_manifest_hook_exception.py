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


# ── Fix round 1 (code-review Critical + critic Critical/Important/Significant) ──


def test_malformed_metadata_entry_does_not_crash_fire_batch(monkeypatch):
    """code-review Critical: _record_manifest_hook_batch_exception used to
    call meta.get(...) unconditionally on every entry in metadatas -- a
    malformed batch (metadatas=[None]) raised AttributeError OUT of
    fire_batch's own except block, aborting the `for hook in self._batch`
    dispatch loop entirely. A hook registered AFTER the manifest hook must
    still fire for this batch, and fire_batch itself must not raise."""
    def faulty_manifest(*args, **kwargs):
        raise RuntimeError("nexus-wbfpw.29 fault injection")

    monkeypatch.setattr(mcp_infra, "manifest_write_batch_hook", faulty_manifest)

    second_hook_calls: list = []

    def second_hook(doc_ids, collection, contents, embeddings=None, metadatas=None):
        second_hook_calls.append(list(doc_ids))

    reg = HookRegistry()
    reg.register_batch(mcp_infra.manifest_write_batch_hook)
    reg.register_batch(second_hook)

    # Must not raise -- this is the assertion under test.
    reg.fire_batch(["chash-1"], "code__x", ["content"], metadatas=[None])

    assert second_hook_calls == [["chash-1"]], (
        "a hook registered after the manifest hook must still fire"
    )
    # A malformed batch with no recoverable identity is the identity-drop
    # shape, not silently nothing.
    assert mcp_infra.get_manifest_identity_drops() == [
        {"collection": "code__x", "batch_size": 1}
    ]


def test_real_manifest_hook_get_catalog_raises_records_write_failure(monkeypatch):
    """critic Critical: the REAL, unpatched manifest_write_batch_hook (not
    a wholesale-replaced double) has its OWN internal
    `try: get_catalog() except Exception: ... return` -- a silent return
    that used to leave every document in this batch with no manifest and
    no signal at all. Registers the production function directly."""
    def raising_get_catalog():
        raise RuntimeError("catalog unreachable")

    monkeypatch.setattr(mcp_infra, "get_catalog", raising_get_catalog)

    reg = HookRegistry()
    reg.register_batch(mcp_infra.manifest_write_batch_hook)
    reg.fire_batch(
        ["chash-1"], "code__x", ["content"],
        metadatas=[{"doc_id": "1.2.3", "chunk_text_hash": "chash-1"}],
    )

    assert mcp_infra.get_manifest_write_failures() == ["1.2.3"]


def test_real_manifest_hook_get_catalog_none_records_write_failure(monkeypatch):
    """critic Critical, sibling of the exception case above: `get_catalog()`
    returning None (catalog configured but not yet initialised) is the
    OTHER silent-return branch in the same function."""
    monkeypatch.setattr(mcp_infra, "get_catalog", lambda: None)

    reg = HookRegistry()
    reg.register_batch(mcp_infra.manifest_write_batch_hook)
    reg.fire_batch(
        ["chash-1"], "code__x", ["content"],
        metadatas=[{"doc_id": "1.2.3", "chunk_text_hash": "chash-1"}],
    )

    assert mcp_infra.get_manifest_write_failures() == ["1.2.3"]


def test_real_manifest_hook_get_catalog_writer_raises_does_not_crash_fire_batch(monkeypatch):
    """code-review Important (test-coverage note): closes the
    identity-check coverage gap directly -- proves `hook is
    manifest_write_batch_hook` correctly matches the REAL, unpatched
    production reference when the failure originates deep inside the
    hook's own body (get_catalog_writer(), which is NOT wrapped in the
    hook's own try/except and so propagates out to fire_batch) rather
    than from a wholesale-replaced double standing in for the whole hook."""
    fake_reader = object()  # truthy, non-None: enough to pass both catalog gates

    monkeypatch.setattr(mcp_infra, "get_catalog", lambda: fake_reader)

    def raising_get_catalog_writer():
        raise RuntimeError("writer construction failed")

    monkeypatch.setattr(mcp_infra, "get_catalog_writer", raising_get_catalog_writer)

    reg = HookRegistry()
    reg.register_batch(mcp_infra.manifest_write_batch_hook)

    # Must not raise.
    reg.fire_batch(
        ["chash-1"], "code__x", ["content"],
        metadatas=[{"doc_id": "1.2.3", "chunk_text_hash": "chash-1"}],
    )

    assert mcp_infra.get_manifest_write_failures() == ["1.2.3"]


def test_collectors_inert_without_an_active_cli_index_run(monkeypatch):
    """code-review Important: the long-lived MCP server's store_put path
    fires the SAME manifest hook and never calls
    reset_identity_drop_collectors() (only the four CLI nx index/nx dt
    index entry points do). Recording must be a no-op until one of them
    has reset the collectors at least once THIS process -- otherwise the
    lists grow forever with zero consumer in a long-lived server."""
    monkeypatch.setattr(mcp_infra, "_identity_drop_collectors_active", False)

    mcp_infra._record_manifest_write_failure("1.2.3")
    mcp_infra._record_manifest_identity_drop("code__x", 2)

    assert mcp_infra.get_manifest_write_failures() == []
    assert mcp_infra.get_manifest_identity_drops() == []

    # An active CLI run (reset_identity_drop_collectors, or either half of
    # it) arms recording for the rest of this (short-lived) process.
    mcp_infra.reset_manifest_write_failures()
    mcp_infra._record_manifest_write_failure("1.2.3")
    assert mcp_infra.get_manifest_write_failures() == ["1.2.3"]


def test_write_failures_dedup_stable_order(monkeypatch):
    """code-review Important: _MANIFEST_WRITE_FAILURES had no dedup -- a
    document that fails across multiple continuation-slice flushes
    (documented normal shape for a multi-batch file) appended once per
    flush, so the warning printed "3 document(s) (X, X, X)" for ONE
    document. Recording the same doc_id repeatedly must collapse to one
    entry, in first-seen order."""
    mcp_infra.reset_manifest_write_failures()

    mcp_infra._record_manifest_write_failure("1.9.1")
    mcp_infra._record_manifest_write_failure("1.9.0")
    mcp_infra._record_manifest_write_failure("1.9.1")
    mcp_infra._record_manifest_write_failure("1.9.1")

    assert mcp_infra.get_manifest_write_failures() == ["1.9.1", "1.9.0"]


# ── Fix round 2 ──────────────────────────────────────────────────────────────


def test_manifest_hook_exception_routing_survives_a_broken_mcp_infra_import():
    """code-review round-2 residual: the identity-check import
    (`from nexus.mcp_infra import manifest_write_batch_hook`) used to sit
    OUTSIDE the guarding try/except -- if it ever raised, the exact
    original failure mode (item 1's bug) would reproduce despite the
    try/except wrapping everything AFTER it. Poisoning sys.modules is the
    standard way to force an `ImportError` on the next `from X import Y`;
    this proves the WHOLE body, import included, is now covered.

    Restores ``sys.modules`` manually (not via ``monkeypatch.setitem``):
    the autouse ``_clean_collectors`` fixture's teardown itself imports
    ``nexus.mcp_infra`` (via ``reset_identity_drop_collectors``), and
    fixture-vs-monkeypatch teardown ORDER does not guarantee the module is
    already unpoisoned by the time that runs -- observed directly: with
    ``monkeypatch.setitem`` the fixture teardown itself raised
    ``ModuleNotFoundError``. Restoring inline, before this test function
    returns, sidesteps that ordering question entirely.
    """
    import sys

    from nexus.hook_registry import _record_manifest_hook_batch_exception

    real_mcp_infra = sys.modules.get("nexus.mcp_infra")
    sys.modules["nexus.mcp_infra"] = None  # type: ignore[assignment]
    try:
        def faulty(*args, **kwargs):
            raise RuntimeError("nexus-wbfpw.29 fault injection")

        # Must not raise -- this is the assertion under test.
        _record_manifest_hook_batch_exception(
            faulty, doc_ids=["chash-1"], collection="code__x",
            metadatas=[{"doc_id": "1.2.3"}], catalog_doc_id="",
        )
    finally:
        if real_mcp_infra is not None:
            sys.modules["nexus.mcp_infra"] = real_mcp_infra
        else:
            del sys.modules["nexus.mcp_infra"]


def test_manifest_write_batch_hook_declares_flush_grain():
    """Regression pin for the round-2 critic Critical: the production
    ``manifest_write_batch_hook`` MUST keep its ``batch_grain = "flush"``
    classification attribute. A bare function replacement (as several
    unit tests above deliberately use to test HookRegistry's OWN
    exception-routing mechanism in isolation) loses this attribute, which
    made an earlier acceptance test pass for the wrong reason: the
    replacement silently reclassified as grain="file" and fired through a
    dispatch bucket (``indexer.py``'s per-file ``_fire_deferred_hooks``)
    the REAL hook never occupies for a ChunkBatcher-accepted file. Any
    test that exercises a REAL indexing entry point (not an isolated
    HookRegistry unit test) must register the unpatched
    ``mcp_infra.manifest_write_batch_hook`` -- see
    tests/integration/test_wbfpw29_manifest_hook_exception_index_run.py's
    channel-3 test -- and fault only its internal dependencies, never
    replace the function object itself."""
    assert mcp_infra.manifest_write_batch_hook.batch_grain == "flush"


# ── Round 8 (T2 critique-wbfpw29-r5 Significant): chash attribution ──────────
#
# Every test above only ever asserts the failed doc_id LIST
# (get_manifest_write_failures()). None of them assert
# get_manifest_write_failure_chashes() -- the per-doc EXPECTED chash set
# commands/_helpers.py's resolve_confirmed_write_failure_doc_ids reads to
# decide whether a same-run self-heal actually closed the gap. A mis-keyed
# grouping (doc A's chash recorded under doc B) would pass every test above
# unnoticed. These drive the REAL producers with two docs carrying distinct,
# known chashes and assert each doc keeps its OWN set -- never the
# sibling's -- plus the UNKNOWN (None) case for a row with no chash.


def test_real_manifest_hook_get_catalog_raises_chash_attribution_two_docs(monkeypatch):
    """manifest_write_batch_hook's get_catalog()-raises branch
    (mcp_infra.py ~2270): two docs in one failing batch must each keep
    their OWN chash, never the sibling's."""
    def raising_get_catalog():
        raise RuntimeError("catalog unreachable")

    monkeypatch.setattr(mcp_infra, "get_catalog", raising_get_catalog)

    reg = HookRegistry()
    reg.register_batch(mcp_infra.manifest_write_batch_hook)
    chash_a, chash_b = "a" * 64, "b" * 64
    reg.fire_batch(
        [chash_a, chash_b], "code__x", ["content-a", "content-b"],
        metadatas=[
            {"doc_id": "1.2.3", "chunk_text_hash": chash_a},
            {"doc_id": "1.9.9", "chunk_text_hash": chash_b},
        ],
    )

    assert sorted(mcp_infra.get_manifest_write_failures()) == ["1.2.3", "1.9.9"]
    expected = mcp_infra.get_manifest_write_failure_chashes()
    assert expected["1.2.3"] == frozenset({chash_a})
    assert expected["1.9.9"] == frozenset({chash_b})


def test_real_manifest_hook_get_catalog_raises_blank_chash_marks_unknown(monkeypatch):
    """A row with no chunk_text_hash means this write's expectation is
    only partly known -- the doc must be recorded UNKNOWN (None), never a
    partial/empty confirmed set."""
    def raising_get_catalog():
        raise RuntimeError("catalog unreachable")

    monkeypatch.setattr(mcp_infra, "get_catalog", raising_get_catalog)

    reg = HookRegistry()
    reg.register_batch(mcp_infra.manifest_write_batch_hook)
    reg.fire_batch(
        ["chash-1"], "code__x", ["content"],
        metadatas=[{"doc_id": "1.2.3"}],  # no chunk_text_hash key
    )

    assert mcp_infra.get_manifest_write_failures() == ["1.2.3"]
    assert mcp_infra.get_manifest_write_failure_chashes()["1.2.3"] is None


def test_real_manifest_hook_get_catalog_none_chash_attribution_two_docs(monkeypatch):
    """manifest_write_batch_hook's `_gate is None` branch (mcp_infra.py
    ~2285): same cross-doc attribution proof as the raises branch above --
    this branch had ZERO chash coverage before this round."""
    monkeypatch.setattr(mcp_infra, "get_catalog", lambda: None)

    reg = HookRegistry()
    reg.register_batch(mcp_infra.manifest_write_batch_hook)
    chash_a, chash_b = "c" * 64, "d" * 64
    reg.fire_batch(
        [chash_a, chash_b], "code__x", ["content-a", "content-b"],
        metadatas=[
            {"doc_id": "2.1", "chunk_text_hash": chash_a},
            {"doc_id": "2.2", "chunk_text_hash": chash_b},
        ],
    )

    assert sorted(mcp_infra.get_manifest_write_failures()) == ["2.1", "2.2"]
    expected = mcp_infra.get_manifest_write_failure_chashes()
    assert expected["2.1"] == frozenset({chash_a})
    assert expected["2.2"] == frozenset({chash_b})


def test_real_manifest_hook_get_catalog_none_blank_chash_marks_unknown(monkeypatch):
    """The `_gate is None` branch's UNKNOWN case."""
    monkeypatch.setattr(mcp_infra, "get_catalog", lambda: None)

    reg = HookRegistry()
    reg.register_batch(mcp_infra.manifest_write_batch_hook)
    reg.fire_batch(
        ["chash-1"], "code__x", ["content"],
        metadatas=[{"doc_id": "2.1"}],
    )

    assert mcp_infra.get_manifest_write_failures() == ["2.1"]
    assert mcp_infra.get_manifest_write_failure_chashes()["2.1"] is None


def test_manifest_hook_exception_chash_attribution_two_docs_via_hook_registry(monkeypatch):
    """hook_registry._record_manifest_hook_batch_exception builds its OWN
    chash_by_doc independently of mcp_infra's -- same cross-doc proof,
    driven through fire_batch's except-block routing rather than the
    hook's own body."""
    def faulty(*args, **kwargs):
        raise RuntimeError("nexus-wbfpw.29 fault injection")

    monkeypatch.setattr(mcp_infra, "manifest_write_batch_hook", faulty)

    reg = HookRegistry()
    reg.register_batch(mcp_infra.manifest_write_batch_hook)
    chash_a, chash_b = "e" * 64, "f" * 64
    reg.fire_batch(
        [chash_a, chash_b], "code__x", ["c1", "c2"],
        metadatas=[
            {"doc_id": "1.9.0", "chunk_text_hash": chash_a},
            {"doc_id": "1.9.1", "chunk_text_hash": chash_b},
        ],
    )

    assert sorted(mcp_infra.get_manifest_write_failures()) == ["1.9.0", "1.9.1"]
    expected = mcp_infra.get_manifest_write_failure_chashes()
    assert expected["1.9.0"] == frozenset({chash_a})
    assert expected["1.9.1"] == frozenset({chash_b})


def test_manifest_hook_exception_legacy_meta_doc_id_blank_chash_marks_unknown(monkeypatch):
    """hook_registry's routing on the exact input
    test_manifest_hook_exception_records_write_failure_by_legacy_meta_doc_id
    already drives (no chunk_text_hash on either row) must record BOTH
    docs UNKNOWN, not an empty-but-known confirmed set."""
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
    expected = mcp_infra.get_manifest_write_failure_chashes()
    assert expected["1.9.0"] is None
    assert expected["1.9.1"] is None
