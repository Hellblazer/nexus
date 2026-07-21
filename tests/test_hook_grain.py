# SPDX-License-Identifier: AGPL-3.0-or-later
"""Grain-aware batch-hook dispatch (nexus-duoak.7).

The duoak-2C batched indexer fires per-file batch hooks AND per-flush
aggregate batch hooks. File-agnostic consumers (taxonomy) declare
``batch_grain = "flush"`` and run once per upload batch; consumers that
need per-file identity (manifest, keyed on catalog_doc_id) stay at the
default file grain. Callers that don't pass ``grain`` fire everything —
so MCP store_put and every legacy path are behaviorally unchanged.
"""

from __future__ import annotations

from nexus.hook_registry import HookRegistry


def _mk_hook(calls: list, grain: str | None = None):
    def hook(doc_ids, collection, contents, embeddings=None, metadatas=None):
        calls.append(list(doc_ids))
    if grain is not None:
        hook.batch_grain = grain
    return hook


class TestGrainDispatch:
    def test_default_call_fires_all_grains(self) -> None:
        reg = HookRegistry()
        file_calls, flush_calls = [], []
        reg.register_batch(_mk_hook(file_calls))
        reg.register_batch(_mk_hook(flush_calls, grain="flush"))
        reg.fire_batch(["d1"], "code__x", ["t"])
        assert file_calls == [["d1"]]
        assert flush_calls == [["d1"]]

    def test_file_grain_call_skips_flush_hooks(self) -> None:
        reg = HookRegistry()
        file_calls, flush_calls = [], []
        reg.register_batch(_mk_hook(file_calls))
        reg.register_batch(_mk_hook(flush_calls, grain="flush"))
        reg.fire_batch(["d1"], "code__x", ["t"], grain="file")
        assert file_calls == [["d1"]]
        assert flush_calls == []

    def test_flush_grain_call_skips_file_hooks(self) -> None:
        reg = HookRegistry()
        file_calls, flush_calls = [], []
        reg.register_batch(_mk_hook(file_calls))
        reg.register_batch(_mk_hook(flush_calls, grain="flush"))
        reg.fire_batch(["d1", "d2"], "code__x", ["t", "u"], grain="flush")
        assert file_calls == []
        assert flush_calls == [["d1", "d2"]]

    def test_default_hook_grain_is_file(self) -> None:
        reg = HookRegistry()
        calls: list = []
        reg.register_batch(_mk_hook(calls))  # no attribute -> "file"
        reg.fire_batch(["d1"], "c", ["t"], grain="file")
        assert calls == [["d1"]]


class TestDefaultConsumersDeclareGrain:
    def test_default_consumers_grain_declarations(self) -> None:
        from nexus.mcp_infra import (
            manifest_write_batch_hook,
            taxonomy_assign_batch_hook,
        )
        assert getattr(taxonomy_assign_batch_hook, "batch_grain", "file") == "flush"
        # nexus-u2kwq: manifest joined flush grain — the batched indexer's
        # aggregate call injects per-chunk doc_id + file-local chunk_index
        # so by_doc grouping/positions stay correct; grain="all" callers
        # (MCP store_put, legacy inline) still fire it per document.
        assert getattr(manifest_write_batch_hook, "batch_grain", "file") == "flush"


class TestLockedRegistryGrainPassthrough:
    def test_locked_wrapper_forwards_grain_kwarg(self) -> None:
        # nexus-duoak.7 review Important: the batched indexer wraps the
        # registry in LockedHookRegistry BEFORE defining the flush-grain
        # closure; the wrapper must forward grain= untouched so the
        # dispatch filter still applies under concurrency.
        from nexus.hook_registry import LockedHookRegistry
        reg = HookRegistry()
        file_calls, flush_calls = [], []
        reg.register_batch(_mk_hook(file_calls))
        reg.register_batch(_mk_hook(flush_calls, grain="flush"))
        locked = LockedHookRegistry(reg)
        locked.fire_batch(["d1"], "code__x", ["t"], grain="flush")
        assert file_calls == []
        assert flush_calls == [["d1"]]
        locked.fire_batch(["d2"], "code__x", ["t"], grain="file")
        assert file_calls == [["d2"]]
        assert flush_calls == [["d1"]]
        locked.fire_batch(["d3"], "code__x", ["t"])  # default: all
        assert file_calls == [["d2"], ["d3"]]
        assert flush_calls == [["d1"], ["d3"]]


class TestFlushGrainOutcomeEquivalence:
    """Critic S2: lock in the 'file-agnostic' claim with real stores —
    aggregating N files' chunks into one flush-grain call must produce
    the same rows as N per-file calls."""

    def test_chash_aggregate_equals_per_file(self, tmp_path) -> None:
        import hashlib
        from nexus.db.t2.chash_index import ChashIndex

        mk = lambda s: hashlib.sha256(s.encode()).hexdigest()
        file_a = [mk(f"a{i}") for i in range(4)]
        file_b = [mk(f"b{i}") for i in range(3)]
        file_c = [mk("a0")]  # duplicate of a chunk in file_a (shared text)

        per_file = ChashIndex(tmp_path / "per_file.db")
        for chunk_set in (file_a, file_b, file_c):
            per_file.upsert_many(chashes=chunk_set, collection="code__x")

        aggregate = ChashIndex(tmp_path / "aggregate.db")
        aggregate.upsert_many(
            chashes=file_a + file_b + file_c, collection="code__x"
        )

        def rows(ix: ChashIndex) -> set[tuple[str, str]]:
            cur = ix.conn.execute(
                "SELECT chash, physical_collection FROM chash_index"
            )
            return set(cur.fetchall())

        assert rows(aggregate) == rows(per_file)
        assert len(rows(aggregate)) == 7  # 4 + 3 unique; the dup collapses
        per_file.close()
        aggregate.close()

    def test_flush_grain_failure_contract_documented(self) -> None:
        # Critic S1 companion: a flush-grain consumer failure affects the
        # WHOLE upload batch's files (widened from per-file). This test
        # pins the contract at the dispatch level: the failing flush-grain
        # hook is best-effort (fire_batch swallows), file-grain hooks and
        # the caller are untouched.
        reg = HookRegistry()
        file_calls: list = []

        def exploding_flush_hook(doc_ids, collection, contents,
                                 embeddings=None, metadatas=None):
            raise RuntimeError("aggregate consumer down")
        exploding_flush_hook.batch_grain = "flush"

        reg.register_batch(exploding_flush_hook)
        reg.register_batch(_mk_hook(file_calls))
        # flush-grain failure is swallowed (logged + T2 hook_failures)
        reg.fire_batch(["d1", "d2"], "code__x", ["t", "u"], grain="flush")
        # file-grain chain unaffected
        reg.fire_batch(["d1"], "code__x", ["t"], grain="file")
        assert file_calls == [["d1"]]
