"""The nx_plan_audit TOOL honours the termination rules (nexus-ll7zm).

THE LAYER THIS TESTS, and why it is separate from
``tests/plans/test_audit_rounds.py``: that file proves the rules are
correct; this file proves the tool USES them. A pure-function suite
passing while the tool still renders its own verdict would be the layer
mistake — a test below the layer production uses proves the layer, not
the feature.
"""

from __future__ import annotations

import inspect

import pytest

from nexus.plans.audit_rounds import (
    BLOCKS_PLANNING,
    DISCOVER_AT_IMPLEMENTATION,
    MAX_BLOCKING_ROUNDS,
    NOT_READY,
    RESIDUALS_ONLY,
)


def _tool_fn():
    """The undecorated coroutine behind the registered MCP tool."""
    from nexus.mcp.core import mcp

    tool = mcp._tool_manager.get_tool("nx_plan_audit")
    return tool.fn


@pytest.fixture
def audit_returning(monkeypatch):
    """Run the tool with a stubbed subprocess payload."""

    async def _run(findings: list[dict], **kwargs) -> str:
        async def fake_dispatch(prompt, schema, **_):
            fake_dispatch.prompt = prompt
            fake_dispatch.schema = schema
            return {
                "verdict": "NOT READY",  # the model's own claim, deliberately
                "summary": "stub summary",
                "findings": findings,
            }

        import nexus.operators.dispatch as dispatch_mod

        monkeypatch.setattr(dispatch_mod, "claude_dispatch", fake_dispatch)
        out = await _tool_fn()(plan_json="{}", **kwargs)
        _run.last_prompt = fake_dispatch.prompt
        _run.last_schema = fake_dispatch.schema
        return out

    return _run


class TestSignature:
    def test_the_tool_accepts_round_and_budget(self) -> None:
        params = inspect.signature(_tool_fn()).parameters
        assert "round_number" in params
        assert "budget_rounds" in params

    def test_round_defaults_to_one(self) -> None:
        """A caller that never counts gets round-1 semantics, not a crash."""
        params = inspect.signature(_tool_fn()).parameters
        assert params["round_number"].default == 1
        assert params["budget_rounds"].default == 0


@pytest.mark.asyncio
class TestVerdictEnforcement:
    async def test_round_one_blocker_blocks(self, audit_returning) -> None:
        out = await audit_returning(
            [{"classification": BLOCKS_PLANNING, "title": "wrong order"}],
            round_number=1,
        )
        assert NOT_READY in out
        assert "wrong order" in out

    async def test_past_the_cap_the_tool_cannot_block(self, audit_returning) -> None:
        """Even when the subprocess insists the verdict is NOT READY.

        The stub returns verdict="NOT READY" and a BLOCKS-PLANNING
        finding. The tool must override both.
        """
        out = await audit_returning(
            [{"classification": BLOCKS_PLANNING, "title": "one more thing"}],
            round_number=MAX_BLOCKING_ROUNDS + 1,
        )
        assert RESIDUALS_ONLY in out
        assert NOT_READY not in out
        assert "one more thing" in out
        assert "Record these residuals" in out

    async def test_residual_only_findings_do_not_block(self, audit_returning) -> None:
        out = await audit_returning(
            [{"classification": DISCOVER_AT_IMPLEMENTATION, "title": "missing fetch"}],
            round_number=1,
        )
        assert NOT_READY not in out
        assert "missing fetch" in out

    async def test_a_declared_budget_caps_earlier(self, audit_returning) -> None:
        out = await audit_returning(
            [{"classification": BLOCKS_PLANNING, "title": "x"}],
            round_number=2,
            budget_rounds=1,
        )
        assert RESIDUALS_ONLY in out

    async def test_unclassified_findings_still_block(self, audit_returning) -> None:
        out = await audit_returning([{"title": "unlabelled"}], round_number=1)
        assert NOT_READY in out

    async def test_a_malformed_findings_container_blocks(self, audit_returning) -> None:
        """A non-list findings payload must never read as a clean audit.

        The timeout-drain partial-JSON path can hand back a non-list;
        silently emptying it produced "Verdict: READY ... No findings."
        for a round whose output was lost (review finding, 2026-08-22).
        """
        out = await audit_returning("garbage, not a list", round_number=1)
        assert NOT_READY in out
        assert "malformed" in out
        assert "No findings." not in out

    async def test_the_verdict_is_logged_structured(
        self, audit_returning, monkeypatch
    ) -> None:
        """Cap conversions must be visible to a future postmortem.

        The module logger is patched directly: nexus caches structlog
        loggers on first use, so ``structlog.testing.capture_logs``
        cannot intercept the already-bound ``_log``.
        """
        import nexus.mcp.core as core_mod

        infos: list[tuple[str, dict]] = []

        class _FakeLog:
            def info(self, event: str, **fields) -> None:
                infos.append((event, fields))

        monkeypatch.setattr(core_mod, "_log", _FakeLog())
        await audit_returning(
            [{"classification": BLOCKS_PLANNING, "title": "x"}],
            round_number=MAX_BLOCKING_ROUNDS + 1,
        )
        events = [f for e, f in infos if e == "nx_plan_audit_verdict"]
        assert len(events) == 1
        assert events[0]["verdict"] == RESIDUALS_ONLY
        assert events[0]["cap_converted"] is True


@pytest.mark.asyncio
class TestPromptAndSchema:
    async def test_the_schema_requires_a_classification(self, audit_returning) -> None:
        await audit_returning([], round_number=1)
        item = audit_returning.last_schema["properties"]["findings"]["items"]
        assert item["required"] == ["classification"]
        assert set(item["properties"]["classification"]["enum"]) == {
            BLOCKS_PLANNING,
            DISCOVER_AT_IMPLEMENTATION,
        }

    async def test_the_prompt_carries_both_contracts(self, audit_returning) -> None:
        await audit_returning([], round_number=1)
        prompt = audit_returning.last_prompt
        assert BLOCKS_PLANNING in prompt
        assert DISCOVER_AT_IMPLEMENTATION in prompt
        assert "round 1" in prompt

    async def test_past_the_cap_the_prompt_says_so(self, audit_returning) -> None:
        await audit_returning([], round_number=MAX_BLOCKING_ROUNDS + 1)
        assert "do not argue for another round" in audit_returning.last_prompt
