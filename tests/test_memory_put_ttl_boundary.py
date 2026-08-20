# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-tk070.p6a (RDR-194 D5) — memory_put no longer coerces ttl<=0.

The MCP tool used to silently rewrite ``ttl<=0`` to ``None`` (the retired
``nexus-cg13x`` shim, ``ttl=ttl if ttl > 0 else None``). This suite proves
two things about the *client* half of the retirement, independent of a live
engine substrate (the engine's own boundary-400 and CHECK-constraint halves
are proven at the Java layer — see ``MemoryHandlerTest`` and
``MemoryRepositoryTest`` in ``service/src/test/java``):

1. ``core.memory_put`` passes ``ttl`` THROUGH verbatim — no client-side
   coercion happens before the store call.
2. When the store raises (simulating the engine's real HTTP 400, whose
   message shape is fixed by ``HttpMemoryStore._raise_for_status``, which
   embeds the JSON body's ``"error"`` field), ``memory_put`` does NOT
   swallow it — the ``_mcp_tool_error`` return string surfaces the engine's
   own "name the fix" wording to the caller.

A fake ``_t2_ctx``/store stands in for the real engine so this suite runs
in the unit tier (no service, no API keys) and pins core.py's own contract
precisely — it deliberately does not re-prove the engine's HTTP/DB-layer
behavior, which belongs to the Java suite.
"""
from __future__ import annotations

import httpx
import pytest


class _FakeMemoryStore:
    """Mimics HttpMemoryStore.put's real-engine 400 shape for ttl<=0.

    The message text mirrors what ``_raise_for_status``
    (``nexus/db/t2/_refreshable_client.py``) actually builds: it extracts the
    JSON body's ``"error"`` field into the exception message. The literal
    wording here matches ``MemoryHandler.requirePositiveOrNullTtl``'s error
    text (Java), so an accidental drift between the two would be caught by a
    reviewer reading both, even though this test cannot invoke the Java code
    directly.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def put(
        self,
        *,
        project: str,
        title: str,
        content: str,
        tags: str = "",
        ttl: int | None = 30,
        agent: str | None = None,
        session: str | None = None,
    ) -> int:
        self.calls.append({"project": project, "title": title, "ttl": ttl})
        if ttl is not None and ttl <= 0:
            req = httpx.Request("POST", "http://fake-engine/v1/memory/put")
            resp = httpx.Response(400, request=req)
            raise httpx.HTTPStatusError(
                f"HttpMemoryStore./v1/memory/put failed: HTTP 400: "
                f"ttl={ttl} is invalid: omit the field or pass null for a "
                f"permanent entry — ttl must be a positive integer number of "
                f"days (0 does NOT mean permanent; NULL does)",
                request=req,
                response=resp,
            )
        return 42


class _FakeT2Db:
    def __init__(self, memory: _FakeMemoryStore) -> None:
        self.memory = memory

    def put(self, **kwargs):
        return self.memory.put(**kwargs)


class _FakeT2Ctx:
    def __init__(self, db: _FakeT2Db) -> None:
        self._db = db

    def __enter__(self) -> _FakeT2Db:
        return self._db

    def __exit__(self, *exc_info) -> bool:
        return False


@pytest.fixture
def fake_memory_store(monkeypatch: pytest.MonkeyPatch) -> _FakeMemoryStore:
    """Replace core._t2_ctx with a fake yielding a fake HttpMemoryStore.

    Patches the module attribute directly (not ``t2_ctx`` at its definition
    site) — ``core.memory_put`` looks up ``_t2_ctx`` as a module global at
    CALL time, so rebinding ``core._t2_ctx`` here is picked up without
    needing to chase ``default_db_path`` indirection (that indirection only
    matters for the now-retired SQLite path; ``HttpMemoryStore`` doesn't
    take a db path at all — see T2Database.__init__, Stage 2 sub-stage A3).
    """
    store = _FakeMemoryStore()
    db = _FakeT2Db(store)

    import nexus.mcp.core as core

    monkeypatch.setattr(core, "_t2_ctx", lambda: _FakeT2Ctx(db))
    monkeypatch.delenv("NX_SESSION_ID", raising=False)
    return store


class TestMemoryPutTtlBoundary:
    def test_ttl_zero_not_coerced_and_surfaces_engine_400(
        self, fake_memory_store: _FakeMemoryStore,
    ) -> None:
        from nexus.mcp.core import memory_put

        result = memory_put(content="x", project="p", title="t", ttl=0)

        assert fake_memory_store.calls[-1]["ttl"] == 0, (
            "core.memory_put must pass ttl=0 THROUGH verbatim to the store "
            "call -- no client-side coercion to None (the retired "
            "nexus-cg13x shim, `ttl=ttl if ttl > 0 else None`)"
        )
        assert result.startswith("Error:"), result
        assert "ttl=0" in result, (
            "the engine's 400 must be SURFACED to the caller, not swallowed "
            "into a bare 'Error occurred' or (worse) a false 'Stored:'"
        )
        assert "permanent" in result.lower(), "the fix must be NAMED, not just flagged invalid"

    def test_ttl_negative_also_not_coerced_and_surfaces_400(
        self, fake_memory_store: _FakeMemoryStore,
    ) -> None:
        from nexus.mcp.core import memory_put

        result = memory_put(content="x", project="p", title="t", ttl=-5)

        assert fake_memory_store.calls[-1]["ttl"] == -5
        assert result.startswith("Error:"), result
        assert "ttl=-5" in result

    def test_ttl_none_is_permanent_and_not_rejected(
        self, fake_memory_store: _FakeMemoryStore,
    ) -> None:
        # Non-vacuity: the boundary must not reject the ACTUAL permanent
        # sentinel, only ttl<=0.
        from nexus.mcp.core import memory_put

        result = memory_put(content="x", project="p", title="t", ttl=None)

        assert fake_memory_store.calls[-1]["ttl"] is None
        assert result.startswith("Stored:"), result

    def test_ttl_positive_is_unaffected(
        self, fake_memory_store: _FakeMemoryStore,
    ) -> None:
        from nexus.mcp.core import memory_put

        result = memory_put(content="x", project="p", title="t", ttl=14)

        assert fake_memory_store.calls[-1]["ttl"] == 14
        assert result.startswith("Stored:"), result

    def test_default_ttl_still_thirty_days(
        self, fake_memory_store: _FakeMemoryStore,
    ) -> None:
        # Backward compat: omitting ttl entirely must still default to 30,
        # not silently become permanent as a side effect of the signature
        # widening to `int | None`.
        from nexus.mcp.core import memory_put

        memory_put(content="x", project="p", title="t")

        assert fake_memory_store.calls[-1]["ttl"] == 30
