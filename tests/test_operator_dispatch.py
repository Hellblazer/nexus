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
