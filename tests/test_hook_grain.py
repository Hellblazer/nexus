# SPDX-License-Identifier: AGPL-3.0-or-later
"""Grain-aware batch-hook dispatch (nexus-duoak.7).

The duoak-2C batched indexer fires per-file batch hooks AND per-flush
aggregate batch hooks. File-agnostic consumers (taxonomy, chash) declare
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
    def test_taxonomy_and_chash_are_flush_grain(self) -> None:
        from nexus.mcp_infra import (
            chash_dual_write_batch_hook,
            manifest_write_batch_hook,
            taxonomy_assign_batch_hook,
        )
        assert getattr(taxonomy_assign_batch_hook, "batch_grain", "file") == "flush"
        assert getattr(chash_dual_write_batch_hook, "batch_grain", "file") == "flush"
        assert getattr(manifest_write_batch_hook, "batch_grain", "file") == "file"
