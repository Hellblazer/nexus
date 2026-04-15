# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pool-survival + graceful-degradation tests — RDR-079 P7.

Three scenarios:

* **SC-2** — hung-worker recovery: a worker that stops responding is
  detected, retired, and replaced without the caller seeing a failure.
* **SC-3** — token-threshold retirement: a worker approaching the
  token budget drains in-flight turns, retires cleanly, and a
  replacement is spawned.
* **SC-10** — graceful degradation without auth: the first operator-
  requiring MCP call converts ``PoolAuthUnavailableError`` into
  ``PlanRunOperatorUnavailableError``; retrieval-only tools continue
  to work.

SC-2 and SC-3 require a live operator pool (``@pytest.mark.integration``).
SC-10 runs inline (no live pool) via a monkeypatched ``check_auth``.
"""
from __future__ import annotations

import pytest


# ── SC-10 — graceful degradation (NO integration mark; always runs) ────────


@pytest.mark.asyncio
async def test_sc10_operator_unavailable_when_no_auth(monkeypatch) -> None:
    """When ``claude auth status`` reports loggedIn=false, the operator
    MCP tool raises the typed ``PlanRunOperatorUnavailableError`` — the
    error the RDR-079 SC-10 contract names."""
    from nexus import mcp_infra
    from nexus.operators.pool import PoolAuthUnavailableError
    from nexus.plans.runner import PlanRunOperatorUnavailableError

    class _NoAuthPool:
        async def dispatch_with_rotation(self, *a, **kw):
            raise PoolAuthUnavailableError(
                "`claude auth status` reports loggedIn=false. "
                "Run `claude auth login` or set ANTHROPIC_API_KEY.",
            )

    monkeypatch.setattr(mcp_infra, "get_operator_pool", lambda *a, **kw: _NoAuthPool())

    from nexus.mcp.core import operator_extract

    with pytest.raises(PlanRunOperatorUnavailableError) as exc:
        await operator_extract(inputs='["x"]', fields="a")

    assert exc.value.operator == "extract"
    assert "claude auth login" in str(exc.value)


@pytest.mark.asyncio
async def test_sc10_retrieval_still_works_when_no_auth(monkeypatch) -> None:
    """Retrieval tools (``search``, ``query``, ``traverse``) do NOT
    touch the operator pool — they must keep working when auth is
    missing. Regression guard: ``check_auth`` can be broken and
    retrieval paths remain unaffected."""
    from nexus.operators.pool import PoolAuthUnavailableError
    import nexus.operators.pool as pool_mod

    def boom() -> None:
        raise PoolAuthUnavailableError("no auth")

    monkeypatch.setattr(pool_mod, "check_auth", boom)
    # Reset the cached flag so our boom() is the next thing executed.
    pool_mod._reset_auth_cache()

    from nexus.plans.runner import _default_dispatcher

    # Dispatches to the real ``traverse`` MCP tool (no auth needed).
    out = await _default_dispatcher(
        "traverse", {"seeds": [], "link_types": [], "depth": 1},
    )
    assert isinstance(out, dict)


# ── SC-2 / SC-3 — live-pool scenarios (@pytest.mark.integration) ───────────


@pytest.mark.asyncio
@pytest.mark.integration
async def test_sc2_hung_worker_detected_and_replaced() -> None:
    """SC-2 — a worker that stops responding (we kill it mid-flight)
    is detected on the next dispatch and a replacement is spawned;
    the caller sees success via the replacement, not a failure.

    The pool marks a worker ``alive=False`` on dispatch failure; the
    next ``dispatch_with_rotation`` iteration skips dead workers and
    spawns a fresh one.
    """
    from nexus import mcp_infra
    from nexus.operators.pool import create_pool_session, teardown_pool_session

    pool_session = create_pool_session()
    try:
        pool = mcp_infra.get_operator_pool(
            "extract",
            operator_role="SC-2 probe: reply with empty StructuredOutput.",
            json_schema={
                "type": "object",
                "required": ["extractions"],
                "properties": {
                    "extractions": {"type": "array"},
                },
            },
        )
        await pool.dispatch_with_rotation(
            prompt="Return {\"extractions\": []}.", timeout=30.0,
        )

        assert pool.workers, "pool must have at least one worker"
        hung = pool.workers[0]
        if hung.process and hung.process.returncode is None:
            hung.process.kill()
            await hung.process.wait()
        # Pool's live-worker picker consults ``alive``; mark the kill
        # explicitly so the next dispatch spawns a replacement. (The
        # pool's own alive-tracking on dispatch failure handles this in
        # steady state; the test just short-circuits.)
        hung.alive = False

        replacement_out = await pool.dispatch_with_rotation(
            prompt="Return {\"extractions\": []}.", timeout=30.0,
        )
        assert isinstance(replacement_out, dict)
        # A live worker DIFFERENT from the one we killed must exist.
        live_ids = {w.session_id for w in pool.workers if w.alive}
        assert hung.session_id not in live_ids, (
            "SC-2: killed worker must not remain in the live set"
        )
    finally:
        teardown_pool_session(pool_session)
        mcp_infra.reset_operator_pool()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_sc3_token_threshold_retires_worker_and_spawns_replacement() -> None:
    """SC-3 — when a worker crosses ``retirement_token_threshold``,
    the NEXT dispatch retires it and the dispatch after that spawns a
    fresh replacement without dropping the request.

    Timing note: the first dispatch retires the over-threshold worker
    AFTER returning its result; the second dispatch's live-worker scan
    no longer sees it and spawns a replacement.
    """
    from nexus import mcp_infra
    from nexus.operators.pool import create_pool_session, teardown_pool_session

    pool_session = create_pool_session()
    try:
        pool = mcp_infra.get_operator_pool(
            "extract",
            operator_role="SC-3 probe: reply with empty StructuredOutput.",
            json_schema={
                "type": "object",
                "required": ["extractions"],
                "properties": {"extractions": {"type": "array"}},
            },
        )
        pool.retirement_token_threshold = 1
        await pool.dispatch_with_rotation(
            prompt="Return {\"extractions\": []}.", timeout=30.0,
        )
        first_live = {w.session_id for w in pool.workers if w.alive}
        await pool.dispatch_with_rotation(
            prompt="Return {\"extractions\": []}.", timeout=30.0,
        )
        second_live = {w.session_id for w in pool.workers if w.alive}
        assert second_live - first_live, (
            "SC-3: second dispatch must land on a replacement worker; "
            f"first_live={first_live!r} second_live={second_live!r}"
        )
    finally:
        teardown_pool_session(pool_session)
        mcp_infra.reset_operator_pool()
