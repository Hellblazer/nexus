# SPDX-License-Identifier: AGPL-3.0-or-later
"""TDD tests for nexus operator dispatch — clean async claude -p subprocess.

Design contract:
  * One async function `claude_dispatch(prompt, json_schema, timeout)` in
    `nexus.operators.dispatch`.
  * Calls `asyncio.create_subprocess_exec("claude", "-p", ...)` — never
    `subprocess.run`, never `subprocess.Popen`.
  * No auth check: claude -p inherits Claude Code auth; checking is
    redundant and blocks the event loop.
  * Prompt delivered via stdin; stdout is parsed as JSON.
  * Five MCP operator tools in `nexus.mcp.core`:
    operator_extract, operator_rank, operator_compare,
    operator_summarize, operator_generate.
  * Each tool composes a prompt, calls claude_dispatch, returns typed dict.
  * No pool, no session management, no warm-worker lifecycle.

All tests run without network or claude CLI — subprocess is mocked.
"""
from __future__ import annotations

import asyncio
import importlib
import json
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest


# ── Helpers ────────────────────────────────────────────────────────────────

def _make_proc(stdout: bytes = b'{"ok": true}', returncode: int = 0,
               stderr: bytes = b'') -> MagicMock:
    """Return a mock asyncio.subprocess.Process."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.kill = MagicMock()
    return proc


_SIMPLE_SCHEMA = {"type": "object", "properties": {"result": {"type": "string"}}}


# ── Event-loop safety ──────────────────────────────────────────────────────

class TestEventLoopSafety:
    """The test that would have caught the entire fiasco."""

    @pytest.mark.asyncio
    async def test_dispatch_does_not_block_event_loop(self) -> None:
        """Other coroutines must run while dispatch awaits subprocess I/O.

        If the implementation calls subprocess.run() or any other blocking
        call, the counter task never ticks and this assertion fails.
        """
        from nexus.operators.dispatch import claude_dispatch

        ticks = 0

        async def counter() -> None:
            nonlocal ticks
            for _ in range(50):
                ticks += 1
                await asyncio.sleep(0)

        async def yielding_communicate(input: bytes | None = None):  # noqa: A002
            # Simulate I/O wait that yields the event loop.
            await asyncio.sleep(0.005)
            return b'{"result": "ok"}', b''

        proc = _make_proc()
        proc.communicate = yielding_communicate

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            counter_task = asyncio.create_task(counter())
            await claude_dispatch("prompt", _SIMPLE_SCHEMA)
            await counter_task

        assert ticks > 0, (
            "Event loop was blocked during dispatch — counter never ticked. "
            "Ensure subprocess.run / blocking calls are NOT used."
        )

    @pytest.mark.asyncio
    async def test_never_calls_subprocess_run(self) -> None:
        """subprocess.run must never be called — it blocks the event loop."""
        import subprocess as _subprocess
        from nexus.operators.dispatch import claude_dispatch

        sync_calls: list = []
        proc = _make_proc()

        with patch("subprocess.run", side_effect=lambda *a, **kw: sync_calls.append(a)), \
             patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            await claude_dispatch("prompt", _SIMPLE_SCHEMA)

        assert not sync_calls, (
            f"subprocess.run was called {len(sync_calls)} time(s) — blocks event loop"
        )

    @pytest.mark.asyncio
    async def test_never_calls_subprocess_popen(self) -> None:
        """subprocess.Popen must never be called — it blocks the event loop."""
        import subprocess as _subprocess
        from nexus.operators.dispatch import claude_dispatch

        popen_calls: list = []
        proc = _make_proc()

        with patch("subprocess.Popen", side_effect=lambda *a, **kw: popen_calls.append(a)), \
             patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            await claude_dispatch("prompt", _SIMPLE_SCHEMA)

        assert not popen_calls, "subprocess.Popen was called — blocks event loop"


# ── Auth check ─────────────────────────────────────────────────────────────

class TestNoAuthCheck:
    """claude -p inherits Claude Code auth. No pre-flight check needed."""

    @pytest.mark.asyncio
    async def test_no_claude_auth_status_subprocess(self) -> None:
        """claude auth status must never be invoked."""
        from nexus.operators.dispatch import claude_dispatch

        auth_invocations: list = []
        proc = _make_proc()

        async def intercept(*args, **kwargs):
            if len(args) >= 2 and "auth" in str(args):
                auth_invocations.append(args)
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=intercept):
            await claude_dispatch("prompt", _SIMPLE_SCHEMA)

        assert not auth_invocations, (
            "claude auth status was invoked — redundant and blocks event loop"
        )

    def test_import_calls_no_subprocess(self) -> None:
        """Importing the module must not spawn any process."""
        import subprocess as _subprocess

        spawned: list = []
        _orig_run = _subprocess.run

        def trap(*args, **kwargs):
            spawned.append(args)
            return _orig_run(*args, **kwargs)

        _subprocess.run = trap
        try:
            import nexus.operators.dispatch as _mod
            importlib.reload(_mod)
        finally:
            _subprocess.run = _orig_run

        assert not spawned, f"subprocess.run called on import: {spawned}"


# ── Subprocess invocation contract ────────────────────────────────────────

class TestSubprocessContract:

    @pytest.mark.asyncio
    async def test_calls_create_subprocess_exec(self) -> None:
        """asyncio.create_subprocess_exec must be the sole spawn path."""
        from nexus.operators.dispatch import claude_dispatch

        proc = _make_proc()
        captured: list = []

        async def intercept(*args, **kwargs):
            captured.append((args, kwargs))
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=intercept):
            await claude_dispatch("prompt", _SIMPLE_SCHEMA)

        assert captured, "asyncio.create_subprocess_exec was never called"

    @pytest.mark.asyncio
    async def test_first_arg_is_claude(self) -> None:
        """Subprocess must invoke the `claude` executable."""
        from nexus.operators.dispatch import claude_dispatch

        proc = _make_proc()
        captured: list = []

        async def intercept(*args, **kwargs):
            captured.append(args)
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=intercept):
            await claude_dispatch("prompt", _SIMPLE_SCHEMA)

        assert captured[0][0] == "claude", (
            f"Expected first arg 'claude', got {captured[0][0]!r}"
        )

    @pytest.mark.asyncio
    async def test_sets_nx_session_id_env_from_current_session(self) -> None:
        """claude_dispatch must read the parent's UUID from current_session
        and export it as NX_SESSION_ID for the subprocess. Without this, the
        subprocess's SessionStart hook can't tell it's a nested call and
        will stomp the parent's current_session pointer.
        """
        from nexus.operators.dispatch import claude_dispatch

        proc = _make_proc()
        captured: list = []

        async def intercept(*args, **kwargs):
            captured.append(kwargs)
            return proc

        with (
            patch("asyncio.create_subprocess_exec", side_effect=intercept),
            patch("nexus.session.read_claude_session_id", return_value="parent-uuid-from-flat-file"),
        ):
            await claude_dispatch("prompt", _SIMPLE_SCHEMA)

        env = captured[0].get("env")
        assert env is not None
        assert env.get("NX_SESSION_ID") == "parent-uuid-from-flat-file", (
            f"NX_SESSION_ID missing or wrong; got {env.get('NX_SESSION_ID')!r}"
        )

    @pytest.mark.asyncio
    async def test_omits_nx_session_id_when_no_parent_session(self) -> None:
        """When there's no parent session (e.g. CLI usage outside Claude Code),
        claude_dispatch must not export an empty/None NX_SESSION_ID — the
        subprocess's hook would treat that as 'top-level' anyway, but exporting
        a junk value risks confusion.
        """
        from nexus.operators.dispatch import claude_dispatch

        proc = _make_proc()
        captured: list = []

        async def intercept(*args, **kwargs):
            captured.append(kwargs)
            return proc

        with (
            patch("asyncio.create_subprocess_exec", side_effect=intercept),
            patch("nexus.session.read_claude_session_id", return_value=None),
        ):
            await claude_dispatch("prompt", _SIMPLE_SCHEMA)

        env = captured[0].get("env")
        assert env is not None
        assert "NX_SESSION_ID" not in env or not env.get("NX_SESSION_ID")

    @pytest.mark.asyncio
    async def test_sets_skip_t1_env(self) -> None:
        """claude_dispatch must export NEXUS_SKIP_T1=1 in the subprocess env so
        the spawned `claude -p`'s nx SessionStart hook does not spin up a chroma
        T1 server. Operator dispatch is stateless — paying the chroma startup
        cost on every call would be pure waste, and the subprocess's T1 client
        falls back to EphemeralClient when no server record is found.
        """
        from nexus.operators.dispatch import claude_dispatch

        proc = _make_proc()
        captured: list = []

        async def intercept(*args, **kwargs):
            captured.append(kwargs)
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=intercept):
            await claude_dispatch("prompt", _SIMPLE_SCHEMA)

        env = captured[0].get("env")
        assert env is not None, "subprocess must be spawned with explicit env (got default inherit)"
        assert env.get("NEXUS_SKIP_T1") == "1", (
            f"NEXUS_SKIP_T1=1 missing from subprocess env; got NEXUS_SKIP_T1={env.get('NEXUS_SKIP_T1')!r}"
        )

    @pytest.mark.asyncio
    async def test_includes_p_flag(self) -> None:
        """Must pass -p flag to invoke non-interactive mode."""
        from nexus.operators.dispatch import claude_dispatch

        proc = _make_proc()
        captured: list = []

        async def intercept(*args, **kwargs):
            captured.append(args)
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=intercept):
            await claude_dispatch("prompt", _SIMPLE_SCHEMA)

        assert "-p" in captured[0], f"'-p' flag missing from {captured[0]}"

    @pytest.mark.asyncio
    async def test_prompt_sent_via_stdin(self) -> None:
        """Prompt must be passed as stdin bytes, not as a positional CLI arg."""
        from nexus.operators.dispatch import claude_dispatch

        proc = _make_proc()

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            await claude_dispatch("my unique prompt text", _SIMPLE_SCHEMA)

        proc.communicate.assert_called_once()
        stdin_bytes = proc.communicate.call_args[0][0]
        assert b"my unique prompt text" in stdin_bytes, (
            "Prompt not found in stdin bytes passed to communicate()"
        )

    @pytest.mark.asyncio
    async def test_returns_parsed_json(self) -> None:
        """Return value must be the parsed JSON dict from stdout."""
        from nexus.operators.dispatch import claude_dispatch

        payload = {"extractions": [{"title": "Foo"}]}
        proc = _make_proc(stdout=json.dumps(payload).encode())

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            result = await claude_dispatch("prompt", _SIMPLE_SCHEMA)

        assert result == payload


# ── Error handling ─────────────────────────────────────────────────────────

class TestErrorHandling:

    @pytest.mark.asyncio
    async def test_timeout_kills_process_and_raises(self) -> None:
        """Timeout must kill the subprocess and raise OperatorTimeoutError."""
        from nexus.operators.dispatch import claude_dispatch, OperatorTimeoutError

        proc = MagicMock()
        proc.kill = MagicMock()

        async def hang(input: bytes | None = None):  # noqa: A002
            await asyncio.sleep(999)
            return b'', b''

        proc.communicate = hang

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            with pytest.raises(OperatorTimeoutError):
                await claude_dispatch("prompt", _SIMPLE_SCHEMA, timeout=0.01)

        proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_nonzero_exit_raises_operator_error(self) -> None:
        """Non-zero returncode raises OperatorError containing stderr text."""
        from nexus.operators.dispatch import claude_dispatch, OperatorError

        proc = _make_proc(stdout=b'', returncode=1, stderr=b'rate limit exceeded')

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            with pytest.raises(OperatorError, match="rate limit exceeded"):
                await claude_dispatch("prompt", _SIMPLE_SCHEMA)

    @pytest.mark.asyncio
    async def test_malformed_json_raises_operator_output_error(self) -> None:
        """Unparseable stdout raises OperatorOutputError."""
        from nexus.operators.dispatch import claude_dispatch, OperatorOutputError

        proc = _make_proc(stdout=b'not valid json {{{{')

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            with pytest.raises(OperatorOutputError):
                await claude_dispatch("prompt", _SIMPLE_SCHEMA)

    @pytest.mark.asyncio
    async def test_empty_stdout_raises_operator_output_error(self) -> None:
        """Empty stdout raises OperatorOutputError, not JSONDecodeError."""
        from nexus.operators.dispatch import claude_dispatch, OperatorOutputError

        proc = _make_proc(stdout=b'')

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            with pytest.raises(OperatorOutputError):
                await claude_dispatch("prompt", _SIMPLE_SCHEMA)


# ── MCP operator tools ─────────────────────────────────────────────────────

@pytest.fixture
def mock_dispatch(monkeypatch):
    """Patch claude_dispatch in nexus.operators.dispatch and return a recorder."""
    import nexus.operators.dispatch as _dispatch_mod

    calls: list[dict] = []

    async def fake_dispatch(prompt: str, schema: dict, timeout: float = 60.0) -> dict:
        calls.append({"prompt": prompt, "schema": schema, "timeout": timeout})
        # Return a minimal valid payload that each tool can parse.
        return _FAKE_PAYLOADS.get("_default", {"ok": True})

    monkeypatch.setattr(_dispatch_mod, "claude_dispatch", fake_dispatch)
    return calls


_FAKE_PAYLOADS: dict[str, dict] = {
    "_default": {"ok": True},
}


class TestOperatorExtract:

    @pytest.mark.asyncio
    async def test_returns_extractions_key(self, monkeypatch) -> None:
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import operator_extract

        async def fake(*a, **kw):
            return {"extractions": [{"title": "Alpha"}]}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        result = await operator_extract(inputs='["text one"]', fields="title")
        assert "extractions" in result
        assert isinstance(result["extractions"], list)

    @pytest.mark.asyncio
    async def test_prompt_contains_fields(self, monkeypatch) -> None:
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import operator_extract

        captured: list[str] = []

        async def fake(prompt, schema, timeout=60.0):
            captured.append(prompt)
            return {"extractions": []}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        await operator_extract(inputs="some text", fields="author,year")

        assert captured, "claude_dispatch never called"
        assert "author" in captured[0]
        assert "year" in captured[0]

    @pytest.mark.asyncio
    async def test_prompt_contains_inputs(self, monkeypatch) -> None:
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import operator_extract

        captured: list[str] = []

        async def fake(prompt, schema, timeout=60.0):
            captured.append(prompt)
            return {"extractions": []}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        await operator_extract(inputs="unique sentinel value abc123", fields="x")

        assert "unique sentinel value abc123" in captured[0]


class TestOperatorRank:

    @pytest.mark.asyncio
    async def test_returns_ranked_key(self, monkeypatch) -> None:
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import operator_rank

        async def fake(*a, **kw):
            return {"ranked": ["b", "a"]}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        result = await operator_rank(items='["a", "b"]', criterion="relevance")
        assert "ranked" in result

    @pytest.mark.asyncio
    async def test_prompt_contains_criterion(self, monkeypatch) -> None:
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import operator_rank

        captured: list[str] = []

        async def fake(prompt, schema, timeout=60.0):
            captured.append(prompt)
            return {"ranked": []}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        await operator_rank(items='["x"]', criterion="novelty")
        assert "novelty" in captured[0]


class TestOperatorCompare:

    @pytest.mark.asyncio
    async def test_returns_comparison_key(self, monkeypatch) -> None:
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import operator_compare

        async def fake(*a, **kw):
            return {"comparison": "A is better"}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        result = await operator_compare(items='["A", "B"]')
        assert "comparison" in result

    @pytest.mark.asyncio
    async def test_one_sided_prompt_uses_items(self, monkeypatch) -> None:
        """One-sided compare (only items) keeps the original prompt shape."""
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import operator_compare

        captured = {}

        async def fake(prompt, schema, timeout):
            captured["prompt"] = prompt
            return {"comparison": "ok"}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        await operator_compare(items='["A", "B"]', focus="hotness")
        assert "Compare the following items" in captured["prompt"]
        assert "Focus on: hotness" in captured["prompt"]
        assert "Items:" in captured["prompt"]
        # Two-sided markers must NOT appear in one-sided mode.
        assert "Set A:" not in captured["prompt"]
        assert "Shared axes" not in captured["prompt"]

    @pytest.mark.asyncio
    async def test_two_sided_prompt_when_both_items_ab_given(self, monkeypatch) -> None:
        """items_a + items_b switches to the cross-corpus compare prompt."""
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import operator_compare

        captured = {}

        async def fake(prompt, schema, timeout):
            captured["prompt"] = prompt
            return {"comparison": "cross"}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        await operator_compare(
            items_a=[{"rdr": "A-001", "decision": "alpha"}],
            items_b=[{"rdr": "B-001", "decision": "beta"}],
            label_a="Arcaneum",
            label_b="Nexus",
            focus="bulk indexing",
        )
        p = captured["prompt"]
        assert "Compare two sets of items" in p
        assert "Set Arcaneum:" in p
        assert "Set Nexus:" in p
        assert "Shared axes" in p
        assert "Divergent decisions" in p
        assert "Philosophy difference" in p
        assert "Focus on: bulk indexing" in p

    @pytest.mark.asyncio
    async def test_list_items_json_serialized_in_prompt(self, monkeypatch) -> None:
        """List args render as clean JSON, not Python repr."""
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import operator_compare

        captured = {}

        async def fake(prompt, schema, timeout):
            captured["prompt"] = prompt
            return {"comparison": "ok"}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        await operator_compare(items=[{"name": "A"}, {"name": "B"}])
        # JSON double-quotes instead of Python single-quote repr.
        assert '"name"' in captured["prompt"]
        assert "'name'" not in captured["prompt"]

    @pytest.mark.asyncio
    async def test_triple_empty_items_falls_into_one_sided_empty_prompt(
        self, monkeypatch,
    ) -> None:
        """All three item parameters empty produces a one-sided prompt with an
        empty Items body. This pins the silent-empty contract documented in
        the operator_compare docstring (code-review finding T-2); callers who
        rely on ``items`` being required previously got a TypeError, and this
        test makes the new default-empty behaviour explicit so a future change
        to add an early-exit raise is caught here."""
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import operator_compare

        captured = {}

        async def fake(prompt, schema, timeout):
            captured["prompt"] = prompt
            return {"comparison": ""}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        result = await operator_compare()
        assert "comparison" in result
        # One-sided format with empty Items body.
        assert "Compare the following items" in captured["prompt"]
        assert captured["prompt"].rstrip().endswith("Items:")

    @pytest.mark.asyncio
    async def test_one_sided_fires_when_items_b_empty(self, monkeypatch) -> None:
        """Only one of items_a/items_b given falls back to one-sided on items."""
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import operator_compare

        captured = {}

        async def fake(prompt, schema, timeout):
            captured["prompt"] = prompt
            return {"comparison": "ok"}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        # items_a provided but items_b empty → degrade to one-sided on items.
        await operator_compare(items='["fallback"]', items_a="only-a", items_b="")
        assert "Compare the following items" in captured["prompt"]
        assert "fallback" in captured["prompt"]
        assert "Set A:" not in captured["prompt"]


class TestOperatorSummarize:

    @pytest.mark.asyncio
    async def test_returns_summary_key(self, monkeypatch) -> None:
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import operator_summarize

        async def fake(*a, **kw):
            return {"summary": "Short summary.", "citations": []}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        result = await operator_summarize(content="Long content here.")
        assert "summary" in result

    @pytest.mark.asyncio
    async def test_prompt_contains_content(self, monkeypatch) -> None:
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import operator_summarize

        captured: list[str] = []

        async def fake(prompt, schema, timeout=60.0):
            captured.append(prompt)
            return {"summary": "", "citations": []}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        await operator_summarize(content="sentinel content xyz")
        assert "sentinel content xyz" in captured[0]


class TestOperatorGenerate:

    @pytest.mark.asyncio
    async def test_returns_output_key(self, monkeypatch) -> None:
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import operator_generate

        async def fake(*a, **kw):
            return {"output": "Generated text.", "citations": []}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        result = await operator_generate(template="synthesis", context="some context")
        assert "output" in result

    @pytest.mark.asyncio
    async def test_prompt_contains_template_and_context(self, monkeypatch) -> None:
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import operator_generate

        captured: list[str] = []

        async def fake(prompt, schema, timeout=60.0):
            captured.append(prompt)
            return {"output": "", "citations": []}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        await operator_generate(template="executive-summary", context="sentinel ctx abc")
        assert "sentinel ctx abc" in captured[0]
        assert "executive-summary" in captured[0]


class TestOperatorFilter:
    """RDR-088 Phase 1: operator_filter returns a subset of input items
    with per-item rationale explaining the keep/reject decision."""

    @pytest.mark.asyncio
    async def test_returns_items_and_rationale_keys(self, monkeypatch) -> None:
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import operator_filter

        async def fake(*a, **kw):
            return {
                "items": [{"id": "a", "title": "Alpha"}],
                "rationale": [
                    {"id": "a", "reason": "satisfies criterion"},
                    {"id": "b", "reason": "rejected: off-topic"},
                ],
            }

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        result = await operator_filter(
            items='[{"id": "a"}, {"id": "b"}]',
            criterion="on-topic",
        )
        assert "items" in result
        assert "rationale" in result
        assert isinstance(result["items"], list)
        assert isinstance(result["rationale"], list)

    @pytest.mark.asyncio
    async def test_prompt_contains_criterion(self, monkeypatch) -> None:
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import operator_filter

        captured: list[str] = []

        async def fake(prompt, schema, timeout=60.0):
            captured.append(prompt)
            return {"items": [], "rationale": []}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        await operator_filter(items='[]', criterion="peer-reviewed-only-sentinel")
        assert "peer-reviewed-only-sentinel" in captured[0]

    @pytest.mark.asyncio
    async def test_prompt_contains_items(self, monkeypatch) -> None:
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import operator_filter

        captured: list[str] = []

        async def fake(prompt, schema, timeout=60.0):
            captured.append(prompt)
            return {"items": [], "rationale": []}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        await operator_filter(
            items='[{"id": "sentinel-item-xyz789"}]',
            criterion="relevant",
        )
        assert "sentinel-item-xyz789" in captured[0]

    @pytest.mark.asyncio
    async def test_schema_declares_items_and_rationale(self, monkeypatch) -> None:
        """Schema must declare both ``items`` and ``rationale`` so the
        substrate enforces the output shape before returning to the caller.
        Without schema declaration, malformed LLM output slips through
        and plan_run downstream steps trip on missing keys."""
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import operator_filter

        captured_schemas: list[dict] = []

        async def fake(prompt, schema, timeout=60.0):
            captured_schemas.append(schema)
            return {"items": [], "rationale": []}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        await operator_filter(items='[]', criterion="x")

        schema = captured_schemas[0]
        assert schema["type"] == "object"
        assert "items" in schema["required"]
        assert "rationale" in schema["required"]
        assert "items" in schema["properties"]
        assert "rationale" in schema["properties"]
        rationale_item_schema = schema["properties"]["rationale"]["items"]
        assert "id" in rationale_item_schema["required"]
        assert "reason" in rationale_item_schema["required"]

    @pytest.mark.asyncio
    async def test_ten_item_input_returns_subset_not_larger(self, monkeypatch) -> None:
        """RDR-088 Test Plan scenario 1: with 10 inputs, the returned
        items list must be <= input length. Mocked dispatch returns a
        realistic subset; asserting the contract makes future regressions
        (e.g. LLM returns duplicates, amplifies input) trip the test."""
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import operator_filter

        inputs = [{"id": f"item-{i}", "title": f"Item {i}"} for i in range(10)]

        async def fake(prompt, schema, timeout=60.0):
            kept = inputs[:4]
            rationale = [
                {"id": it["id"], "reason": "keeps criterion"} for it in kept
            ] + [
                {"id": it["id"], "reason": "rejects criterion"} for it in inputs[4:]
            ]
            return {"items": kept, "rationale": rationale}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        result = await operator_filter(
            items=json.dumps(inputs), criterion="even index",
        )
        assert len(result["items"]) <= len(inputs)
        assert len(result["rationale"]) == len(inputs)
        output_ids = {it["id"] for it in result["items"]}
        input_ids = {it["id"] for it in inputs}
        assert output_ids.issubset(input_ids), (
            f"operator_filter must return subset of input ids; "
            f"got extras {output_ids - input_ids}"
        )


class TestOperatorCheck:
    """RDR-088 Phase 2: operator_check returns a structured boolean plus
    grounding evidence across multiple items (paper §D.2 Check)."""

    @pytest.mark.asyncio
    async def test_returns_ok_and_evidence_keys(self, monkeypatch) -> None:
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import operator_check

        async def fake(*a, **kw):
            return {
                "ok": True,
                "evidence": [
                    {"item_id": "p1", "quote": "A B", "role": "supports"},
                ],
            }

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        result = await operator_check(
            items='[{"id": "p1"}]', check_instruction="claim X holds",
        )
        assert "ok" in result
        assert "evidence" in result
        assert isinstance(result["ok"], bool)
        assert isinstance(result["evidence"], list)

    @pytest.mark.asyncio
    async def test_prompt_contains_instruction_and_items(self, monkeypatch) -> None:
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import operator_check

        captured: list[str] = []

        async def fake(prompt, schema, timeout=60.0):
            captured.append(prompt)
            return {"ok": True, "evidence": []}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        await operator_check(
            items='[{"id": "sentinel-paper-abc"}]',
            check_instruction="consistency-probe-xyz",
        )
        assert "sentinel-paper-abc" in captured[0]
        assert "consistency-probe-xyz" in captured[0]

    @pytest.mark.asyncio
    async def test_schema_declares_ok_evidence_and_role_enum(self, monkeypatch) -> None:
        """Schema must pin the {item_id, quote, role} evidence shape
        and role must be restricted to the enum {supports, contradicts,
        neutral} per the RDR Technical Design. Without enum enforcement
        the LLM can emit a role like 'partial' that breaks downstream
        branching."""
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import operator_check

        captured_schemas: list[dict] = []

        async def fake(prompt, schema, timeout=60.0):
            captured_schemas.append(schema)
            return {"ok": True, "evidence": []}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        await operator_check(items='[]', check_instruction="x")

        schema = captured_schemas[0]
        assert "ok" in schema["required"]
        assert "evidence" in schema["required"]
        assert schema["properties"]["ok"]["type"] == "boolean"
        evidence_item = schema["properties"]["evidence"]["items"]
        assert set(evidence_item["required"]) == {"item_id", "quote", "role"}
        assert set(evidence_item["properties"]["role"]["enum"]) == {
            "supports", "contradicts", "neutral",
        }

    @pytest.mark.asyncio
    async def test_three_agreeing_papers_yield_ok_true(self, monkeypatch) -> None:
        """RDR-088 Test Plan scenario 2: 3 papers that agree on a claim
        yield ok=True with >=1 supporting quote per paper, no contradicts."""
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import operator_check

        async def fake(prompt, schema, timeout=60.0):
            return {
                "ok": True,
                "evidence": [
                    {"item_id": "p1", "quote": "agrees-1",
                     "role": "supports"},
                    {"item_id": "p2", "quote": "agrees-2",
                     "role": "supports"},
                    {"item_id": "p3", "quote": "agrees-3",
                     "role": "supports"},
                ],
            }

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        result = await operator_check(
            items='[{"id": "p1"}, {"id": "p2"}, {"id": "p3"}]',
            check_instruction="papers agree on baseline",
        )
        assert result["ok"] is True
        paper_ids = {e["item_id"] for e in result["evidence"]}
        assert paper_ids == {"p1", "p2", "p3"}
        roles = {e["role"] for e in result["evidence"]}
        assert "contradicts" not in roles

    @pytest.mark.asyncio
    async def test_contradicting_paper_yields_ok_false(self, monkeypatch) -> None:
        """RDR-088 Test Plan scenario 3: when 1 of 3 papers contradicts
        the claim, ok=False and the contradicting quote must be surfaced
        with role=contradicts."""
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import operator_check

        async def fake(prompt, schema, timeout=60.0):
            return {
                "ok": False,
                "evidence": [
                    {"item_id": "p1", "quote": "supports-1",
                     "role": "supports"},
                    {"item_id": "p2", "quote": "supports-2",
                     "role": "supports"},
                    {"item_id": "p3", "quote": "contradicts-sentinel",
                     "role": "contradicts"},
                ],
            }

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        result = await operator_check(
            items='[{"id": "p1"}, {"id": "p2"}, {"id": "p3"}]',
            check_instruction="all papers report same numbers",
        )
        assert result["ok"] is False
        contradicts = [
            e for e in result["evidence"] if e["role"] == "contradicts"
        ]
        assert len(contradicts) == 1
        assert contradicts[0]["item_id"] == "p3"
        assert contradicts[0]["quote"] == "contradicts-sentinel"


class TestOperatorVerify:
    """RDR-088 Phase 2: operator_verify targets a single claim against
    a single evidence blob (paper §D.2 Verify)."""

    @pytest.mark.asyncio
    async def test_returns_verified_reason_citations(self, monkeypatch) -> None:
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import operator_verify

        async def fake(*a, **kw):
            return {
                "verified": True,
                "reason": "grounded in §2.1",
                "citations": ["§2.1, p.3"],
            }

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        result = await operator_verify(
            claim="X is a transformer variant",
            evidence="Section 2.1: X is built on transformer layers...",
        )
        assert result["verified"] is True
        assert isinstance(result["reason"], str)
        assert isinstance(result["citations"], list)

    @pytest.mark.asyncio
    async def test_prompt_contains_claim_and_evidence(self, monkeypatch) -> None:
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import operator_verify

        captured: list[str] = []

        async def fake(prompt, schema, timeout=60.0):
            captured.append(prompt)
            return {"verified": False, "reason": "", "citations": []}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        await operator_verify(
            claim="claim-sentinel-abc",
            evidence="evidence-sentinel-xyz",
        )
        assert "claim-sentinel-abc" in captured[0]
        assert "evidence-sentinel-xyz" in captured[0]

    @pytest.mark.asyncio
    async def test_schema_declares_verified_reason_citations(
        self, monkeypatch,
    ) -> None:
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import operator_verify

        captured_schemas: list[dict] = []

        async def fake(prompt, schema, timeout=60.0):
            captured_schemas.append(schema)
            return {"verified": False, "reason": "", "citations": []}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        await operator_verify(claim="c", evidence="e")

        schema = captured_schemas[0]
        assert schema["properties"]["verified"]["type"] == "boolean"
        assert {"verified", "reason", "citations"}.issubset(
            set(schema["required"]),
        )
        assert schema["properties"]["citations"]["type"] == "array"

    @pytest.mark.asyncio
    async def test_grounded_claim_verified_with_citations(
        self, monkeypatch,
    ) -> None:
        """RDR-088 Test Plan scenario 4: a claim that IS grounded in the
        evidence returns verified=True with at least one citation span."""
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import operator_verify

        async def fake(prompt, schema, timeout=60.0):
            return {
                "verified": True,
                "reason": "quote-at-p3-matches-claim",
                "citations": ["p.3, §2", "Table 1"],
            }

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        result = await operator_verify(
            claim="X uses attention",
            evidence="Table 1 compares attention variants...",
        )
        assert result["verified"] is True
        assert len(result["citations"]) >= 1


class TestOperatorGroupby:
    """RDR-093 Phase 1: operator_groupby partitions a flat list of items
    into N groups keyed by a natural-language partition expression. C-1
    inline-items contract: each emitted group's ``items`` carries the
    full input dicts inline (not id-only references) so a downstream
    bundled aggregate sees resolvable content."""

    @pytest.mark.asyncio
    async def test_returns_groups_with_key_value_and_items(self, monkeypatch) -> None:
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import operator_groupby

        async def fake(*a, **kw):
            return {
                "groups": [
                    {"key_value": "2018", "items": [{"id": "a", "year": 2018}]},
                    {"key_value": "2020", "items": [{"id": "b", "year": 2020}]},
                ],
            }

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        result = await operator_groupby(
            items='[{"id": "a", "year": 2018}, {"id": "b", "year": 2020}]',
            key="publication year",
        )
        assert "groups" in result
        assert isinstance(result["groups"], list)
        for g in result["groups"]:
            assert "key_value" in g and "items" in g
            assert isinstance(g["key_value"], str)
            assert isinstance(g["items"], list)

    @pytest.mark.asyncio
    async def test_prompt_contains_key_and_items(self, monkeypatch) -> None:
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import operator_groupby

        captured: list[str] = []

        async def fake(prompt, schema, timeout=60.0):
            captured.append(prompt)
            return {"groups": []}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        await operator_groupby(
            items='[{"id": "sentinel-item-groupby"}]',
            key="partition-by-sentinel-key",
        )
        assert "sentinel-item-groupby" in captured[0]
        assert "partition-by-sentinel-key" in captured[0]

    @pytest.mark.asyncio
    async def test_schema_pins_inline_items_contract(self, monkeypatch) -> None:
        """C-1 contract guard at the unit level: the JSON schema must
        require an ``items`` array of objects on each group, not a
        bare list of strings (which would be the historical id-only
        shape from the pre-gate design). Reverting to id-references
        breaks this assertion."""
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import operator_groupby

        captured_schemas: list[dict] = []

        async def fake(prompt, schema, timeout=60.0):
            captured_schemas.append(schema)
            return {"groups": []}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        await operator_groupby(items='[]', key="x")

        schema = captured_schemas[0]
        assert schema["type"] == "object"
        assert "groups" in schema["required"]
        groups_item = schema["properties"]["groups"]["items"]
        assert {"key_value", "items"}.issubset(set(groups_item["required"]))
        assert groups_item["properties"]["key_value"]["type"] == "string"
        assert groups_item["properties"]["items"]["type"] == "array"
        # The inner items are objects (dicts), not strings — this is the
        # C-1 inline-items contract.
        assert groups_item["properties"]["items"]["items"]["type"] == "object"

    @pytest.mark.asyncio
    async def test_unassigned_group_collects_low_confidence_items(
        self, monkeypatch,
    ) -> None:
        """RDR-093 Test Plan scenario 2: ambiguous inputs land in
        ``key_value="unassigned"`` rather than being force-fit. Plan
        authors inspect unassigned size as a quality signal."""
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import operator_groupby

        async def fake(prompt, schema, timeout=60.0):
            return {
                "groups": [
                    {"key_value": "2018",
                     "items": [{"id": "a", "year": 2018}]},
                    {"key_value": "unassigned",
                     "items": [{"id": "b", "no_year": True}]},
                ],
            }

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        result = await operator_groupby(
            items='[{"id": "a", "year": 2018}, {"id": "b"}]',
            key="publication year",
        )
        unassigned = [
            g for g in result["groups"] if g["key_value"] == "unassigned"
        ]
        assert len(unassigned) == 1
        assert any(it["id"] == "b" for it in unassigned[0]["items"])

    @pytest.mark.asyncio
    async def test_inline_items_contract_dicts_not_id_strings(
        self, monkeypatch,
    ) -> None:
        """C-1 regression guard at unit scope: a group's ``items``
        contains dicts (preserving the input's id+content), NOT bare
        id strings. The pre-gate design carried only ids and the
        bundle path could not resolve them. If a future change reverts
        groupby to id-references, this test fails."""
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import operator_groupby

        async def fake(*a, **kw):
            return {
                "groups": [
                    {"key_value": "yes",
                     "items": [{"id": "a", "body": "first item body"}]},
                    {"key_value": "no",
                     "items": [{"id": "b", "body": "second item body"}]},
                ],
            }

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        result = await operator_groupby(
            items='[{"id": "a", "body": "first item body"}, '
                  '{"id": "b", "body": "second item body"}]',
            key="some axis",
        )
        for g in result["groups"]:
            for it in g["items"]:
                assert isinstance(it, dict), (
                    "C-1 contract: group items must be dicts (inline), "
                    "not id-only strings"
                )
                assert "id" in it
