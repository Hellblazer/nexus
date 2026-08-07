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


class TestHookTimings:
    """nexus-lde88 G4: fire_batch's optional hook_timings= accumulates
    per-hook-name wall seconds, so a caller firing multiple batch_grain
    hooks in one call (taxonomy + manifest, both grain="flush") can split
    the total back out by name instead of it staying one merged bucket."""

    def test_hook_timings_keyed_by_hook_name(self) -> None:
        import time as _time

        reg = HookRegistry()

        def slow_hook(doc_ids, collection, contents, embeddings=None, metadatas=None):
            _time.sleep(0.02)

        def quick_hook(doc_ids, collection, contents, embeddings=None, metadatas=None):
            pass

        slow_hook.__name__ = "slow_hook"
        quick_hook.__name__ = "quick_hook"
        reg.register_batch(slow_hook)
        reg.register_batch(quick_hook)

        timings: dict[str, float] = {}
        reg.fire_batch(["d1"], "code__x", ["t"], hook_timings=timings)

        assert set(timings) == {"slow_hook", "quick_hook"}
        assert timings["slow_hook"] >= 0.02
        assert timings["quick_hook"] < 0.02

    def test_hook_timings_accumulates_across_calls(self) -> None:
        """A caller reusing one dict across several fire_batch calls (the
        indexer's run-lifetime _flush_hook_by_name) gets a running total,
        not a per-call overwrite."""
        reg = HookRegistry()
        calls: list = []
        reg.register_batch(_mk_hook(calls))

        timings: dict[str, float] = {}
        reg.fire_batch(["d1"], "code__x", ["t"], hook_timings=timings)
        first = timings["hook"]
        reg.fire_batch(["d2"], "code__x", ["t"], hook_timings=timings)
        assert timings["hook"] >= first

    def test_hook_timings_none_by_default_no_crash(self) -> None:
        # Default (no hook_timings=) path must still work — the vast
        # majority of fire_batch callers never pass it.
        reg = HookRegistry()
        calls: list = []
        reg.register_batch(_mk_hook(calls))
        reg.fire_batch(["d1"], "code__x", ["t"])
        assert calls == [["d1"]]

    def test_hook_timings_recorded_even_on_hook_failure(self) -> None:
        """A raising hook is still timed and attributed — best-effort
        failure handling must not blind the timing accounting."""
        reg = HookRegistry()

        def boom(doc_ids, collection, contents, embeddings=None, metadatas=None):
            raise RuntimeError("boom")
        boom.__name__ = "boom"
        reg.register_batch(boom)

        timings: dict[str, float] = {}
        reg.fire_batch(["d1"], "code__x", ["t"], hook_timings=timings)
        assert "boom" in timings


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
    """Critic S2: lock in the 'file-agnostic' claim with the real store
    client — aggregating N files' chunks into one flush-grain call must
    produce the same rows as N per-file calls. Ported to HttpChashIndex
    against the in-process fake ChashHandler (nexus-i711w: the SQLite
    ChashIndex died with the 7.0.0 wave); the equivalence now also pins
    the client-side dedup (nexus-85z0y) collapsing the shared-text
    duplicate inside the aggregate batch."""

    def test_chash_aggregate_equals_per_file(self) -> None:
        import hashlib
        import threading
        from http.server import HTTPServer

        from nexus.db.t2.http_chash_index import HttpChashIndex
        from tests.db._fake_t2_server import free_port as _free_port
        from tests.db.test_http_chash_index import (
            _STORE,
            _STORE_LOCK,
            TOKEN,
            _FakeChashHandler,
        )

        port = _free_port()
        server = HTTPServer(("127.0.0.1", port), _FakeChashHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base_url = f"http://127.0.0.1:{port}"
        with _STORE_LOCK:
            _STORE.clear()
        try:
            mk = lambda s: hashlib.sha256(s.encode()).hexdigest()
            file_a = [mk(f"a{i}") for i in range(4)]
            file_b = [mk(f"b{i}") for i in range(3)]
            file_c = [mk("a0")]  # duplicate of a chunk in file_a (shared text)

            per_file = HttpChashIndex(base_url=base_url, _token=TOKEN)
            for chunk_set in (file_a, file_b, file_c):
                per_file.upsert_many(chashes=chunk_set, collection="code__per_file")
            per_file.close()

            aggregate = HttpChashIndex(base_url=base_url, _token=TOKEN)
            aggregate.upsert_many(
                chashes=file_a + file_b + file_c, collection="code__aggregate"
            )
            aggregate.close()

            with _STORE_LOCK:
                per_rows = {ch for (ch, coll) in _STORE if coll == "code__per_file"}
                agg_rows = {ch for (ch, coll) in _STORE if coll == "code__aggregate"}
            assert agg_rows == per_rows
            assert len(agg_rows) == 7  # 4 + 3 unique; the dup collapses
        finally:
            with _STORE_LOCK:
                _STORE.clear()
            server.shutdown()

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
