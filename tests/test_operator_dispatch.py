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

def _one_shot_reader(data: bytes):
    """Async ``read(n)``-shaped callable: returns *data* whole on the
    first call, then ``b""`` (EOF) on every call after."""
    state = {"served": False}

    async def _read(n: int = -1) -> bytes:
        if state["served"]:
            return b""
        state["served"] = True
        return data

    return _read


def _make_proc(stdout: bytes = b'{"ok": true}', returncode: int = 0,
               stderr: bytes = b'') -> MagicMock:
    """Return a mock asyncio.subprocess.Process.

    Exposes the stdin/stdout/stderr streaming interface claude_dispatch's
    manual drain loop uses (``_feed_stdin`` / ``_drain_stream``) -- NOT
    ``.communicate()``. nexus-h33x8.6 a3 found that ``communicate()``'s
    internal ``read(-1)`` loop buffers into a coroutine-local list that a
    ``wait_for()``-driven timeout cancellation discards (and the bytes
    are already popped out of the StreamReader's own buffer by then
    too), which made the nexus-1at5 timeout drain structurally empty
    regardless of output format. claude_dispatch now reads via its own
    accumulator that survives cancellation, so the mock speaks that
    interface instead.
    """
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock(return_value=None)
    proc.stdin.close = MagicMock()
    proc.stdout = MagicMock()
    proc.stdout.read = _one_shot_reader(stdout)
    proc.stderr = MagicMock()
    proc.stderr.read = _one_shot_reader(stderr)
    proc.wait = AsyncMock(return_value=returncode)
    proc.kill = MagicMock()
    # A handful of tests still stub .communicate directly for scenarios
    # written before the manual drain loop existed; harmless to leave
    # wired since nothing in claude_dispatch calls it any more.
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


_SIMPLE_SCHEMA = {"type": "object", "properties": {"result": {"type": "string"}}}


@pytest.fixture(autouse=True)
def _fake_t1_dispatch_mint(monkeypatch):
    """Module-wide default: stub the T1 mint every ``ephemeral=True``
    dispatch now performs (nexus-4lkmz decision 1), so this file's own
    stated contract -- "All tests run without network or claude CLI --
    subprocess is mocked" -- holds for T1 session minting too. Tests that
    care about the mint call itself (``TestBuildDispatchEnvStripsT1Session``)
    override this locally via their own ``monkeypatch.setattr`` (later call
    on the same fixture instance wins)."""
    monkeypatch.setattr(
        "nexus.db.t1.mint_t1_session_token",
        lambda session_id, *, context: {"session_token": f"fake-dispatch-tok-{session_id}"},
    )


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

        proc = _make_proc()
        served = {"n": 0}

        async def yielding_stdout_read(n: int = -1) -> bytes:
            # Simulate I/O wait that yields the event loop.
            if served["n"] == 0:
                served["n"] += 1
                await asyncio.sleep(0.005)
                return b'{"result": "ok"}'
            return b""

        proc.stdout.read = yielding_stdout_read

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


# ── Opt-in tool access (Fix B, nexus-mawqw) ────────────────────────────────

class TestOptInToolAccess:
    """Stateless operators stay tool-free by default. Agent-replacement
    tools (nx_enrich_beads, nx_plan_audit) opt in to MCP + built-in tools
    by passing ``allowed_tools`` and ``mcp_servers``. Without this, the
    child claude -p sees the conexus MCP server as unapproved (post-CC
    2.1.162) and every tool call is denied.
    """

    @pytest.mark.asyncio
    async def test_default_dispatch_passes_no_tool_flags(self) -> None:
        """The stateless default must NOT pass --allowedTools or
        --mcp-config. Adding blanket tool access to every operator would
        regress the stateless-tool-free invariant."""
        from nexus.operators.dispatch import claude_dispatch

        proc = _make_proc()
        captured: list = []

        async def intercept(*args, **kwargs):
            captured.append(args)
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=intercept):
            await claude_dispatch("prompt", _SIMPLE_SCHEMA)

        argv = captured[0]
        assert "--allowedTools" not in argv, "default dispatch must be tool-free"
        assert "--mcp-config" not in argv, "default dispatch must not inject MCP servers"
        # RDR-196 Gap 4 (nexus-0d, 2026-08-19): without --strict-mcp-config
        # the tool-free child still LOADS the user's entire MCP server set
        # (.mcp.json / user settings) -- measured ~92K context tokens and
        # 2x the dollars per operator call vs ~45K with a strict empty
        # config -- for a subprocess that has no tools to call them with.
        assert "--strict-mcp-config" in argv, (
            "tool-free dispatch must pass --strict-mcp-config so the child "
            "loads ZERO MCP servers (no --mcp-config + strict = none)"
        )

    @pytest.mark.asyncio
    async def test_allowed_tools_emits_flag(self) -> None:
        """Passing allowed_tools must emit --allowedTools with the
        comma-joined tool names."""
        from nexus.operators.dispatch import claude_dispatch

        proc = _make_proc()
        captured: list = []

        async def intercept(*args, **kwargs):
            captured.append(args)
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=intercept):
            await claude_dispatch(
                "prompt", _SIMPLE_SCHEMA,
                allowed_tools=["Read", "Grep", "mcp__nexus"],
            )

        argv = list(captured[0])
        assert "--allowedTools" in argv
        val = argv[argv.index("--allowedTools") + 1]
        assert val == "Read,Grep,mcp__nexus"

    @pytest.mark.asyncio
    async def test_mcp_servers_emits_inline_config(self) -> None:
        """Passing mcp_servers must emit --mcp-config with an inline
        {"mcpServers": {...}} JSON payload. Servers passed via the flag
        are explicitly provided, so they clear the post-2.1.162
        pending-approval gate that blocks .mcp.json servers."""
        from nexus.operators.dispatch import claude_dispatch

        proc = _make_proc()
        captured: list = []

        async def intercept(*args, **kwargs):
            captured.append(args)
            return proc

        servers = {"nexus": {"command": "nx-mcp", "args": []}}
        with patch("asyncio.create_subprocess_exec", side_effect=intercept):
            await claude_dispatch(
                "prompt", _SIMPLE_SCHEMA, mcp_servers=servers,
            )

        argv = list(captured[0])
        assert "--mcp-config" in argv
        # Opt-in servers are the ONLY servers: strict keeps the user's
        # ambient .mcp.json / settings servers out of the child even when
        # a caller grants specific ones (RDR-196 Gap 4).
        assert "--strict-mcp-config" in argv
        cfg = json.loads(argv[argv.index("--mcp-config") + 1])
        assert cfg == {"mcpServers": {"nexus": {"command": "nx-mcp", "args": []}}}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("call_operator", [
        lambda: __import__("nexus.mcp.core", fromlist=["operator_extract"]).operator_extract(inputs="x", fields="y"),
        lambda: __import__("nexus.mcp.core", fromlist=["operator_rank"]).operator_rank(items='["a","b"]', criterion="rel"),
        lambda: __import__("nexus.mcp.core", fromlist=["operator_filter"]).operator_filter(items='[{"id":"a"}]', criterion="rel", source="llm"),
    ])
    async def test_stateless_operators_pass_no_tool_flags(self, call_operator) -> None:
        """Load-bearing invariant guard at the CONCRETE-operator layer
        (substantive-critic, nexus-mawqw). The dispatch-layer default test
        alone wouldn't catch an operator that accidentally started passing
        a tool grant. Drive the real operator through create_subprocess_exec
        and assert the argv carries no --allowedTools / --mcp-config."""
        proc = _make_proc(
            stdout=b'{"extractions": [], "ranked": [], "items": [], "rationale": []}'
        )
        captured: list = []

        async def intercept(*args, **kwargs):
            captured.append(args)
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=intercept):
            await call_operator()

        assert captured, "operator never reached create_subprocess_exec"
        argv = captured[0]
        assert "--allowedTools" not in argv, (
            "stateless operator leaked tool access — the tool-free invariant "
            "is broken"
        )
        assert "--mcp-config" not in argv, (
            "stateless operator injected an MCP server — must stay tool-free"
        )

    @pytest.mark.asyncio
    async def test_tool_enabled_dispatch_still_parses_json(self) -> None:
        """Opt-in tool flags must not change the output contract — the
        return value is still the parsed JSON dict."""
        from nexus.operators.dispatch import claude_dispatch

        payload = {"result": "ok"}
        proc = _make_proc(stdout=json.dumps(payload).encode())

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            result = await claude_dispatch(
                "prompt", _SIMPLE_SCHEMA,
                allowed_tools=["Read"],
                mcp_servers={"nexus": {"command": "nx-mcp", "args": []}},
            )

        assert result == payload


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
    async def test_tool_free_dispatch_never_mints_t1_session(self) -> None:
        """nexus-bjltu: the stateless tool-free default (no allowed_tools /
        mcp_servers -- the common case, ~15/17 call sites) must NEVER mint
        a T1 session. Nothing in a tool-free subprocess can reach T1, so
        minting would be dead weight and a needless single point of
        failure on the storage service."""
        from nexus.operators.dispatch import claude_dispatch

        proc = _make_proc()
        captured: list = []
        mint_calls: list = []

        async def intercept(*args, **kwargs):
            captured.append(kwargs)
            return proc

        def _fake_mint(session_id: str, *, context: str) -> dict:
            mint_calls.append(session_id)
            return {"session_token": f"tok-for-{session_id}", "expires_in_seconds": 3600}

        with (
            patch("asyncio.create_subprocess_exec", side_effect=intercept),
            patch("nexus.db.t1.mint_t1_session_token", side_effect=_fake_mint),
        ):
            await claude_dispatch("prompt", _SIMPLE_SCHEMA)

        assert mint_calls == [], "a tool-free dispatch must never mint a T1 session"
        env = captured[0].get("env")
        assert env is not None, "subprocess must be spawned with explicit env (got default inherit)"
        assert "NX_T1_ISOLATED" not in env, (
            "the retired isolated leg must never be set by claude_dispatch"
        )
        assert "NX_T1_SESSION_ID" not in env
        assert "NX_T1_SESSION" not in env

    @pytest.mark.asyncio
    async def test_tool_granted_dispatch_mints_own_t1_session(self) -> None:
        """A dispatch that DOES grant tool access (allowed_tools set) mints
        its own, freshly minted PG-backed T1 session (NX_T1_SESSION /
        NX_T1_SESSION_ID) -- the subprocess CAN reach T1 in this case, so
        the mint is not dead weight."""
        from nexus.operators.dispatch import claude_dispatch

        proc = _make_proc()
        captured: list = []

        async def intercept(*args, **kwargs):
            captured.append(kwargs)
            return proc

        def _fake_mint(session_id: str, *, context: str) -> dict:
            return {"session_token": f"tok-for-{session_id}", "expires_in_seconds": 3600}

        with (
            patch("asyncio.create_subprocess_exec", side_effect=intercept),
            patch("nexus.db.t1.mint_t1_session_token", side_effect=_fake_mint),
        ):
            await claude_dispatch(
                "prompt", _SIMPLE_SCHEMA, allowed_tools=["Read", "Grep"]
            )

        env = captured[0].get("env")
        assert env is not None, "subprocess must be spawned with explicit env (got default inherit)"
        assert "NX_T1_ISOLATED" not in env, (
            "the retired isolated leg must never be set by claude_dispatch"
        )
        assert env.get("NX_T1_SESSION_ID"), "must mint an own T1 session id"
        assert env.get("NX_T1_SESSION") == f"tok-for-{env['NX_T1_SESSION_ID']}"

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

        proc.stdin.write.assert_called_once()
        stdin_bytes = proc.stdin.write.call_args[0][0]
        assert b"my unique prompt text" in stdin_bytes, (
            "Prompt not found in stdin bytes passed to stdin.write()"
        )

    @pytest.mark.asyncio
    async def test_stdin_written_even_when_stdout_never_drains(self) -> None:
        """nexus-h33x8.6 a3 (dca12e1e3): ``_feed_stdin`` and
        ``_drain_stream`` run CONCURRENTLY via ``asyncio.gather`` -- stdin
        must be fed regardless of how slow/stuck the read side is, never
        waiting on stdout/stderr draining first. The old
        ``proc.communicate()``-based mechanism this replaced is where the
        historical 'no stdin data received in 3s' class originated
        (taxonomy class 4, nx_answer_runs id=100). Makes stdout/stderr
        hang forever and asserts stdin still gets written."""
        from nexus.operators.dispatch import claude_dispatch

        async def _hang_forever(n: int = -1) -> bytes:
            await asyncio.Future()
            return b""  # pragma: no cover — unreachable, Future never resolves

        proc = _make_proc()
        proc.stdout.read = _hang_forever
        proc.stderr.read = _hang_forever

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            task = asyncio.create_task(claude_dispatch("prompt", _SIMPLE_SCHEMA))
            try:
                for _ in range(200):
                    if proc.stdin.write.called:
                        break
                    await asyncio.sleep(0.005)
                assert proc.stdin.write.called, (
                    "stdin.write must fire even though stdout/stderr never "
                    "drain -- sequential feed-then-drain would leave this "
                    "unset"
                )
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    @pytest.mark.asyncio
    async def test_returns_parsed_json(self) -> None:
        """Return value must be the parsed JSON dict from stdout."""
        from nexus.operators.dispatch import claude_dispatch

        payload = {"extractions": [{"title": "Foo"}]}
        proc = _make_proc(stdout=json.dumps(payload).encode())

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            result = await claude_dispatch("prompt", _SIMPLE_SCHEMA)

        assert result == payload


class TestModelAndOperatorKwargs:
    """RDR-196 .p2b (nexus-nyry9.15): ``model``/``operator`` are ability-only
    keyword-only additions to ``claude_dispatch``. The load-bearing
    assertion is the DEFAULT-argv-unchanged test below -- everything else
    is new, additive behavior gated behind an explicit opt-in."""

    @pytest.mark.asyncio
    async def test_model_none_default_produces_no_model_flag_in_argv(self) -> None:
        """The protected assertion: model=None (every one of the 18
        pre-.p2b call sites) must leave argv byte-identical -- no
        --model flag anywhere in it."""
        from nexus.operators.dispatch import claude_dispatch

        proc = _make_proc()
        captured: list = []

        async def intercept(*args, **kwargs):
            captured.append(args)
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=intercept):
            await claude_dispatch("prompt", _SIMPLE_SCHEMA)

        assert "--model" not in captured[0], (
            f"--model must not appear in argv when model is not passed: {captured[0]}"
        )

    @pytest.mark.asyncio
    async def test_model_set_appends_model_flag_to_argv(self) -> None:
        """An explicit model= reaches argv as an adjacent --model <value>
        pair."""
        from nexus.operators.dispatch import claude_dispatch

        proc = _make_proc()
        captured: list = []

        async def intercept(*args, **kwargs):
            captured.append(args)
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=intercept):
            await claude_dispatch("prompt", _SIMPLE_SCHEMA, model="haiku")

        argv = captured[0]
        assert "--model" in argv, f"--model missing from argv: {argv}"
        idx = argv.index("--model")
        assert argv[idx + 1] == "haiku", (
            f"expected 'haiku' immediately after --model, got {argv[idx + 1]!r} in {argv}"
        )

    @pytest.mark.asyncio
    async def test_default_path_never_consults_the_tier_table(self) -> None:
        """DO 3: the tier table/resolver is not consulted on the default
        path. Structural, not intent-based: claude_dispatch has zero
        IMPORT of nexus.operators.model_tiers, so a default dispatch can
        be run with that module never imported and still succeed --
        proving the default path cannot possibly have consulted it.
        (Prose *mentions* of the module name in comments/docstrings are
        fine and expected -- only an actual import statement would let
        claude_dispatch reach the table, so that's what this checks.)"""
        import re

        from nexus.operators.dispatch import claude_dispatch

        proc = _make_proc()
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            await claude_dispatch("prompt", _SIMPLE_SCHEMA)

        # The real assertion: dispatch.py's own source has no IMPORT
        # STATEMENT reaching model_tiers -- grep for the import form,
        # not for the bare substring (which would also flag legitimate
        # doc-comment mentions of the module by name).
        import inspect

        import nexus.operators.dispatch as dispatch_mod

        source = inspect.getsource(dispatch_mod)
        import_pattern = re.compile(
            r"^\s*(import\s+nexus\.operators\.model_tiers"
            r"|from\s+nexus\.operators(\.model_tiers)?\s+import\s+"
            r"(model_tiers|\w+.*model_tiers))",
            re.MULTILINE,
        )
        assert not import_pattern.search(source), (
            "claude_dispatch must not IMPORT nexus.operators.model_tiers "
            "-- tier resolution is entirely the caller's responsibility "
            "in this bead"
        )

    @pytest.mark.asyncio
    async def test_bogus_model_error_names_both_model_and_operator(self) -> None:
        """DO 4: a --model rejected by the CLI (non-zero exit) must
        produce an OperatorError naming BOTH the rejected model id and
        the operator that requested it."""
        from nexus.operators.dispatch import claude_dispatch, OperatorError

        proc = _make_proc(
            stdout=b"",
            stderr=b"Error: model 'bogus-model-xyz' not found",
            returncode=1,
        )

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            with pytest.raises(OperatorError) as exc_info:
                await claude_dispatch(
                    "prompt", _SIMPLE_SCHEMA,
                    model="bogus-model-xyz", operator="operator_rank",
                )

        message = str(exc_info.value)
        assert "bogus-model-xyz" in message, (
            f"error must name the rejected model: {message}"
        )
        assert "operator_rank" in message, (
            f"error must name the requesting operator: {message}"
        )

    @pytest.mark.asyncio
    async def test_default_error_text_unchanged_when_model_not_set(self) -> None:
        """A non-zero-exit failure with model=None (the default) must not
        gain a stray '[model=None operator=None]' clause -- the default
        error text is untouched by this bead."""
        from nexus.operators.dispatch import claude_dispatch, OperatorError

        proc = _make_proc(
            stdout=b"", stderr=b"some unrelated failure", returncode=1,
        )

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            with pytest.raises(OperatorError) as exc_info:
                await claude_dispatch("prompt", _SIMPLE_SCHEMA)

        assert "model=" not in str(exc_info.value)
        assert "operator=" not in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_alias_model_reaches_argv_but_usage_sink_records_canonical(self) -> None:
        """RDR-196 R3 (relay decision): the tier resolves to a CLI ALIAS
        ("haiku"), which is what reaches argv -- but DispatchUsage.model
        (already wired by .p1a from the envelope's canonicalModel) must
        record whichever concrete model the alias actually resolved to,
        never the alias string itself. This is what makes an alias
        re-point observable in telemetry instead of silently confounding
        every measurement keyed on the tier table."""
        from nexus.operators.dispatch import claude_dispatch, DispatchUsage
        from nexus.operators.model_tiers import resolve_model_for_tier

        alias = resolve_model_for_tier("cheap")
        assert alias == "haiku"

        canonical_id = "claude-haiku-4-5-20260101"
        ndjson = (
            json.dumps({
                "type": "result",
                "is_error": False,
                "result": "ok",
                "structured_output": {"result": "ok"},
                "total_cost_usd": 0.02,
                "duration_ms": 500,
                "duration_api_ms": 400,
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "modelUsage": {
                    alias: {
                        "canonicalModel": canonical_id,
                        "inputTokens": 10,
                        "outputTokens": 5,
                        "costUSD": 0.02,
                    },
                },
            }) + "\n"
        )
        proc = _make_proc(stdout=ndjson.encode(), returncode=0, stderr=b"")
        sink: list[DispatchUsage] = []
        captured: list = []

        async def intercept(*args, **kwargs):
            captured.append(args)
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=intercept):
            await claude_dispatch(
                "prompt", _SIMPLE_SCHEMA, model=alias, usage_sink=sink,
            )

        argv = captured[0]
        idx = argv.index("--model")
        assert argv[idx + 1] == alias, "argv must carry the alias, not the canonical id"

        assert len(sink) == 1
        assert sink[0].model == canonical_id, (
            f"DispatchUsage.model must record the canonical id "
            f"({canonical_id!r}), not the alias ({alias!r}); got {sink[0].model!r}"
        )

    @pytest.mark.asyncio
    async def test_durable_log_emit_carries_model_and_operator(self) -> None:
        """Review fix (nexus-nyry9.15, code-review-expert [23032]
        Important #1): the DURABLE structlog ``operator_dispatch_failed``
        emit -- not just the transient exception text -- must carry
        ``model=``/``operator=``, since the function's own comments say
        this emit is the only thing a later investigation can grep."""
        from nexus.operators.dispatch import claude_dispatch, OperatorError

        proc = _make_proc(
            stdout=b"", stderr=b"Error: model 'bogus-model-xyz' not found",
            returncode=1,
        )

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            with patch("nexus.operators.dispatch._log") as mock_log:
                with pytest.raises(OperatorError):
                    await claude_dispatch(
                        "prompt", _SIMPLE_SCHEMA,
                        model="bogus-model-xyz", operator="operator_rank",
                    )

        assert mock_log.warning.called, "expected the WARNING-level emit (outside rolled-up scope)"
        _, kwargs = mock_log.warning.call_args
        assert kwargs.get("model") == "bogus-model-xyz", (
            f"durable log record must carry model=; got kwargs={kwargs}"
        )
        assert kwargs.get("operator") == "operator_rank", (
            f"durable log record must carry operator=; got kwargs={kwargs}"
        )

    @pytest.mark.asyncio
    async def test_durable_log_emit_carries_none_when_model_not_set(self) -> None:
        """The default path (model=None) still passes model=/operator=
        to the emit -- as None -- rather than omitting the keys, so the
        log record shape is uniform across both cases."""
        from nexus.operators.dispatch import claude_dispatch, OperatorError

        proc = _make_proc(stdout=b"", stderr=b"some unrelated failure", returncode=1)

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            with patch("nexus.operators.dispatch._log") as mock_log:
                with pytest.raises(OperatorError):
                    await claude_dispatch("prompt", _SIMPLE_SCHEMA)

        _, kwargs = mock_log.warning.call_args
        # Deliberately "key present with value None", not "key absent" --
        # `.get() is None` would pass either way, so check membership too.
        assert "model" in kwargs and kwargs["model"] is None
        assert "operator" in kwargs and kwargs["operator"] is None

    @pytest.mark.asyncio
    async def test_empty_string_model_normalized_to_none_with_warning(self) -> None:
        """Review fix (nexus-nyry9.15, code-review-expert [23032]
        Important #2): model="" (and whitespace-only) is normalized to
        None ONCE at function entry -- picked over rejecting with
        ValueError -- so the argv-append (`if model:`) and the error-
        clause gate (`if model is not None:`) can never disagree about
        whether a model was "set". A structlog warning names the
        operator so a caller that accidentally passes "" is diagnosable."""
        from nexus.operators.dispatch import claude_dispatch

        proc = _make_proc()
        captured: list = []

        async def intercept(*args, **kwargs):
            captured.append(args)
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=intercept):
            with patch("nexus.operators.dispatch._log") as mock_log:
                await claude_dispatch(
                    "prompt", _SIMPLE_SCHEMA, model="   ", operator="operator_extract",
                )

        assert "--model" not in captured[0], (
            f"an empty/whitespace model must never reach argv: {captured[0]}"
        )
        assert mock_log.warning.called
        _, kwargs = mock_log.warning.call_args
        assert kwargs.get("operator") == "operator_extract"

    @pytest.mark.asyncio
    async def test_empty_string_model_error_clause_matches_argv_absence(self) -> None:
        """The inconsistency the review flagged directly: model="" must
        produce NEITHER a --model flag in argv NOR a
        '[model=... operator=...]' clause on a harness-failure error --
        the two must agree, in both directions, not just the argv half."""
        from nexus.operators.dispatch import claude_dispatch, OperatorError

        proc = _make_proc(stdout=b"", stderr=b"some unrelated failure", returncode=1)

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            with pytest.raises(OperatorError) as exc_info:
                await claude_dispatch(
                    "prompt", _SIMPLE_SCHEMA, model="", operator="operator_rank",
                )

        message = str(exc_info.value)
        assert "model=" not in message, (
            f"empty-string model must not produce a model= clause: {message}"
        )
        assert "operator=" not in message, (
            f"empty-string model must not produce an operator= clause either: {message}"
        )


class TestMaxBudgetUsd:
    """nexus-2g8y7: ``max_budget_usd`` is an opt-in, per-DISPATCH cost
    ceiling wired straight to the CLI's own ``--max-budget-usd`` flag.
    The protected assertion is the DEFAULT-argv-unchanged test below --
    everything else is new, additive behavior gated behind an explicit
    opt-in (param or env)."""

    @pytest.mark.asyncio
    async def test_default_produces_no_max_budget_flag_in_argv(self) -> None:
        """max_budget_usd=None (the default) and no env var set must leave
        argv byte-identical -- no --max-budget-usd flag anywhere in it."""
        from nexus.operators.dispatch import claude_dispatch

        proc = _make_proc()
        captured: list = []

        async def intercept(*args, **kwargs):
            captured.append(args)
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=intercept):
            await claude_dispatch("prompt", _SIMPLE_SCHEMA)

        assert "--max-budget-usd" not in captured[0], (
            f"--max-budget-usd must not appear in argv when unset: {captured[0]}"
        )

    @pytest.mark.asyncio
    async def test_param_appends_max_budget_flag_to_argv(self) -> None:
        """An explicit max_budget_usd= reaches argv as an adjacent
        --max-budget-usd <value> pair, fixed-point formatted."""
        from nexus.operators.dispatch import claude_dispatch

        proc = _make_proc()
        captured: list = []

        async def intercept(*args, **kwargs):
            captured.append(args)
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=intercept):
            await claude_dispatch("prompt", _SIMPLE_SCHEMA, max_budget_usd=1.5)

        argv = captured[0]
        assert "--max-budget-usd" in argv, f"--max-budget-usd missing from argv: {argv}"
        idx = argv.index("--max-budget-usd")
        assert argv[idx + 1] == "1.500000", (
            f"expected fixed-point '1.500000' after --max-budget-usd, got "
            f"{argv[idx + 1]!r} in {argv}"
        )

    @pytest.mark.asyncio
    async def test_tiny_param_does_not_use_scientific_notation(self) -> None:
        """A very small ceiling (e.g. the probe script's 0.000001) must not
        reach argv as Python's default scientific-notation str() -- the
        CLI must see an unambiguous decimal literal."""
        from nexus.operators.dispatch import claude_dispatch

        proc = _make_proc()
        captured: list = []

        async def intercept(*args, **kwargs):
            captured.append(args)
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=intercept):
            await claude_dispatch("prompt", _SIMPLE_SCHEMA, max_budget_usd=0.000001)

        argv = captured[0]
        idx = argv.index("--max-budget-usd")
        assert "e" not in argv[idx + 1].lower(), (
            f"budget value must not use scientific notation: {argv[idx + 1]!r}"
        )
        assert argv[idx + 1] == "0.000001"

    @pytest.mark.asyncio
    async def test_env_fallback_appends_max_budget_flag(self, monkeypatch) -> None:
        """NX_DISPATCH_MAX_BUDGET_USD is consulted when the param is not
        passed."""
        from nexus.operators.dispatch import claude_dispatch

        monkeypatch.setenv("NX_DISPATCH_MAX_BUDGET_USD", "2.25")

        proc = _make_proc()
        captured: list = []

        async def intercept(*args, **kwargs):
            captured.append(args)
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=intercept):
            await claude_dispatch("prompt", _SIMPLE_SCHEMA)

        argv = captured[0]
        idx = argv.index("--max-budget-usd")
        assert argv[idx + 1] == "2.250000"

    @pytest.mark.asyncio
    async def test_param_wins_over_env(self, monkeypatch) -> None:
        """The explicit parameter takes precedence over the env fallback
        when both are set."""
        from nexus.operators.dispatch import claude_dispatch

        monkeypatch.setenv("NX_DISPATCH_MAX_BUDGET_USD", "9.99")

        proc = _make_proc()
        captured: list = []

        async def intercept(*args, **kwargs):
            captured.append(args)
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=intercept):
            await claude_dispatch("prompt", _SIMPLE_SCHEMA, max_budget_usd=0.5)

        argv = captured[0]
        idx = argv.index("--max-budget-usd")
        assert argv[idx + 1] == "0.500000", (
            "param must win over env, not be overridden or merged"
        )

    @pytest.mark.asyncio
    async def test_invalid_env_value_fails_loud_before_spawning(self, monkeypatch) -> None:
        """An unparseable NX_DISPATCH_MAX_BUDGET_USD must raise ValueError
        immediately -- never silently ignored, never reaching argv, and
        never spawning the subprocess."""
        from nexus.operators.dispatch import claude_dispatch

        monkeypatch.setenv("NX_DISPATCH_MAX_BUDGET_USD", "not-a-number")

        with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_spawn:
            with pytest.raises(ValueError, match="NX_DISPATCH_MAX_BUDGET_USD"):
                await claude_dispatch("prompt", _SIMPLE_SCHEMA)

        mock_spawn.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "-1", "0"])
    async def test_non_finite_or_non_positive_budget_fails_loud(
        self, monkeypatch, bad,
    ) -> None:
        """Review Medium 2 ([23268]): float() accepts nan/inf/negatives,
        none of which is a budget. Env AND param paths both reject."""
        from nexus.operators.dispatch import claude_dispatch

        monkeypatch.setenv("NX_DISPATCH_MAX_BUDGET_USD", bad)
        with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_spawn:
            with pytest.raises(ValueError, match="finite positive"):
                await claude_dispatch("prompt", _SIMPLE_SCHEMA)
        mock_spawn.assert_not_called()

        monkeypatch.delenv("NX_DISPATCH_MAX_BUDGET_USD")
        with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_spawn:
            with pytest.raises(ValueError, match="finite positive"):
                await claude_dispatch("prompt", _SIMPLE_SCHEMA,
                                      max_budget_usd=float(bad))
        mock_spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_env_value_is_treated_as_unset(self, monkeypatch) -> None:
        """An empty-string env value (unset-but-exported) must not raise
        and must not append the flag -- consistent with the model= empty-
        string normalization elsewhere in this module."""
        from nexus.operators.dispatch import claude_dispatch

        monkeypatch.setenv("NX_DISPATCH_MAX_BUDGET_USD", "")

        proc = _make_proc()
        captured: list = []

        async def intercept(*args, **kwargs):
            captured.append(args)
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=intercept):
            await claude_dispatch("prompt", _SIMPLE_SCHEMA)

        assert "--max-budget-usd" not in captured[0]

    @pytest.mark.asyncio
    async def test_budget_signal_raises_typed_error_with_partial_fields(self) -> None:
        """A fabricated budget-abort stderr, WITH max_budget_usd set, must
        raise OperatorBudgetExceededError (not the generic OperatorError)
        carrying the same partial_text/event_count/log_path fields as
        OperatorTimeoutError."""
        from pathlib import Path

        from nexus.operators.dispatch import (
            claude_dispatch,
            OperatorBudgetExceededError,
        )

        ndjson_line = json.dumps({
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "partial answer text"},
            },
        })
        proc = _make_proc(
            stdout=(ndjson_line + "\n").encode(),
            stderr=b"Error: max-budget-usd 0.000001 exceeded, aborting run",
            returncode=1,
        )

        captured_log_calls: list[tuple] = []

        def fake_persist(max_budget_usd, partial_text, stderr, event_count) -> str:
            captured_log_calls.append((max_budget_usd, partial_text, stderr, event_count))
            return "/fake/budget/log/path.log"

        with (
            patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)),
            patch("nexus.operators.dispatch._persist_budget_log", side_effect=fake_persist),
        ):
            with pytest.raises(OperatorBudgetExceededError) as exc_info:
                await claude_dispatch(
                    "prompt", _SIMPLE_SCHEMA, max_budget_usd=0.000001,
                )

        err = exc_info.value
        assert "partial answer text" in err.partial_text
        assert err.event_count == 1
        assert err.log_path == Path("/fake/budget/log/path.log")
        assert captured_log_calls, "_persist_budget_log was never called"
        assert captured_log_calls[0][0] == 0.000001

    @pytest.mark.asyncio
    async def test_budget_signal_ignored_when_not_opted_in(self) -> None:
        """The SAME budget-shaped stderr, WITHOUT max_budget_usd set, must
        raise the ordinary OperatorError -- a dispatch that never opted
        into a ceiling cannot have its failures reclassified."""
        from nexus.operators.dispatch import (
            claude_dispatch,
            OperatorBudgetExceededError,
            OperatorError,
        )

        proc = _make_proc(
            stdout=b"",
            stderr=b"Error: max-budget-usd 0.000001 exceeded, aborting run",
            returncode=1,
        )

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            with pytest.raises(OperatorError) as exc_info:
                await claude_dispatch("prompt", _SIMPLE_SCHEMA)

        assert not isinstance(exc_info.value, OperatorBudgetExceededError), (
            "a dispatch that never set max_budget_usd must not raise the "
            "budget-specific subclass, even if the failure text mentions "
            "'budget'"
        )

    @pytest.mark.asyncio
    async def test_unrelated_failure_with_budget_set_keeps_ordinary_error(self) -> None:
        """max_budget_usd is set, but the failure text has no budget/abort
        signal at all -- must raise the ordinary OperatorError, not the
        typed subclass (no false positive from the mere presence of the
        opt-in kwarg)."""
        from nexus.operators.dispatch import (
            claude_dispatch,
            OperatorBudgetExceededError,
            OperatorError,
        )

        proc = _make_proc(
            stdout=b"", stderr=b"rate limit exceeded", returncode=1,
        )

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            with pytest.raises(OperatorError) as exc_info:
                await claude_dispatch(
                    "prompt", _SIMPLE_SCHEMA, max_budget_usd=5.0,
                )

        assert not isinstance(exc_info.value, OperatorBudgetExceededError)

    @pytest.mark.asyncio
    async def test_cli_rejecting_the_flag_surfaces_cli_stderr_not_stripped(self) -> None:
        """An old CLI that does not support --max-budget-usd must still
        fail loud with the CLI's own stderr in the message -- the flag is
        never silently stripped from argv on a prior failure (there is no
        retry path here at all: the failure is simply reported)."""
        from nexus.operators.dispatch import claude_dispatch, OperatorError

        proc = _make_proc(
            stdout=b"",
            stderr=b"error: unknown option '--max-budget-usd'",
            returncode=1,
        )
        captured: list = []

        async def intercept(*args, **kwargs):
            captured.append(args)
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=intercept):
            with pytest.raises(OperatorError) as exc_info:
                await claude_dispatch(
                    "prompt", _SIMPLE_SCHEMA, max_budget_usd=1.0,
                )

        assert "--max-budget-usd" in captured[0], (
            "the flag must still have been passed to argv -- never stripped"
        )
        assert "unknown option" in str(exc_info.value), (
            "the CLI's real stderr must reach the caller-visible message"
        )


# ── nexus-5daww: _build_dispatch_env must not forward a live T1 session ─────
#
# operators.dispatch.claude_dispatch's `ephemeral=True` mode (the default,
# used by RDR-080 tool grants such as nx_plan_audit / nx_enrich_beads) spawns
# a nested `nx-mcp`. Pre-fix, `_build_dispatch_env` copied `os.environ`
# verbatim except for a short explicit-strip list that did NOT include
# NX_T1_SESSION / NX_T1_SESSION_ID -- so a PARENT nx-mcp's already-minted,
# LIVE SERVICE-backed T1 session token leaked straight through to the child's
# env. The child's own `_t1_lifespan` Branch 0 would then resolve the
# SAME session id (via the still-forwarded NX_SESSION_ID) and either directly
# reuse the leaked token to mint a REPLACEMENT (before the mcp/core.py fix)
# or, worse, invalidate the parent's live token via
# HttpTokenStore.start_session's ON CONFLICT DO UPDATE rotation. This class
# locks the defense-in-depth half of the fix: the child must never even see
# the parent's token pair in its own env, in both `ephemeral` and `owned`
# dispatch modes.

class TestBuildDispatchEnvStripsT1Session:
    def test_ephemeral_strips_inherited_t1_session_pair(self, monkeypatch) -> None:
        """nexus-4lkmz decision 1 / nexus-bjltu: the parent's INHERITED
        token must never reach the subprocess; when the dispatch grants
        tool access, a fresh, OWN minted token replaces it (env keys are
        present again, but never the inherited values)."""
        from nexus.operators.dispatch import _build_dispatch_env

        monkeypatch.setenv("NX_T1_SESSION", "live-parent-token")
        monkeypatch.setenv("NX_T1_SESSION_ID", "live-parent-session")
        monkeypatch.setattr(
            "nexus.db.t1.mint_t1_session_token",
            lambda session_id, *, context: {"session_token": "own-minted-token"},
        )

        env = _build_dispatch_env(
            ephemeral=True,
            parent_session_id="live-parent-session",
            grants_tool_access=True,
        )

        assert env.get("NX_T1_SESSION") == "own-minted-token", (
            "a nested MCP subprocess must never inherit the parent's live "
            "SERVICE-backed T1 session token"
        )
        assert env.get("NX_T1_SESSION_ID") != "live-parent-session"

    def test_ephemeral_tool_free_strips_inherited_pair_and_mints_nothing(
        self, monkeypatch
    ) -> None:
        """nexus-bjltu: without tool access granted, the inherited pair is
        still stripped (defense in depth, unconditional) but nothing new
        is minted to replace it -- the keys are simply absent."""
        from nexus.operators.dispatch import _build_dispatch_env

        monkeypatch.setenv("NX_T1_SESSION", "live-parent-token")
        monkeypatch.setenv("NX_T1_SESSION_ID", "live-parent-session")
        monkeypatch.setattr(
            "nexus.db.t1.mint_t1_session_token",
            lambda session_id, *, context: (_ for _ in ()).throw(
                AssertionError("must not be called for a tool-free dispatch")
            ),
        )

        env = _build_dispatch_env(ephemeral=True, parent_session_id="live-parent-session")

        assert "NX_T1_SESSION" not in env
        assert "NX_T1_SESSION_ID" not in env

    def test_owned_strips_inherited_t1_session_pair(self, monkeypatch) -> None:
        from nexus.operators.dispatch import _build_dispatch_env

        monkeypatch.setenv("NX_T1_SESSION", "live-parent-token")
        monkeypatch.setenv("NX_T1_SESSION_ID", "live-parent-session")

        env = _build_dispatch_env()  # owned: neither share_t1 nor ephemeral

        assert "NX_T1_SESSION" not in env
        assert "NX_T1_SESSION_ID" not in env

    def test_ephemeral_still_forwards_nx_session_id_for_attribution(
        self, monkeypatch
    ) -> None:
        """The GENERAL NX_SESSION_ID (distinct from the T1-specific
        NX_T1_SESSION_ID) is deliberately still forwarded for attribution --
        only the T1 session-token pair is stripped-then-reminted."""
        from nexus.operators.dispatch import _build_dispatch_env

        monkeypatch.setenv("NX_T1_SESSION", "live-parent-token")
        monkeypatch.setenv("NX_T1_SESSION_ID", "live-parent-session")
        monkeypatch.setattr(
            "nexus.db.t1.mint_t1_session_token",
            lambda session_id, *, context: {"session_token": "own-minted-token"},
        )

        env = _build_dispatch_env(
            ephemeral=True,
            parent_session_id="live-parent-session",
            grants_tool_access=True,
        )

        assert env.get("NX_SESSION_ID") == "live-parent-session"


# ── Minted-session lifecycle (nexus-bjltu Significant #1) ──────────────────
#
# operator dispatch is the high-volume default path; a per-dispatch mint
# with no corresponding close relies entirely on the passive 24h TTL sweep
# backstop -- real session-row accretion. claude_dispatch now closes any
# session IT minted in a finally, after the subprocess exits, mirroring
# mcp.core's Branch-0 teardown (HttpScratchStore().close_session() +
# .close()) but constructed explicitly from the minted id/token.


class _FakeScratchStore:
    """Records scratch-close activity; used across TestDispatchSessionClose."""

    events: list[tuple] = []

    def __init__(self, *, session_id, _session_token):
        type(self).events.append(("scratch_constructed", session_id, _session_token))

    def close_session(self):
        type(self).events.append(("scratch_close_session",))
        return 0

    def close(self):
        type(self).events.append(("scratch_close",))


class _FakeTokenStore:
    """Records token-close activity; used across TestDispatchSessionClose."""

    events: list[tuple] = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        type(self).events.append(("token_store_closed",))

    def close_session(self, session_id):
        type(self).events.append(("token_close_session", session_id))
        return {"closed": 1}


class TestDispatchSessionClose:
    @pytest.mark.asyncio
    async def test_successful_dispatch_closes_both_scratch_and_token(self) -> None:
        """nexus-bjltu round 2 (code-review finding): round 1 closed ONLY
        the scratch rows (HttpScratchStore.close_session). The minted
        SESSION TOKEN itself (mint_t1_session_token -> POST
        /v1/sessions/start) is a SEPARATE row, closed by a SEPARATE call
        (HttpTokenStore().close_session(session_id) -> POST
        /v1/sessions/close) -- mirroring mcp.core's Branch-0
        _t1_session_shutdown exactly: scratch close first, token close
        second, both independently best-effort."""
        from nexus.operators.dispatch import claude_dispatch

        _FakeScratchStore.events = []
        _FakeTokenStore.events = []

        proc = _make_proc()

        async def intercept(*args, **kwargs):
            return proc

        def _fake_mint(session_id: str, *, context: str) -> dict:
            return {"session_token": f"tok-for-{session_id}"}

        with (
            patch("asyncio.create_subprocess_exec", side_effect=intercept),
            patch("nexus.db.t1.mint_t1_session_token", side_effect=_fake_mint),
            patch("nexus.db.http_scratch_store.HttpScratchStore", _FakeScratchStore),
            patch("nexus.db.t2.http_token_store.HttpTokenStore", _FakeTokenStore),
        ):
            dispatch_result = await claude_dispatch(
                "prompt", _SIMPLE_SCHEMA, allowed_tools=["Read"]
            )

        assert dispatch_result == {"ok": True}
        scratch_kinds = [e[0] for e in _FakeScratchStore.events]
        token_kinds = [e[0] for e in _FakeTokenStore.events]
        assert "scratch_close_session" in scratch_kinds, (
            f"the minted session's scratch rows must be closed: {_FakeScratchStore.events}"
        )
        assert "scratch_close" in scratch_kinds
        assert "token_close_session" in token_kinds, (
            f"the minted session TOKEN must be closed independently -- it has "
            f"no sweep backstop, unlike the scratch rows: {_FakeTokenStore.events}"
        )
        # Ordering: scratch close first, then token close (mirrors
        # mcp.core._t1_session_shutdown's own ordering).
        scratch_close_idx = scratch_kinds.index("scratch_close_session")
        token_close_idx = token_kinds.index("token_close_session")
        # Both event lists are separately ordered per-store; assert via a
        # combined merge using insertion order is unnecessary here since
        # _close_dispatch_session runs scratch step 1 to completion before
        # starting token step 2 -- verified structurally: the scratch
        # store's close() (pool teardown, always last for that step) must
        # already be recorded before ANY token event exists.
        assert "scratch_close" in scratch_kinds[:scratch_close_idx + 2]
        assert token_close_idx >= 0

    @pytest.mark.asyncio
    async def test_tool_free_dispatch_attempts_no_close(self) -> None:
        """No mint happened, so neither the scratch close nor the token
        close must be attempted."""
        from nexus.operators.dispatch import claude_dispatch

        _FakeScratchStore.events = []
        _FakeTokenStore.events = []

        proc = _make_proc()

        async def intercept(*args, **kwargs):
            return proc

        with (
            patch("asyncio.create_subprocess_exec", side_effect=intercept),
            patch("nexus.db.http_scratch_store.HttpScratchStore", _FakeScratchStore),
            patch("nexus.db.t2.http_token_store.HttpTokenStore", _FakeTokenStore),
        ):
            await claude_dispatch("prompt", _SIMPLE_SCHEMA)

        assert _FakeScratchStore.events == [], (
            "a tool-free dispatch minted nothing, so it must attempt no scratch close"
        )
        assert _FakeTokenStore.events == [], (
            "a tool-free dispatch minted nothing, so it must attempt no token close"
        )

    @pytest.mark.asyncio
    async def test_scratch_close_failure_does_not_block_token_close(self) -> None:
        """The two closes are INDEPENDENT: a failure in the scratch-row
        close must not skip the token close."""
        from structlog.testing import capture_logs

        from nexus.operators.dispatch import claude_dispatch

        _FakeTokenStore.events = []

        proc = _make_proc()

        async def intercept(*args, **kwargs):
            return proc

        def _fake_mint(session_id: str, *, context: str) -> dict:
            return {"session_token": f"tok-for-{session_id}"}

        class _FailingScratchStore:
            def __init__(self, *, session_id, _session_token):
                pass

            def close_session(self):
                raise RuntimeError("scratch close: storage service unreachable")

            def close(self):
                pass

        with (
            patch("asyncio.create_subprocess_exec", side_effect=intercept),
            patch("nexus.db.t1.mint_t1_session_token", side_effect=_fake_mint),
            patch("nexus.db.http_scratch_store.HttpScratchStore", _FailingScratchStore),
            patch("nexus.db.t2.http_token_store.HttpTokenStore", _FakeTokenStore),
            capture_logs() as cap,
        ):
            result = await claude_dispatch(
                "prompt", _SIMPLE_SCHEMA, allowed_tools=["Read"]
            )

        assert result == {"ok": True}, "a scratch-close failure must not fail an already-successful dispatch"
        assert any(e[0] == "token_close_session" for e in _FakeTokenStore.events), (
            "the token close must still run even though the scratch close raised"
        )
        warnings = [
            e for e in cap
            if e.get("event") == "operator_dispatch_t1_scratch_close_failed"
        ]
        assert warnings, f"the scratch-close failure must be logged: {cap}"
        assert "scratch close: storage service unreachable" in warnings[0].get("error", "")

    @pytest.mark.asyncio
    async def test_token_close_failure_does_not_fail_the_dispatch(self) -> None:
        """A token-close failure (storage service hiccup) must be logged
        and swallowed -- independent of whether the scratch close
        succeeded. NO sweep backstops an unrevoked token (unlike the
        scratch rows), but that is exactly why this call must never
        crash a dispatch that already succeeded -- the failure is
        recorded, not silently discarded and not fatal."""
        from structlog.testing import capture_logs

        from nexus.operators.dispatch import claude_dispatch

        _FakeScratchStore.events = []

        proc = _make_proc()

        async def intercept(*args, **kwargs):
            return proc

        def _fake_mint(session_id: str, *, context: str) -> dict:
            return {"session_token": f"tok-for-{session_id}"}

        class _FailingTokenStore:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                pass

            def close_session(self, session_id):
                raise RuntimeError("token close: storage service unreachable")

        with (
            patch("asyncio.create_subprocess_exec", side_effect=intercept),
            patch("nexus.db.t1.mint_t1_session_token", side_effect=_fake_mint),
            patch("nexus.db.http_scratch_store.HttpScratchStore", _FakeScratchStore),
            patch("nexus.db.t2.http_token_store.HttpTokenStore", _FailingTokenStore),
            capture_logs() as cap,
        ):
            result = await claude_dispatch(
                "prompt", _SIMPLE_SCHEMA, allowed_tools=["Read"]
            )

        assert result == {"ok": True}, "a token-close failure must not fail an already-successful dispatch"
        assert any(e[0] == "scratch_close_session" for e in _FakeScratchStore.events), (
            "the scratch close must have run regardless of the token close's later failure"
        )
        warnings = [
            e for e in cap
            if e.get("event") == "operator_dispatch_t1_token_close_failed"
        ]
        assert warnings, f"the token-close failure must be logged: {cap}"
        assert "token close: storage service unreachable" in warnings[0].get("error", "")

    @pytest.mark.asyncio
    async def test_subprocess_creation_failure_still_closes_session(self) -> None:
        """nexus-bjltu round 2 (code-review + critic independently): if
        ``asyncio.create_subprocess_exec`` itself raises (fork/exec error,
        resource exhaustion) AFTER a successful mint, the minted session
        (scratch rows AND token) must still be closed -- not leaked."""
        from nexus.operators.dispatch import claude_dispatch

        _FakeScratchStore.events = []
        _FakeTokenStore.events = []

        def _fake_mint(session_id: str, *, context: str) -> dict:
            return {"session_token": f"tok-for-{session_id}"}

        async def _boom(*args, **kwargs):
            raise OSError("fork failed: resource temporarily unavailable")

        with (
            patch("asyncio.create_subprocess_exec", side_effect=_boom),
            patch("nexus.db.t1.mint_t1_session_token", side_effect=_fake_mint),
            patch("nexus.db.http_scratch_store.HttpScratchStore", _FakeScratchStore),
            patch("nexus.db.t2.http_token_store.HttpTokenStore", _FakeTokenStore),
        ):
            with pytest.raises(OSError, match="fork failed"):
                await claude_dispatch(
                    "prompt", _SIMPLE_SCHEMA, allowed_tools=["Read"]
                )

        assert any(e[0] == "scratch_close_session" for e in _FakeScratchStore.events), (
            f"a subprocess-creation failure must still close the minted "
            f"scratch session: {_FakeScratchStore.events}"
        )
        assert any(e[0] == "token_close_session" for e in _FakeTokenStore.events), (
            f"a subprocess-creation failure must still close the minted "
            f"session token: {_FakeTokenStore.events}"
        )


# ── Error handling ─────────────────────────────────────────────────────────

class TestErrorHandling:

    @pytest.mark.asyncio
    async def test_timeout_kills_process_and_raises(self) -> None:
        """Timeout must kill the subprocess and raise OperatorTimeoutError."""
        from nexus.operators.dispatch import claude_dispatch, OperatorTimeoutError

        proc = _make_proc()
        call_count = {"n": 0}

        async def hang(n: int = -1) -> bytes:
            # Only the FIRST call (the drain loop's own read, which is what
            # must be cancelled by the timeout) hangs. Every later call
            # (whichever of stdout/stderr didn't win the race, plus the
            # post-kill mop-up reads) returns immediately -- mirrors the
            # real dead-transport EOF behaviour after kill+wait.
            call_count["n"] += 1
            if call_count["n"] == 1:
                await asyncio.sleep(999)
            return b""

        proc.stdout.read = hang
        proc.stderr.read = hang

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            with pytest.raises(OperatorTimeoutError):
                await claude_dispatch("prompt", _SIMPLE_SCHEMA, timeout=0.01)

        proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_timeout_fires_when_streams_eof_but_process_never_exits(self) -> None:
        """nexus-h33x8.6 review round 2 (code-review on dca12e1e3): a3 moved
        the final ``await proc.wait()`` to AFTER the ``asyncio.wait_for``
        that guards the read loop, mirroring the shape but not the
        SCOPE of CPython's own ``Process.communicate()`` (which calls
        ``self.wait()`` from INSIDE the coroutine tree it awaits). Both
        streams reaching EOF does not guarantee the child has actually
        exited -- it can close its stdout/stderr fds before its own
        process exit completes. A ``proc.wait()`` left outside the
        timeout guard then hangs forever with no kill ever firing.

        Both streams EOF immediately (the default one-shot reader on
        empty stdout/stderr), but ``proc.wait()`` never returns within
        the budget -- this must still time out and kill, not hang.
        """
        from nexus.operators.dispatch import claude_dispatch, OperatorTimeoutError

        proc = _make_proc(stdout=b"", stderr=b"")
        call_count = {"n": 0}

        async def wait_side_effect() -> int:
            # First call (inside the guarded scope) hangs past the budget
            # and must be cancelled by wait_for. The post-kill reap call
            # in the except-block returns immediately -- the real
            # transport reaps cleanly once the process is actually dead.
            call_count["n"] += 1
            if call_count["n"] == 1:
                await asyncio.sleep(999)
            return 0

        proc.wait = wait_side_effect

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            with pytest.raises(OperatorTimeoutError):
                # Outer bound: on a real regression (wait() back outside the
                # guard) the dispatch hangs for the mock's 999s sleep; fail
                # fast instead of stalling a worker (no pytest-timeout here).
                await asyncio.wait_for(
                    claude_dispatch("prompt", _SIMPLE_SCHEMA, timeout=0.05), timeout=2
                )

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
    async def test_nonzero_exit_surfaces_stdout_when_stderr_is_empty(self) -> None:
        """A diagnostic on stdout must reach the error (GH #1414).

        ``claude -p --output-format json`` reports its errors on STDOUT, so
        building the message from stderr alone produced the literal
        ``claude -p exited 1:`` with nothing after the colon — twice, for
        nx_plan_audit, with nothing in mcp.log either. The subprocess said
        why; we threw it away.
        """
        from nexus.operators.dispatch import claude_dispatch, OperatorError

        proc = _make_proc(
            stdout=b'{"type":"result","subtype":"error_during_execution"}',
            returncode=1,
            stderr=b'',
        )

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            with pytest.raises(OperatorError, match="error_during_execution"):
                await claude_dispatch("prompt", _SIMPLE_SCHEMA)

    @pytest.mark.asyncio
    async def test_nonzero_exit_surfaces_claude_error_text_not_ndjson_envelope(self) -> None:
        """GH #1414 REGRESSION (nexus-h33x8.6 review round 2, code-review on
        dca12e1e3): a3 switched stdout to NDJSON (stream-json), but this
        error path still read the RAW joined bytes for the snippet/detail/
        durable-log fields -- mostly ``system``/``assistant`` envelope JSON
        now, not claude's own error text. That is exactly the opacity class
        the original GH #1414 fix existed to end, reintroduced one layer
        down.

        A realistic multi-line NDJSON stream ending in an ``is_error``
        result must surface THAT result's own ``result`` text, not the
        preceding envelope noise.
        """
        from nexus.operators.dispatch import claude_dispatch, OperatorError

        ndjson = "\n".join([
            '{"type":"system","subtype":"init","cwd":"/tmp","session_id":"s1"}',
            '{"type":"assistant","message":{"role":"assistant","content":'
            '[{"type":"text","text":"thinking about the request"}]}}',
            '{"type":"result","is_error":true,'
            '"result":"claude reported: rate limit exceeded for this account",'
            '"subtype":"error_during_execution","structured_output":null}',
        ])
        proc = _make_proc(stdout=ndjson.encode(), returncode=1, stderr=b"")

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            with pytest.raises(OperatorError) as exc:
                await claude_dispatch("prompt", _SIMPLE_SCHEMA)

        message = str(exc.value)
        assert "rate limit exceeded for this account" in message, (
            f"claude's own error text is missing from the surfaced message: {message!r}"
        )
        assert '"type":"system"' not in message, (
            f"NDJSON envelope noise leaked into the surfaced message: {message!r}"
        )
        assert '"type":"assistant"' not in message, (
            f"NDJSON envelope noise leaked into the surfaced message: {message!r}"
        )
        assert "thinking about the request" not in message, (
            f"an intermediate assistant turn leaked into the surfaced message, "
            f"not the terminal result: {message!r}"
        )

    @pytest.mark.asyncio
    async def test_nonzero_exit_reports_both_streams_when_both_spoke(self) -> None:
        """Neither stream is dropped when both carry text."""
        from nexus.operators.dispatch import claude_dispatch, OperatorError

        proc = _make_proc(
            stdout=b'stdout-diagnosis',
            returncode=2,
            stderr=b'stderr-diagnosis',
        )

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            with pytest.raises(OperatorError) as exc:
                await claude_dispatch("prompt", _SIMPLE_SCHEMA)

        assert "stdout-diagnosis" in str(exc.value)
        assert "stderr-diagnosis" in str(exc.value)

    @pytest.mark.asyncio
    async def test_nonzero_exit_says_so_when_the_subprocess_was_silent(self) -> None:
        """Silence must read as silence, not as a truncated message.

        A bare trailing colon is indistinguishable from 'we dropped the
        output' — which is precisely the ambiguity GH #1414 spent a session
        resolving by hand.
        """
        from nexus.operators.dispatch import claude_dispatch, OperatorError

        proc = _make_proc(stdout=b'', returncode=1, stderr=b'')

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            with pytest.raises(OperatorError, match="no output on stdout or stderr"):
                await claude_dispatch("prompt", _SIMPLE_SCHEMA)

    @pytest.mark.asyncio
    async def test_nonzero_exit_logs_a_durable_event(self) -> None:
        """A non-zero exit must leave a record, not only an exception.

        GH #1414 searched a May-July mcp.log and found nothing for the
        failure. Of the 17 claude_dispatch call sites, 13 propagate bare
        with no logging on at least one real invocation path, and three
        (nx_plan_audit, nx_tidy, nx_enrich_beads) have no covered path at
        all — FastMCP's handler returns str(e) to the client without
        logging. So the record has to be written HERE, at the one choke
        point every caller passes through.
        """
        from nexus.operators.dispatch import claude_dispatch, OperatorError

        proc = _make_proc(
            stdout=b'stdout-diagnosis', returncode=7, stderr=b'stderr-diagnosis',
        )

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            with patch("nexus.operators.dispatch._log") as mock_log:
                with pytest.raises(OperatorError):
                    await claude_dispatch("prompt", _SIMPLE_SCHEMA)

        assert mock_log.warning.called, "non-zero exit left no durable record"
        event, kwargs = mock_log.warning.call_args[0], mock_log.warning.call_args[1]
        assert event[0] == "operator_dispatch_failed"
        assert kwargs["returncode"] == 7
        assert "stdout-diagnosis" in kwargs["stdout"]
        assert "stderr-diagnosis" in kwargs["stderr"]

    @pytest.mark.asyncio
    async def test_nonzero_exit_log_carries_more_than_the_exception(self) -> None:
        """The log is the durable channel; it must not inherit the 300-char
        exception cap.

        nexus-1at5's actual lesson was durable persistence independent of
        the exception-text channel, not a bigger snippet in the message. A
        JSON error payload with a stack summary exceeds 300 chars easily,
        and truncating the log to the message's cap would lose it again.
        """
        from nexus.operators.dispatch import claude_dispatch, OperatorError

        long_diagnostic = ("E" * 900) + "TAIL-MARKER"
        proc = _make_proc(
            stdout=long_diagnostic.encode(), returncode=1, stderr=b'',
        )

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            with patch("nexus.operators.dispatch._log") as mock_log:
                with pytest.raises(OperatorError) as exc:
                    await claude_dispatch("prompt", _SIMPLE_SCHEMA)

        assert "TAIL-MARKER" not in str(exc.value), "exception should stay capped"
        logged = mock_log.warning.call_args[1]["stdout"]
        assert "TAIL-MARKER" in logged, "the durable record lost the diagnostic tail"

    @pytest.mark.asyncio
    async def test_logged_stream_marks_its_own_truncation(self) -> None:
        """A capped field must say it was capped.

        Without a marker, a field of exactly _LOG_STREAM_CAP chars is
        indistinguishable from a diagnostic that happened to be exactly
        that long — the same "silence must read as silence" ambiguity this
        module designs out of the exception text, one field over, in the
        branch whose whole job is preserving diagnostics.
        """
        from nexus.operators.dispatch import (
            claude_dispatch, OperatorError, _LOG_STREAM_CAP,
        )

        oversized = "D" * (_LOG_STREAM_CAP + 500)
        proc = _make_proc(stdout=oversized.encode(), returncode=1, stderr=b'')

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            with patch("nexus.operators.dispatch._log") as mock_log:
                with pytest.raises(OperatorError):
                    await claude_dispatch("prompt", _SIMPLE_SCHEMA)

        logged = mock_log.warning.call_args[1]["stdout"]
        assert logged.endswith("...[truncated]"), "capped field does not admit it"

    @pytest.mark.asyncio
    async def test_logged_stream_at_the_cap_is_not_marked(self) -> None:
        """The marker must mean something: no marker when nothing was cut."""
        from nexus.operators.dispatch import (
            claude_dispatch, OperatorError, _LOG_STREAM_CAP,
        )

        exact = "D" * _LOG_STREAM_CAP
        proc = _make_proc(stdout=exact.encode(), returncode=1, stderr=b'')

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            with patch("nexus.operators.dispatch._log") as mock_log:
                with pytest.raises(OperatorError):
                    await claude_dispatch("prompt", _SIMPLE_SCHEMA)

        logged = mock_log.warning.call_args[1]["stdout"]
        assert "truncated" not in logged
        assert len(logged) == _LOG_STREAM_CAP

    @pytest.mark.asyncio
    async def test_malformed_json_raises_operator_output_error(self) -> None:
        """Unparseable stdout raises OperatorOutputError."""
        from nexus.operators.dispatch import claude_dispatch, OperatorOutputError

        proc = _make_proc(stdout=b'not valid json {{{{')

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            with pytest.raises(OperatorOutputError):
                await claude_dispatch("prompt", _SIMPLE_SCHEMA)

    @pytest.mark.asyncio
    async def test_malformed_json_error_names_event_count_and_offending_line(self) -> None:
        """nexus-nyry9.4 review-fix (RDR-196 .r4 residual, taxonomy
        id=99, 2026-05-06): the generic 'not valid JSON' message gave no
        way to tell 'the child never spoke stream-json at all' apart
        from 'a real stream got cut mid-line' -- both produced identical
        text, and the old ``raw[:200]`` preview showed only the START of
        the WHOLE blob, which for a real multi-line NDJSON stream is
        rarely where the parse actually failed. Enrich with: how many
        NDJSON lines parsed as objects, that no terminal 'result' event
        was seen, and the OFFENDING line's own content (located via the
        JSONDecodeError's line number), not just the blob's opening
        bytes.

        Two lines, each individually valid JSON (so both count as
        parsed events) -- line 1 padded past 200 chars so the old
        ``raw[:200]`` preview never reaches line 2's content; their
        CONCATENATION is not one valid JSON value, so whole-blob
        ``json.loads`` fails with 'Extra data' pointing at line 2.
        """
        from nexus.operators.dispatch import claude_dispatch, OperatorOutputError

        padding = "x" * 300
        line1 = (
            '{"type":"system","subtype":"init","cwd":"/' + padding + '","session_id":"s1"}'
        )
        line2 = '{"type":"weird_unexpected_event","note":"THE-OFFENDING-CONTENT-MARKER"}'
        proc = _make_proc(stdout=(line1 + "\n" + line2).encode())

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            with pytest.raises(OperatorOutputError) as exc_info:
                await claude_dispatch("prompt", _SIMPLE_SCHEMA)

        msg = str(exc_info.value)
        assert "not valid JSON" in msg
        assert "2 NDJSON line(s) parsed as objects" in msg, (
            f"event count missing from: {msg}"
        )
        assert "no terminal 'result' event seen" in msg, (
            f"result-event status missing from: {msg}"
        )
        assert "THE-OFFENDING-CONTENT-MARKER" in msg, (
            "the offending line's own content must be surfaced -- "
            f"raw[:200] of the whole (padded) blob never reaches it: {msg}"
        )

    @pytest.mark.asyncio
    async def test_empty_stdout_raises_operator_output_error(self) -> None:
        """Empty stdout raises OperatorOutputError, not JSONDecodeError."""
        from nexus.operators.dispatch import claude_dispatch, OperatorOutputError

        proc = _make_proc(stdout=b'')

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            with pytest.raises(OperatorOutputError):
                await claude_dispatch("prompt", _SIMPLE_SCHEMA)


# ── stream-json parsing (nexus-h33x8.6 a3) ──────────────────────────────────
#
# ``claude -p --output-format json`` buffers the ENTIRE response and writes
# nothing until the turn completes, so a subprocess killed at timeout has
# written exactly zero bytes BY CONSTRUCTION -- the nexus-1at5 partial-output
# drain has never preserved one. Switching to ``--output-format stream-json``
# (NDJSON, one event per line) puts bytes on the wire as they happen. The
# terminal ``{"type":"result", ...}`` line is byte-identical in shape to the
# single object ``--output-format json`` returns -- verified empirically
# against the captured fixture pair below (same prompt, same schema, both
# formats, same run family) -- so claude_dispatch's return contract for
# callers is unchanged; only the wire format and the read mechanism differ.

import pathlib  # noqa: E402 - deferred: only needed by the fixture-reading tests below

_FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"


def _load_fixture_lines(name: str) -> list[str]:
    text = (_FIXTURES_DIR / name).read_text()
    return [line for line in text.splitlines() if line.strip()]


class TestStreamJsonParsing:
    """Unit tests for ``_parse_stream_json_output`` against a captured
    fixture pair (tests/fixtures/claude_dispatch_{json,stream_json}_mode_
    sample.*) -- one real ``claude -p`` invocation of the same trivial
    prompt/schema, captured once in each output format."""

    def test_final_result_matches_json_mode_output_shape(self) -> None:
        """The reconstructed terminal event must carry the same
        is_error/result/structured_output the plain json-mode call
        returned for the identical prompt+schema -- proving the parser
        doesn't change claude_dispatch's return contract."""
        from nexus.operators.dispatch import _parse_stream_json_output

        stream_raw = "\n".join(_load_fixture_lines("claude_dispatch_stream_json_sample.ndjson"))
        json_mode = json.loads(
            (_FIXTURES_DIR / "claude_dispatch_json_mode_sample.json").read_text()
        )

        final_result, partial_text, event_count = _parse_stream_json_output(stream_raw)

        assert final_result is not None, "no terminal result event found in fixture"
        assert final_result["is_error"] == json_mode["is_error"]
        assert final_result["result"] == json_mode["result"]
        assert final_result["structured_output"] == json_mode["structured_output"]
        assert final_result["type"] == "result"
        assert event_count == len(_load_fixture_lines("claude_dispatch_stream_json_sample.ndjson"))

    def test_reconstructs_partial_text_when_killed_before_result(self) -> None:
        """Feed every fixture line EXCEPT the terminal result event
        (simulating a SIGKILL before the turn finished) and confirm the
        parser reconstructs the in-flight structured-output JSON from the
        ``input_json_delta`` stream events -- the dominant partial-content
        shape for a schema-constrained dispatch (every real
        claude_dispatch call passes json_schema, so the model answers via
        a StructuredOutput tool call, not free text)."""
        from nexus.operators.dispatch import _parse_stream_json_output

        lines = _load_fixture_lines("claude_dispatch_stream_json_sample.ndjson")
        pre_result_lines = [l for l in lines if json.loads(l).get("type") != "result"]
        raw = "\n".join(pre_result_lines)

        final_result, partial_text, event_count = _parse_stream_json_output(raw)

        assert final_result is None, "no result line was fed -- must not fabricate one"
        assert event_count == len(pre_result_lines)
        # The fixture's input_json_delta chunks assemble to '{"ok": true}'.
        assert '"ok"' in partial_text
        assert "true" in partial_text

    def test_skips_malformed_trailing_line_without_raising(self) -> None:
        """A line truncated mid-write by SIGKILL (the writer died between
        two fwrite() calls) must be skipped, not raise -- the whole point
        of reconstructing partial output is resilience to a cut stream."""
        from nexus.operators.dispatch import _parse_stream_json_output

        lines = _load_fixture_lines("claude_dispatch_stream_json_sample.ndjson")
        well_formed = [l for l in lines if json.loads(l).get("type") != "result"]
        truncated = '{"type":"stream_event","event":{"type":"content_block_delta"'  # cut mid-object
        raw = "\n".join(well_formed + [truncated])

        final_result, partial_text, event_count = _parse_stream_json_output(raw)

        assert final_result is None
        assert event_count == len(well_formed), (
            "the truncated trailing line must not count as a parsed event"
        )

    def test_empty_input_returns_none_and_zero_events(self) -> None:
        from nexus.operators.dispatch import _parse_stream_json_output

        final_result, partial_text, event_count = _parse_stream_json_output("")

        assert final_result is None
        assert partial_text == ""
        assert event_count == 0

    def test_no_result_line_and_no_recognized_events_yields_none(self) -> None:
        """A plain single-blob payload (no ``type`` field at all -- the
        shape every pre-a3 test in this file used, and still uses via
        _make_proc's fallback default) must yield final_result=None so
        claude_dispatch falls back to parsing it as a bare JSON blob."""
        from nexus.operators.dispatch import _parse_stream_json_output

        final_result, partial_text, event_count = _parse_stream_json_output(
            '{"ok": true}'
        )

        assert final_result is None
        assert partial_text == ""
        assert event_count == 1


class TestTimeoutPartialCapture:
    """De-vacuation of the nexus-1at5 timeout drain (nexus-h33x8.6 a3).

    Fake subprocess: stdout.read() returns a chunk of real captured
    stream-json bytes (pre-result-event fixture lines, i.e. what a
    SIGKILL-before-completion would have left on the wire) on its first
    call, then hangs forever -- simulating a process that is still
    running past the timeout budget with partial output already emitted.
    """

    @pytest.mark.asyncio
    async def test_timeout_log_receives_reconstructed_partial_text(self) -> None:
        from nexus.operators.dispatch import claude_dispatch, OperatorTimeoutError

        lines = _load_fixture_lines("claude_dispatch_stream_json_sample.ndjson")
        pre_result_lines = [l for l in lines if json.loads(l).get("type") != "result"]
        partial_bytes = ("\n".join(pre_result_lines) + "\n").encode()

        proc = _make_proc()
        call_count = 0

        async def stdout_read(n: int = -1) -> bytes:
            # Call 1 (inside the drain loop): the partial NDJSON already on
            # the wire when the timeout fires -- returns immediately, gets
            # appended to the drain loop's accumulator.
            # Call 2: the drain loop asks for more; the subprocess is still
            # "thinking" past the timeout budget, so this hangs until
            # asyncio.wait_for cancels it.
            # Call 3+ (the post-kill mop-up read in claude_dispatch's
            # timeout except-block): mirrors the real post-EOF behaviour a
            # dead transport gives a StreamReader -- returns immediately,
            # empty.
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return partial_bytes
            if call_count == 2:
                await asyncio.sleep(999)
            return b""

        proc.stdout.read = stdout_read

        captured_log_calls: list[tuple] = []

        def fake_persist(timeout: float, partial_text: str, stderr: bytes, event_count: int) -> str:
            captured_log_calls.append((timeout, partial_text, stderr, event_count))
            return "/fake/log/path.log"

        with (
            patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)),
            patch("nexus.operators.dispatch._persist_timeout_log", side_effect=fake_persist),
        ):
            with pytest.raises(OperatorTimeoutError) as exc_info:
                await claude_dispatch("prompt", _SIMPLE_SCHEMA, timeout=0.05)

        # nexus-h33x8.6 critic (a4 precondition): the reconstructed partial
        # must be a STRUCTURED attribute on the exception, not only baked
        # into the message string or persisted to the log file -- a4 (hard
        # budget + partial results) needs to consume it directly.
        from pathlib import Path

        err = exc_info.value
        assert err.partial_text != "", "OperatorTimeoutError.partial_text is empty"
        assert '"ok"' in err.partial_text
        assert err.event_count == len(pre_result_lines)
        assert err.log_path == Path("/fake/log/path.log")

        assert captured_log_calls, "_persist_timeout_log was never called"
        _timeout, partial_text, _stderr, event_count = captured_log_calls[0]
        assert partial_text != "", (
            "the timeout log's reconstructed partial text is empty -- this is "
            "exactly the nexus-1at5 vacuous-drain bug a3 exists to fix"
        )
        assert '"ok"' in partial_text
        assert event_count == len(pre_result_lines)


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


class TestSimpleOperatorReturnShapeAndPromptContent:
    """Extract/Rank/Compare/Summarize/Generate: the simplest operator
    family, each a thin ``claude_dispatch`` wrapper. Two facets are
    identical in shape across all five — the returned dict carries the
    documented key, and the composed prompt embeds every caller-supplied
    argument verbatim — so they're tabled here rather than duplicated
    per operator. Operator-specific behavior (schema shape, multi-item
    scenarios, one/two-sided compare mode selection) stays in each
    operator's own class below.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "op_name, call_kwargs, fake_return, expected_key",
        [
            (
                "operator_extract",
                {"inputs": '["text one"]', "fields": "title"},
                {"extractions": [{"title": "Alpha"}]},
                "extractions",
            ),
            (
                "operator_rank",
                {"items": '["a", "b"]', "criterion": "relevance"},
                {"ranked": ["b", "a"]},
                "ranked",
            ),
            (
                "operator_compare",
                {"items": '["A", "B"]'},
                {"comparison": "A is better"},
                "comparison",
            ),
            (
                "operator_summarize",
                {"content": "Long content here."},
                {"summary": "Short summary.", "citations": []},
                "summary",
            ),
            (
                "operator_generate",
                {"template": "synthesis", "context": "some context"},
                {"output": "Generated text.", "citations": []},
                "output",
            ),
        ],
        ids=["extract", "rank", "compare", "summarize", "generate"],
    )
    async def test_returns_expected_key(
        self, monkeypatch, op_name, call_kwargs, fake_return, expected_key,
    ) -> None:
        import nexus.operators.dispatch as _mod
        import nexus.mcp.core as _core

        async def fake(*a, **kw):
            return fake_return

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        operator = getattr(_core, op_name)
        result = await operator(**call_kwargs)
        assert expected_key in result
        assert isinstance(result[expected_key], type(fake_return[expected_key]))

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "op_name, call_kwargs, fake_return, expected_substrings",
        [
            (
                "operator_extract",
                {"inputs": "some text", "fields": "author,year"},
                {"extractions": []},
                ["author", "year"],
            ),
            (
                "operator_extract",
                {"inputs": "unique sentinel value abc123", "fields": "x"},
                {"extractions": []},
                ["unique sentinel value abc123"],
            ),
            (
                "operator_rank",
                {"items": '["x"]', "criterion": "novelty"},
                {"ranked": []},
                ["novelty"],
            ),
            (
                "operator_summarize",
                {"content": "sentinel content xyz"},
                {"summary": "", "citations": []},
                ["sentinel content xyz"],
            ),
            (
                "operator_generate",
                {"template": "executive-summary", "context": "sentinel ctx abc"},
                {"output": "", "citations": []},
                ["sentinel ctx abc", "executive-summary"],
            ),
        ],
        ids=[
            "extract_fields",
            "extract_inputs_echoed",
            "rank_criterion",
            "summarize_content",
            "generate_template_and_context",
        ],
    )
    async def test_prompt_contains_call_args(
        self, monkeypatch, op_name, call_kwargs, fake_return, expected_substrings,
    ) -> None:
        import nexus.operators.dispatch as _mod
        import nexus.mcp.core as _core

        captured: list[str] = []

        async def fake(prompt, schema, timeout=60.0, model=None, **kw):
            captured.append(prompt)
            return fake_return

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        operator = getattr(_core, op_name)
        await operator(**call_kwargs)

        assert captured, "claude_dispatch never called"
        for substring in expected_substrings:
            assert substring in captured[0]


class TestOperatorCompare:

    @pytest.mark.asyncio
    async def test_one_sided_prompt_uses_items(self, monkeypatch) -> None:
        """One-sided compare (only items) keeps the original prompt shape."""
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import operator_compare

        captured = {}

        async def fake(prompt, schema, timeout, model=None, **kw):
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

        async def fake(prompt, schema, timeout, model=None, **kw):
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

        async def fake(prompt, schema, timeout, model=None, **kw):
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

        async def fake(prompt, schema, timeout, model=None, **kw):
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

        async def fake(prompt, schema, timeout, model=None, **kw):
            captured["prompt"] = prompt
            return {"comparison": "ok"}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        # items_a provided but items_b empty → degrade to one-sided on items.
        await operator_compare(items='["fallback"]', items_a="only-a", items_b="")
        assert "Compare the following items" in captured["prompt"]
        assert "fallback" in captured["prompt"]
        assert "Set A:" not in captured["prompt"]


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

        async def fake(prompt, schema, timeout=60.0, model=None, **kw):
            captured.append(prompt)
            return {"items": [], "rationale": []}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        # source="llm" forces the LLM substrate; default "auto" would
        # short-circuit on empty items via the RDR-089 SQL fast path
        # before ever reaching dispatch.
        await operator_filter(
            items='[]',
            criterion="peer-reviewed-only-sentinel",
            source="llm",
        )
        assert "peer-reviewed-only-sentinel" in captured[0]

    @pytest.mark.asyncio
    async def test_prompt_contains_items(self, monkeypatch) -> None:
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import operator_filter

        captured: list[str] = []

        async def fake(prompt, schema, timeout=60.0, model=None, **kw):
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

        async def fake(prompt, schema, timeout=60.0, model=None, **kw):
            captured_schemas.append(schema)
            return {"items": [], "rationale": []}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        # source="llm" — this test pins the LLM substrate's schema; the
        # RDR-089 SQL fast path short-circuits empty-items inputs and
        # would never reach dispatch under default "auto".
        await operator_filter(items='[]', criterion="x", source="llm")

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

        async def fake(prompt, schema, timeout=60.0, model=None, **kw):
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

    @pytest.mark.asyncio
    async def test_sql_hit_stamps_dispatch_source_marker(self, monkeypatch) -> None:
        """RDR-196 .p1b Gap 5 (nexus-nyry9.8): when the SQL fast path
        actually serves the request, the returned dict carries the
        ``_dispatch_source: "sql"`` marker plan_run reads to record
        source="sql" instead of the default "llm" inference. Empty
        items is a pure-SQL hit with no document_aspects dependency —
        monkeypatch claude_dispatch to raise so a wrongly-taken LLM
        fallback fails loudly instead of hanging on a real subprocess.
        """
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import operator_filter

        async def must_not_be_called(*a, **kw):
            raise AssertionError("SQL fast path should have short-circuited")

        monkeypatch.setattr(_mod, "claude_dispatch", must_not_be_called)
        result = await operator_filter(items="[]", criterion="x")
        assert result.get("_dispatch_source") == "sql"

    @pytest.mark.asyncio
    async def test_llm_fallback_has_no_dispatch_source_marker(self, monkeypatch) -> None:
        """The LLM-dispatched branch must NOT stamp a marker — plan_run's
        default is_operator_tool()-based inference ("llm") already
        covers it; a marker here would be redundant, not wrong, but its
        absence is the actual contract this test pins."""
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import operator_filter

        async def fake(prompt, schema, timeout=60.0, model=None, **kw):
            return {"items": [], "rationale": []}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        result = await operator_filter(items="[]", criterion="x", source="llm")
        assert "_dispatch_source" not in result


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

        async def fake(prompt, schema, timeout=60.0, model=None, **kw):
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

        async def fake(prompt, schema, timeout=60.0, model=None, **kw):
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

        async def fake(prompt, schema, timeout=60.0, model=None, **kw):
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

        async def fake(prompt, schema, timeout=60.0, model=None, **kw):
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

        async def fake(prompt, schema, timeout=60.0, model=None, **kw):
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

        async def fake(prompt, schema, timeout=60.0, model=None, **kw):
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

        async def fake(prompt, schema, timeout=60.0, model=None, **kw):
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

        async def fake(prompt, schema, timeout=60.0, model=None, **kw):
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

        async def fake(prompt, schema, timeout=60.0, model=None, **kw):
            captured_schemas.append(schema)
            return {"groups": []}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        # source="llm" — this test pins the LLM substrate's schema; the
        # RDR-089 SQL fast path short-circuits empty-items inputs and
        # would never reach dispatch under default "auto".
        await operator_groupby(items='[]', key="x", source="llm")

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

        async def fake(prompt, schema, timeout=60.0, model=None, **kw):
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

    @pytest.mark.asyncio
    async def test_sql_hit_stamps_dispatch_source_marker(self, monkeypatch) -> None:
        """RDR-196 .p1b Gap 5: empty items is a pure-SQL hit
        (``{"groups": []}``), no document_aspects dependency."""
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import operator_groupby

        async def must_not_be_called(*a, **kw):
            raise AssertionError("SQL fast path should have short-circuited")

        monkeypatch.setattr(_mod, "claude_dispatch", must_not_be_called)
        result = await operator_groupby(items="[]", key="x")
        assert result.get("_dispatch_source") == "sql"


class TestOperatorAggregate:
    """RDR-093 Phase 2: operator_aggregate reduces each group of items
    into a per-group summary keyed by the group's key_value. Receives
    groups pre-hydrated from operator_groupby's inline-items output
    (no runner-side nested-id hydration). Pairs with operator_groupby
    for the canonical filter -> groupby -> aggregate pipeline."""

    @pytest.mark.asyncio
    async def test_returns_aggregates_with_key_value_and_summary(
        self, monkeypatch,
    ) -> None:
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import operator_aggregate

        async def fake(*a, **kw):
            return {
                "aggregates": [
                    {"key_value": "GroupA", "summary": "Method-A wins"},
                    {"key_value": "GroupB", "summary": "Method-B wins"},
                    {"key_value": "GroupC", "summary": "Method-C wins"},
                ],
            }

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        groups_in = json.dumps([
            {"key_value": "GroupA",
             "items": [{"id": "a1"}, {"id": "a2"}, {"id": "a3"}]},
            {"key_value": "GroupB",
             "items": [{"id": "b1"}, {"id": "b2"}, {"id": "b3"}]},
            {"key_value": "GroupC",
             "items": [{"id": "c1"}, {"id": "c2"}, {"id": "c3"}]},
        ])
        result = await operator_aggregate(
            groups=groups_in,
            reducer="most-cited method",
        )
        assert "aggregates" in result
        assert isinstance(result["aggregates"], list)
        assert len(result["aggregates"]) == 3
        for a in result["aggregates"]:
            assert "key_value" in a and "summary" in a
            assert isinstance(a["key_value"], str)
            assert isinstance(a["summary"], str)

    @pytest.mark.asyncio
    async def test_prompt_contains_reducer_and_groups(
        self, monkeypatch,
    ) -> None:
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import operator_aggregate

        captured: list[str] = []

        async def fake(prompt, schema, timeout=60.0, model=None, **kw):
            captured.append(prompt)
            return {"aggregates": []}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        groups_in = json.dumps([
            {"key_value": "GroupSentinelABC", "items": []},
        ])
        await operator_aggregate(
            groups=groups_in,
            reducer="reducer-sentinel-xyz",
        )
        assert "reducer-sentinel-xyz" in captured[0]
        assert "GroupSentinelABC" in captured[0]

    @pytest.mark.asyncio
    async def test_schema_pins_aggregates_shape(self, monkeypatch) -> None:
        """Schema must declare {aggregates: [{key_value, summary}]}
        with both required fields. The flat schema (per RDR-093
        §Technical Design) is what makes the bundled groupby ->
        aggregate path composable."""
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import operator_aggregate

        captured_schemas: list[dict] = []

        async def fake(prompt, schema, timeout=60.0, model=None, **kw):
            captured_schemas.append(schema)
            return {"aggregates": []}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        await operator_aggregate(groups='[]', reducer="x")

        schema = captured_schemas[0]
        assert schema["type"] == "object"
        assert "aggregates" in schema["required"]
        agg_item = schema["properties"]["aggregates"]["items"]
        assert {"key_value", "summary"}.issubset(set(agg_item["required"]))
        assert agg_item["properties"]["key_value"]["type"] == "string"
        assert agg_item["properties"]["summary"]["type"] == "string"

    @pytest.mark.asyncio
    async def test_prompt_isolates_each_group_explicitly(
        self, monkeypatch,
    ) -> None:
        """RDR-093 §Risks and Mitigations: the prompt framing must
        explicitly isolate each group ('USING ONLY this group's items'
        or equivalent). Spike B (nexus-rojs) PASSED with this framing;
        future prompt edits that drop the isolation directive should
        trip this test."""
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import operator_aggregate

        captured: list[str] = []

        async def fake(prompt, schema, timeout=60.0, model=None, **kw):
            captured.append(prompt)
            return {"aggregates": []}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        await operator_aggregate(groups='[]', reducer="x")

        prompt = captured[0].lower()
        # Group-isolation directive must be present in some recognisable form.
        assert (
            "only" in prompt and "group" in prompt
        ), (
            "RDR-093 §Risks and Mitigations: aggregate prompt must "
            "carry a per-group isolation directive ('USING ONLY this "
            "group's items' or equivalent) per Spike B's validated "
            "framing"
        )

    @pytest.mark.asyncio
    async def test_three_groups_yields_three_aggregates(
        self, monkeypatch,
    ) -> None:
        """RDR-093 Test Plan scenario 3: operator_aggregate with 3 groups
        x 3 items + reducer 'most-cited method' returns exactly 3
        aggregates, each key_value preserved. Cross-group leakage is
        checked separately (Spike B and integration tests)."""
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import operator_aggregate

        async def fake(prompt, schema, timeout=60.0, model=None, **kw):
            return {
                "aggregates": [
                    {"key_value": "alpha", "summary": "alpha-wins-method"},
                    {"key_value": "beta", "summary": "beta-wins-method"},
                    {"key_value": "gamma", "summary": "gamma-wins-method"},
                ],
            }

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        groups_in = json.dumps([
            {"key_value": "alpha", "items": [{"id": "a1"}]},
            {"key_value": "beta", "items": [{"id": "b1"}]},
            {"key_value": "gamma", "items": [{"id": "c1"}]},
        ])
        result = await operator_aggregate(
            groups=groups_in,
            reducer="most-cited method",
        )
        assert len(result["aggregates"]) == 3
        keys = {a["key_value"] for a in result["aggregates"]}
        assert keys == {"alpha", "beta", "gamma"}

    @pytest.mark.asyncio
    async def test_sql_hit_stamps_dispatch_source_marker(self, monkeypatch) -> None:
        """RDR-196 .p1b Gap 5: an empty ``groups`` array with the
        recognised ``count`` reducer is a pure-SQL hit
        (``{"aggregates": []}``), no document_aspects dependency."""
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import operator_aggregate

        async def must_not_be_called(*a, **kw):
            raise AssertionError("SQL fast path should have short-circuited")

        monkeypatch.setattr(_mod, "claude_dispatch", must_not_be_called)
        result = await operator_aggregate(groups="[]", reducer="count")
        assert result.get("_dispatch_source") == "sql"


class TestFailureRecordAddressability:
    """nexus-ri56e (GH #1414 follow-ups): the failure branch must (a) make
    its HARNESS origin unambiguous — a populated message reads like an
    ordinary application error, but this is `claude -p` itself exiting
    non-zero, not a model answer; and (b) name where the durable record
    went (the timeout branch always has), or honestly say there is none."""

    @pytest.mark.asyncio
    async def test_message_states_harness_origin(self) -> None:
        from nexus.operators.dispatch import claude_dispatch, OperatorError

        proc = _make_proc(stdout=b'model-ish error text', returncode=1, stderr=b'')
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            with pytest.raises(OperatorError) as exc:
                await claude_dispatch("prompt", _SIMPLE_SCHEMA)
        msg = str(exc.value)
        assert "dispatch-harness failure, not a model answer" in msg
        assert "model-ish error text" in msg  # original detail preserved

    @pytest.mark.asyncio
    async def test_message_names_the_log_file_when_one_is_attached(
        self, tmp_path,
    ) -> None:
        import logging.handlers

        from nexus.operators.dispatch import claude_dispatch, OperatorError

        handler = logging.handlers.RotatingFileHandler(tmp_path / "mcp.log")
        logging.getLogger().addHandler(handler)
        try:
            proc = _make_proc(stdout=b'boom', returncode=1, stderr=b'')
            with patch(
                "asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc),
            ):
                with pytest.raises(OperatorError) as exc:
                    await claude_dispatch("prompt", _SIMPLE_SCHEMA)
        finally:
            logging.getLogger().removeHandler(handler)
            handler.close()
        msg = str(exc.value)
        assert "operator_dispatch_failed" in msg  # the event name to look for
        assert str(tmp_path / "mcp.log") in msg   # ...and exactly where

    @pytest.mark.asyncio
    async def test_message_says_no_record_in_plain_cli_mode(self) -> None:
        # No RotatingFileHandler attached (plain CLI): the exception must
        # say the message IS the record, never imply a greppable file.
        import logging.handlers

        from nexus.operators.dispatch import claude_dispatch, OperatorError

        root = logging.getLogger()
        assert not any(
            isinstance(h, logging.handlers.RotatingFileHandler)
            for h in root.handlers
        ), "test precondition: no file handler attached"
        proc = _make_proc(stdout=b'boom', returncode=1, stderr=b'')
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            with pytest.raises(OperatorError) as exc:
                await claude_dispatch("prompt", _SIMPLE_SCHEMA)
        assert "no log file attached" in str(exc.value)


class TestRolledUpFailureDemotion:
    """nexus-l1qpj: inside rolled_up_dispatch_failures() the per-failure
    choke-point event demotes WARNING -> INFO (the batch caller emits its
    own rollup); outside, the WARNING default stands."""

    @pytest.mark.asyncio
    async def test_demoted_inside_scope(self) -> None:
        from nexus.operators.dispatch import (
            claude_dispatch,
            rolled_up_dispatch_failures,
            OperatorError,
        )

        proc = _make_proc(stdout=b'x', returncode=1, stderr=b'')
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            with patch("nexus.operators.dispatch._log") as mock_log:
                with rolled_up_dispatch_failures():
                    with pytest.raises(OperatorError):
                        await claude_dispatch("prompt", _SIMPLE_SCHEMA)
        assert mock_log.info.called
        assert not mock_log.warning.called

    @pytest.mark.asyncio
    async def test_warning_outside_scope(self) -> None:
        from nexus.operators.dispatch import claude_dispatch, OperatorError

        proc = _make_proc(stdout=b'x', returncode=1, stderr=b'')
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            with patch("nexus.operators.dispatch._log") as mock_log:
                with pytest.raises(OperatorError):
                    await claude_dispatch("prompt", _SIMPLE_SCHEMA)
        assert mock_log.warning.called
        assert not mock_log.info.called

    def test_scope_restores_on_exit(self) -> None:
        from nexus.operators.dispatch import (
            _ROLLED_UP,
            rolled_up_dispatch_failures,
        )

        assert _ROLLED_UP.get() is False
        with rolled_up_dispatch_failures():
            assert _ROLLED_UP.get() is True
        assert _ROLLED_UP.get() is False


# ── DispatchUsage / cost-usage capture (RDR-196 .p1a, nexus-nyry9.7) ────────
#
# ``claude -p --output-format stream-json``'s terminal result event carries
# ``total_cost_usd`` / ``usage.*`` / ``modelUsage`` -- dispatch.py parses it
# (``_parse_stream_json_output``) and, before this bead, discarded every
# cost/usage field. Field names below are FIXTURE-VERIFIED against
# ``tests/fixtures/claude_dispatch_stream_json_sample.ndjson`` line 12 (the
# terminal ``result`` event), not the RDR-196 Technical Design section's
# illustrative sketch, which used ``elapsed_ms`` for what the wire payload
# actually calls ``duration_ms`` -- see the correction block in
# docs/rdr/rdr-196-cost-aware-nx-answer.md's Technical Design section.
#
# ``claude_dispatch``'s existing return contract (a bare dict -- the parsed
# ``structured_output`` or the raw wrapper) is UNCHANGED: all ~17 non-
# aspect_extractor.py call sites (mcp/core.py, plans/bundle.py,
# commands/taxonomy_cmd.py) do ``return await claude_dispatch(...)`` with no
# tuple-unpacking, so returning a tuple would break every one of them
# despite the RDR sketch's "existing callers are unaffected" claim. Usage is
# instead reachable via an opt-in ``usage_sink: list[DispatchUsage] | None``
# out-param: ``None`` (default) is a complete no-op, byte-identical to the
# pre-change behaviour for all existing callers; a caller that wants usage
# passes its own list and reads ``sink[0]`` after the ``await`` returns.

_RESULT_EVENT_LINE = 12  # 1-indexed line in the fixture; the terminal "result" event


def _fixture_result_event() -> dict:
    lines = _load_fixture_lines("claude_dispatch_stream_json_sample.ndjson")
    obj = json.loads(lines[_RESULT_EVENT_LINE - 1])
    assert obj.get("type") == "result", "fixture line 12 must be the terminal result event"
    return obj


class TestParseDispatchUsage:
    """Unit tests for ``_parse_dispatch_usage`` against the real captured
    fixture's terminal result event."""

    def test_parses_exact_values_from_fixture(self) -> None:
        from nexus.operators.dispatch import _parse_dispatch_usage

        usage = _parse_dispatch_usage(_fixture_result_event())

        assert usage.cost_usd == 0.5414490000000001
        assert usage.input_tokens == 2
        assert usage.output_tokens == 52
        assert usage.cache_creation_input_tokens == 25989
        assert usage.cache_read_input_tokens == 19049
        assert usage.duration_ms == 3220
        assert usage.duration_api_ms == 3135
        assert usage.num_turns == 2
        assert usage.model == "claude-fable-5"
        assert set(usage.model_usage.keys()) == {"claude-fable-5"}
        mu = usage.model_usage["claude-fable-5"]
        assert mu.canonical_model == "claude-fable-5"
        assert mu.input_tokens == 2
        assert mu.output_tokens == 52
        assert mu.cache_read_input_tokens == 19049
        assert mu.cache_creation_input_tokens == 25989
        assert mu.cost_usd == 0.5414490000000001

    def test_no_result_event_yields_all_none_and_warns(self) -> None:
        """A legacy bare-JSON test double (no stream-json result event at
        all) must yield an all-``None`` DispatchUsage, never 0.0 -- 0.0
        reads as "this call was free", not "we don't know"."""
        from structlog.testing import capture_logs

        from nexus.operators.dispatch import _parse_dispatch_usage

        with capture_logs() as cap:
            usage = _parse_dispatch_usage(None)

        assert usage.cost_usd is None
        assert usage.input_tokens is None
        assert usage.output_tokens is None
        assert usage.model is None
        assert usage.model_usage == {}
        assert any(e.get("event") == "dispatch_usage_no_result_event" for e in cap)

    def test_absent_fields_record_none_not_zero_and_warn(self) -> None:
        """Doctored copy of the fixture's result event with total_cost_usd,
        usage, and modelUsage stripped -- the absent-field path per the
        RDR's risk register. Every affected field must be None, never 0.0,
        and a warning must name what was missing."""
        from structlog.testing import capture_logs

        from nexus.operators.dispatch import _parse_dispatch_usage

        doctored = dict(_fixture_result_event())
        del doctored["total_cost_usd"]
        del doctored["usage"]
        del doctored["modelUsage"]

        with capture_logs() as cap:
            usage = _parse_dispatch_usage(doctored)

        assert usage.cost_usd is None
        assert usage.input_tokens is None
        assert usage.output_tokens is None
        assert usage.cache_creation_input_tokens is None
        assert usage.cache_read_input_tokens is None
        assert usage.model is None
        assert usage.model_usage == {}
        # duration_ms/duration_api_ms/num_turns are untouched by the
        # doctoring and must still parse normally.
        assert usage.duration_ms == 3220
        assert usage.duration_api_ms == 3135
        assert usage.num_turns == 2

        warnings = [e for e in cap if e.get("event") == "dispatch_usage_fields_missing"]
        assert warnings, f"expected a dispatch_usage_fields_missing warning: {cap}"
        missing = warnings[0].get("missing", [])
        assert "total_cost_usd" in missing
        assert "usage" in missing
        assert "modelUsage" in missing

    def test_fixture_result_event_contains_cost_and_usage_fields(self) -> None:
        """Non-vacuity guard (bead item 3): if a future fixture
        regeneration drops the cost/usage fields, THIS must fail loudly --
        otherwise every DispatchUsage test above would keep passing on an
        empty result event and the whole telemetry arc silently measures
        nothing. See the handback for a demonstration that this assertion
        actually fails on a stripped copy."""
        result = _fixture_result_event()
        assert "total_cost_usd" in result
        assert "usage" in result
        assert "modelUsage" in result
        assert isinstance(result["usage"], dict) and result["usage"], (
            "usage must be a populated dict, not empty/absent"
        )
        assert isinstance(result["modelUsage"], dict) and result["modelUsage"], (
            "modelUsage must be a populated dict, not empty/absent"
        )

    def test_single_model_alias_key_records_canonical_not_key(self) -> None:
        """review finding (substantive-critic Significant #2): the real
        fixture's single modelUsage key happens to equal its
        canonicalModel ("claude-fable-5" both ways), so
        test_parses_exact_values_from_fixture cannot distinguish reading
        ``canonicalModel`` from falling back to the dict key -- exactly
        what 196-R3 ("record the canonical id, never the requested
        alias") needs distinguished. Doctor the map key to a requested
        alias distinct from its canonicalModel and assert the id that
        actually gets recorded is the canonical one."""
        from nexus.operators.dispatch import _parse_dispatch_usage

        doctored = dict(_fixture_result_event())
        doctored["modelUsage"] = {
            "sonnet": {  # the requested alias -- must NOT be what's recorded
                "inputTokens": 2,
                "outputTokens": 52,
                "cacheReadInputTokens": 19049,
                "cacheCreationInputTokens": 25989,
                "costUSD": 0.5414490000000001,
                "canonicalModel": "claude-sonnet-5-20260101",
            },
        }

        usage = _parse_dispatch_usage(doctored)

        assert usage.model == "claude-sonnet-5-20260101", (
            f"expected the canonical id, not the requested alias 'sonnet': got {usage.model!r}"
        )
        assert set(usage.model_usage.keys()) == {"sonnet"}, (
            "the map is still keyed by the alias -- only the recorded model id must be canonical"
        )
        assert usage.model_usage["sonnet"].canonical_model == "claude-sonnet-5-20260101"

    def test_two_models_no_cross_wiring_between_top_level_and_per_model_usage(self) -> None:
        """review finding (substantive-critic Significant #2): the real
        fixture is single-turn/single-model, so top-level ``usage.*`` is
        numerically IDENTICAL to the one ``modelUsage`` entry's numbers --
        a mis-sourced field (reading from the wrong dict) would still pass
        every exact-value assertion. Doctor two modelUsage entries, both
        with a distinct alias/canonical pair, and top-level usage/cost
        numbers that match NEITHER entry, then assert (a) each entry's
        canonical_model is read from canonicalModel, not its key, (b) the
        top-level DispatchUsage fields come from the top-level ``usage``/
        ``total_cost_usd``, not either modelUsage entry, and (c) the
        single-value ``model`` convenience field stays None -- genuinely
        ambiguous with two distinct canonical models, not a silent pick."""
        from nexus.operators.dispatch import _parse_dispatch_usage

        doctored = dict(_fixture_result_event())
        doctored["total_cost_usd"] = 9.0  # distinct from both entries below
        doctored["usage"] = {
            "input_tokens": 900,
            "output_tokens": 901,
            "cache_creation_input_tokens": 902,
            "cache_read_input_tokens": 903,
        }
        doctored["modelUsage"] = {
            "sonnet": {
                "inputTokens": 100, "outputTokens": 101,
                "cacheReadInputTokens": 102, "cacheCreationInputTokens": 103,
                "costUSD": 1.0,
                "canonicalModel": "claude-sonnet-5-20260101",
            },
            "haiku": {
                "inputTokens": 200, "outputTokens": 201,
                "cacheReadInputTokens": 202, "cacheCreationInputTokens": 203,
                "costUSD": 2.0,
                "canonicalModel": "claude-haiku-5-20260101",
            },
        }

        usage = _parse_dispatch_usage(doctored)

        assert usage.model_usage["sonnet"].canonical_model == "claude-sonnet-5-20260101"
        assert usage.model_usage["haiku"].canonical_model == "claude-haiku-5-20260101"
        assert usage.model is None, (
            "two distinct canonical models is genuinely ambiguous for the "
            "single-value convenience field -- must not silently pick one"
        )
        # The load-bearing anti-cross-wiring assertions: these values exist
        # ONLY at the top level (900/901/9.0), never in either modelUsage
        # entry (100/101/1.0 or 200/201/2.0) -- a mis-sourced field would
        # fail these even though it might pass a single-model fixture.
        assert usage.cost_usd == 9.0
        assert usage.input_tokens == 900
        assert usage.output_tokens == 901
        assert usage.cache_creation_input_tokens == 902
        assert usage.cache_read_input_tokens == 903


class TestClaudeDispatchUsageSink:
    """``usage_sink`` is a no-op by default (existing ~17 call sites pass
    nothing); opting in gets exactly one DispatchUsage per successful
    dispatch, without altering the returned dict."""

    @pytest.mark.asyncio
    async def test_default_none_is_unaffected(self) -> None:
        """No usage_sink passed -- byte-identical to pre-change behaviour."""
        from nexus.operators.dispatch import claude_dispatch

        proc = _make_proc(stdout=b'{"ok": true}')
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            result = await claude_dispatch("prompt", _SIMPLE_SCHEMA)

        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_usage_sink_populated_from_real_fixture_stream(self) -> None:
        """Feed the FULL real stream-json fixture (every event, terminal
        result included) as stdout and confirm usage_sink receives a
        DispatchUsage matching the fixture's result event, while the
        returned structured-output dict is unaffected."""
        from nexus.operators.dispatch import claude_dispatch, DispatchUsage

        lines = _load_fixture_lines("claude_dispatch_stream_json_sample.ndjson")
        raw = ("\n".join(lines) + "\n").encode()
        proc = _make_proc(stdout=raw)

        sink: list[DispatchUsage] = []
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            result = await claude_dispatch("prompt", _SIMPLE_SCHEMA, usage_sink=sink)

        assert result == {"ok": True}, "usage_sink must not change claude_dispatch's return contract"
        assert len(sink) == 1
        usage = sink[0]
        assert usage.cost_usd == 0.5414490000000001
        assert usage.input_tokens == 2
        assert usage.output_tokens == 52
        assert usage.model == "claude-fable-5"

    @pytest.mark.asyncio
    async def test_usage_sink_none_when_legacy_bare_blob(self) -> None:
        """A legacy bare-JSON test double (no result event) with
        usage_sink set still appends exactly one DispatchUsage -- all
        fields None, never 0.0 -- rather than silently appending nothing
        or fabricating a zero-cost record."""
        from nexus.operators.dispatch import claude_dispatch, DispatchUsage

        proc = _make_proc(stdout=b'{"ok": true}')
        sink: list[DispatchUsage] = []
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            result = await claude_dispatch("prompt", _SIMPLE_SCHEMA, usage_sink=sink)

        assert result == {"ok": True}
        assert len(sink) == 1
        assert sink[0].cost_usd is None
        assert sink[0].model is None


class TestUsageSinkAppendsBeforeSubsequentRaises:
    """review findings (code-review-expert Important + substantive-critic
    Significant #1): ``usage_sink.append()`` runs right after
    ``_parse_stream_json_output(raw)``, which is BEFORE three of
    ``claude_dispatch``'s six possible raises -- the append and the raise
    both happen on those three paths, not one or the other. Coordinator
    decision: adopt that as the contract (real spend is recorded whenever
    a result envelope parsed, even when the dispatch then raises -- this
    serves the arc's cost-tracking goal and .p1b builds on it), correct
    the docstring to say so precisely (done), and cover all three paths
    here -- previously zero test exercised usage_sink through any of
    them."""

    @pytest.mark.asyncio
    async def test_is_error_true_still_appends_usage_then_raises(self) -> None:
        """A result envelope with is_error=true (claude exited 0 but
        reported an application-level error) still carries real,
        non-zero spend -- the append must happen before the
        OperatorError raise, not be skipped by it."""
        from nexus.operators.dispatch import claude_dispatch, DispatchUsage, OperatorError

        ndjson = "\n".join([
            '{"type":"system","subtype":"init","cwd":"/tmp","session_id":"s1"}',
            '{"type":"result","is_error":true,'
            '"result":"claude reported: rate limit exceeded for this account",'
            '"subtype":"error_during_execution","structured_output":null,'
            '"total_cost_usd":0.42,"duration_ms":1000,"duration_api_ms":900,'
            '"num_turns":1,'
            '"usage":{"input_tokens":10,"output_tokens":20,'
            '"cache_creation_input_tokens":0,"cache_read_input_tokens":0},'
            '"modelUsage":{"claude-fable-5":{"inputTokens":10,"outputTokens":20,'
            '"cacheReadInputTokens":0,"cacheCreationInputTokens":0,'
            '"costUSD":0.42,"canonicalModel":"claude-fable-5"}}}',
        ])
        proc = _make_proc(stdout=ndjson.encode(), returncode=0, stderr=b"")
        sink: list[DispatchUsage] = []

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            with pytest.raises(OperatorError, match="rate limit exceeded"):
                await claude_dispatch("prompt", _SIMPLE_SCHEMA, usage_sink=sink)

        assert len(sink) == 1, "the append must happen even though the call raises"
        assert sink[0].cost_usd == 0.42
        assert sink[0].input_tokens == 10
        assert sink[0].model == "claude-fable-5"

    @pytest.mark.asyncio
    async def test_null_structured_output_still_appends_usage_then_raises(self) -> None:
        """is_error=false but structured_output=null: still a parsed
        envelope with real usage, still appended before the
        OperatorOutputError raise."""
        from nexus.operators.dispatch import claude_dispatch, DispatchUsage, OperatorOutputError

        ndjson = (
            '{"type":"result","is_error":false,'
            '"result":"","structured_output":null,'
            '"total_cost_usd":0.13,"duration_ms":500,"duration_api_ms":450,'
            '"num_turns":1,'
            '"usage":{"input_tokens":5,"output_tokens":6,'
            '"cache_creation_input_tokens":0,"cache_read_input_tokens":0},'
            '"modelUsage":{"claude-fable-5":{"inputTokens":5,"outputTokens":6,'
            '"cacheReadInputTokens":0,"cacheCreationInputTokens":0,'
            '"costUSD":0.13,"canonicalModel":"claude-fable-5"}}}'
        )
        proc = _make_proc(stdout=ndjson.encode(), returncode=0, stderr=b"")
        sink: list[DispatchUsage] = []

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            with pytest.raises(OperatorOutputError, match="null structured_output"):
                await claude_dispatch("prompt", _SIMPLE_SCHEMA, usage_sink=sink)

        assert len(sink) == 1, "the append must happen even though the call raises"
        assert sink[0].cost_usd == 0.13
        assert sink[0].output_tokens == 6

    @pytest.mark.asyncio
    async def test_invalid_json_fallback_still_appends_all_none_usage_then_raises(self) -> None:
        """No stream-json result event at all AND the raw blob fails
        json.loads: the append still runs (with the all-None DispatchUsage
        _parse_dispatch_usage produces when final_result is None), then
        the OperatorOutputError fires."""
        from nexus.operators.dispatch import claude_dispatch, DispatchUsage, OperatorOutputError

        proc = _make_proc(stdout=b'not valid json {{{{', returncode=0, stderr=b"")
        sink: list[DispatchUsage] = []

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            with pytest.raises(OperatorOutputError, match="not valid JSON"):
                await claude_dispatch("prompt", _SIMPLE_SCHEMA, usage_sink=sink)

        assert len(sink) == 1, "the append must happen even though the call raises"
        assert sink[0].cost_usd is None
        assert sink[0].model is None


def _result_ndjson(
    *, cost_usd: float, input_tokens: int, output_tokens: int,
    model: str, structured_output: dict | None = None,
) -> bytes:
    """Build a minimal-but-real terminal stream-json result event, shaped
    like the fixture (RDR-196 .p1b Gap-1 addendum tests)."""
    payload = {
        "type": "result", "is_error": False,
        "result": "", "structured_output": structured_output or {"summary": "ok"},
        "total_cost_usd": cost_usd,
        "duration_ms": 1000, "duration_api_ms": 900, "num_turns": 1,
        "usage": {
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
        },
        "modelUsage": {
            model: {
                "inputTokens": input_tokens, "outputTokens": output_tokens,
                "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0,
                "costUSD": cost_usd, "canonicalModel": model,
            },
        },
    }
    return json.dumps(payload).encode()


class TestAmbientUsageSink:
    """RDR-196 .p1b Gap-1 addendum (nexus-nyry9.8 coordinator directive,
    2026-08-20): ``ambient_usage_sink`` closes the isolated/bundle-
    fallback usage-observability gap the original .p1b handback left
    open -- an operator_* MCP tool's OWN internal claude_dispatch call,
    several stack frames past anywhere a caller could pass an explicit
    usage_sink kwarg, is now captured via a contextvar instead."""

    @pytest.mark.asyncio
    async def test_ambient_sink_is_none_outside_any_scope(self) -> None:
        from nexus.operators.dispatch import _ambient_usage_sink

        assert _ambient_usage_sink.get() is None

    @pytest.mark.asyncio
    async def test_explicit_and_ambient_sinks_both_receive_same_record(self) -> None:
        """Both sinks populated from ONE dispatch; the SAME DispatchUsage
        instance lands in both -- parsed once, never two independently-
        parsed copies."""
        from nexus.operators.dispatch import (
            ambient_usage_sink, claude_dispatch, DispatchUsage,
        )

        proc = _make_proc(
            stdout=_result_ndjson(
                cost_usd=0.07, input_tokens=11, output_tokens=22,
                model="claude-sonnet-5-20260101",
            ),
            returncode=0, stderr=b"",
        )
        explicit_sink: list[DispatchUsage] = []
        ambient_sink: list[DispatchUsage] = []

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            with ambient_usage_sink(ambient_sink):
                await claude_dispatch("prompt", _SIMPLE_SCHEMA, usage_sink=explicit_sink)

        assert len(explicit_sink) == 1
        assert len(ambient_sink) == 1
        assert explicit_sink[0] is ambient_sink[0]
        assert explicit_sink[0].cost_usd == 0.07
        assert explicit_sink[0].model == "claude-sonnet-5-20260101"

    @pytest.mark.asyncio
    async def test_ambient_sink_survives_child_task(self) -> None:
        """asyncio Tasks copy the current contextvars.Context at creation
        -- a claude_dispatch call issued from inside an
        asyncio.create_task() spawned WHILE the ambient scope is active
        must still be captured. This is the exact shape an operator that
        internally fans out sub-dispatches would hit."""
        from nexus.operators.dispatch import (
            ambient_usage_sink, claude_dispatch, DispatchUsage,
        )

        proc = _make_proc(
            stdout=_result_ndjson(
                cost_usd=0.09, input_tokens=5, output_tokens=6,
                model="claude-sonnet-5-20260101",
            ),
            returncode=0, stderr=b"",
        )
        sink: list[DispatchUsage] = []

        async def _child() -> None:
            await claude_dispatch("prompt", _SIMPLE_SCHEMA)

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            with ambient_usage_sink(sink):
                task = asyncio.create_task(_child())
                await task

        assert len(sink) == 1, "ambient sink must survive a child asyncio.Task"
        assert sink[0].cost_usd == 0.09

    @pytest.mark.asyncio
    async def test_two_sequential_scopes_no_cross_contamination(self) -> None:
        """Two consecutive ambient-sink scopes, distinguished by cost --
        each sink must contain ONLY its own scope's usage, not the
        other's, and the ambient var must not leak state between them."""
        from nexus.operators.dispatch import (
            _ambient_usage_sink, ambient_usage_sink, claude_dispatch, DispatchUsage,
        )

        sink_a: list[DispatchUsage] = []
        sink_b: list[DispatchUsage] = []

        proc_a = _make_proc(
            stdout=_result_ndjson(
                cost_usd=0.11, input_tokens=1, output_tokens=2,
                model="claude-sonnet-5-20260101",
            ),
            returncode=0, stderr=b"",
        )
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc_a)):
            with ambient_usage_sink(sink_a):
                await claude_dispatch("prompt-a", _SIMPLE_SCHEMA)

        assert _ambient_usage_sink.get() is None, "scope must fully reset after exit"

        proc_b = _make_proc(
            stdout=_result_ndjson(
                cost_usd=0.22, input_tokens=3, output_tokens=4,
                model="claude-opus-4-20260514",
            ),
            returncode=0, stderr=b"",
        )
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc_b)):
            with ambient_usage_sink(sink_b):
                await claude_dispatch("prompt-b", _SIMPLE_SCHEMA)

        assert len(sink_a) == 1 and sink_a[0].cost_usd == 0.11
        assert len(sink_b) == 1 and sink_b[0].cost_usd == 0.22

    @pytest.mark.asyncio
    async def test_isolated_operator_summarize_records_real_usage(self) -> None:
        """End-to-end through the REAL call chain: operator_summarize (an
        MCP tool that owns its own internal claude_dispatch call, never
        touched by plans/runner.py directly) still gets captured because
        the ambient sink is set around the CALLER's dispatch, not around
        claude_dispatch's own call site."""
        from nexus.mcp.core import operator_summarize
        from nexus.operators.dispatch import ambient_usage_sink, DispatchUsage

        proc = _make_proc(
            stdout=_result_ndjson(
                cost_usd=0.031, input_tokens=40, output_tokens=15,
                model="claude-sonnet-5-20260101",
                structured_output={"summary": "a concise summary"},
            ),
            returncode=0, stderr=b"",
        )
        sink: list[DispatchUsage] = []

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            with ambient_usage_sink(sink):
                result = await operator_summarize(content="some long content")

        assert result == {"summary": "a concise summary"}
        assert len(sink) == 1
        assert sink[0].model == "claude-sonnet-5-20260101"
        assert sink[0].cost_usd == 0.031
        assert sink[0].input_tokens == 40


@pytest.mark.integration
class TestClaudeDispatchLiveUsage:
    """Live claude -p dispatch records non-zero cost -- the fixture alone
    only proves the parser handles a captured shape, not that it still
    matches production's actual wire payload. Skipped by default; requires
    ``claude`` on PATH with valid credentials.

    Run with: uv run pytest -m integration tests/test_operator_dispatch.py -k LiveUsage
    """

    @staticmethod
    def _claude_auth_available() -> bool:
        import subprocess

        try:
            result = subprocess.run(
                ["claude", "auth", "status", "--json"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                return False
            data = json.loads(result.stdout)
            return bool(data.get("loggedIn") or data.get("isLoggedIn"))
        except Exception:
            return False

    @pytest.mark.asyncio
    async def test_live_dispatch_records_nonzero_cost(self) -> None:
        if not self._claude_auth_available():
            pytest.skip("claude CLI not on PATH or not authenticated -- live dispatch skipped")

        from nexus.operators.dispatch import claude_dispatch, DispatchUsage

        sink: list[DispatchUsage] = []
        result = await claude_dispatch(
            'Respond with exactly {"ok": true} and nothing else.',
            _SIMPLE_SCHEMA,
            timeout=60.0,
            usage_sink=sink,
        )

        assert isinstance(result, dict)
        assert len(sink) == 1, f"expected exactly one usage record from a live dispatch, got {sink}"
        usage = sink[0]
        assert usage.cost_usd is not None and usage.cost_usd > 0, (
            f"live dispatch must record a non-zero cost, got {usage.cost_usd!r}"
        )
        assert usage.input_tokens is not None and usage.input_tokens > 0
        assert usage.model is not None and usage.model != "", (
            "live dispatch must record a canonical model id"
        )


@pytest.mark.integration
class TestClaudeDispatchPerOperatorSchemaCheapTier:
    """RDR-196 .p2b DO 5 (nexus-nyry9.15): every operator's own real
    json_schema, dispatched at the cheap tier, must still produce
    schema-conforming structured output. Per the RDR's failure mode, a
    schema-conformance error on the cheap tier surfaces as an operator
    failure -- exercising all 10 real per-operator schemas here (not one
    blanket schema) is what keeps .p2c's quality-proxy measurement from
    confounding a genuine tier-quality regression with a plumbing bug
    this test would have caught first.

    Schemas and prompt shapes are copied verbatim from the 10
    ``@mcp.tool``-registered operators in ``src/nexus/mcp/core.py``
    (``operator_extract`` .. ``operator_aggregate``) -- read-only source,
    not re-derived or simplified, so a schema drift in core.py is a
    genuine signal here rather than noise from two independently
    maintained copies.

    Skipped by default; requires ``claude`` on PATH with valid
    credentials.

    Run with:
      uv run pytest -m integration tests/test_operator_dispatch.py -k PerOperatorSchema
    """

    @staticmethod
    def _claude_auth_available() -> bool:
        import subprocess

        try:
            result = subprocess.run(
                ["claude", "auth", "status", "--json"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                return False
            data = json.loads(result.stdout)
            return bool(data.get("loggedIn") or data.get("isLoggedIn"))
        except Exception:
            return False

    # (operator_name, prompt, schema) -- schema/prompt copied verbatim
    # from src/nexus/mcp/core.py as of RDR-196 .p2b.
    _CHECK_EVIDENCE_ITEM_SCHEMA = {
        "type": "object",
        "required": ["item_id", "quote", "role"],
        "properties": {
            "item_id": {"type": "string"},
            "quote": {"type": "string"},
            "role": {
                "type": "string",
                "enum": ["supports", "contradicts", "neutral"],
            },
        },
    }

    _CASES = [
        (
            "operator_extract",
            "Extract the following fields from each item: name\n\n"
            'Items:\n[{"name": "Alice", "age": 30}]',
            {
                "type": "object",
                "required": ["extractions"],
                "properties": {
                    "extractions": {"type": "array", "items": {"type": "object"}},
                },
            },
        ),
        (
            "operator_rank",
            "Rank the following items by size.\n"
            "Return them in ranked order, best first.\n\n"
            'Items:\n["small", "medium", "large"]',
            {
                "type": "object",
                "required": ["ranked"],
                "properties": {
                    "ranked": {"type": "array", "items": {"type": "string"}},
                },
            },
        ),
        (
            "operator_compare",
            "Compare the following items.\n\n"
            'Items:\n["apple", "orange"]',
            {
                "type": "object",
                "required": ["comparison"],
                "properties": {"comparison": {"type": "string"}},
            },
        ),
        (
            "operator_summarize",
            "Summarize the following content concisely.\n\n"
            "Nexus is a semantic search and knowledge management CLI.",
            {
                "type": "object",
                "required": ["summary"],
                "properties": {
                    "summary": {"type": "string"},
                    "citations": {"type": "array", "items": {"type": "string"}},
                },
            },
        ),
        (
            "operator_generate",
            "Generate a one-sentence description.\n\n"
            "Context:\nA CLI tool for semantic search.",
            {
                "type": "object",
                "required": ["output"],
                "properties": {
                    "output": {"type": "string"},
                    "citations": {"type": "array", "items": {"type": "string"}},
                },
            },
        ),
        (
            "operator_filter",
            "Filter the following items by this criterion: is a fruit\n"
            "Return only the items that satisfy the criterion in the "
            "'items' array. Populate 'rationale' with one entry per "
            "input item, keyed by the item's id, giving the reason each "
            "item was kept or rejected. The output 'items' array must "
            "be a subset of the input; never add synthetic items.\n\n"
            'Items:\n[{"id": "1", "name": "apple"}, {"id": "2", "name": "car"}]',
            {
                "type": "object",
                "required": ["items", "rationale"],
                "properties": {
                    "items": {"type": "array", "items": {"type": "object"}},
                    "rationale": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["id", "reason"],
                            "properties": {
                                "id": {"type": "string"},
                                "reason": {"type": "string"},
                            },
                        },
                    },
                },
            },
        ),
        (
            "operator_check",
            "Check whether the following items are consistent with this "
            "claim or question: all items are fruit\n"
            "Set ok=true when every item supports the claim, false when "
            "at least one item contradicts it. Populate 'evidence' with "
            "a record per item containing a short grounding 'quote' and "
            "a 'role' of 'supports', 'contradicts', or 'neutral'.\n\n"
            'Items:\n[{"id": "1", "name": "apple"}, {"id": "2", "name": "car"}]',
            {
                "type": "object",
                "required": ["ok", "evidence"],
                "properties": {
                    "ok": {"type": "boolean"},
                    "evidence": {
                        "type": "array",
                        "items": _CHECK_EVIDENCE_ITEM_SCHEMA,
                    },
                },
            },
        ),
        (
            "operator_verify",
            "Verify whether the following claim is grounded in the "
            "evidence provided.\n\n"
            "Claim: the sky is blue\n\n"
            "Evidence:\nOn a clear day, the sky appears blue due to "
            "Rayleigh scattering.\n\n"
            "Set verified=true only when the claim is directly "
            "supported by the evidence. Provide a concise 'reason'. "
            "Populate 'citations' with locators.",
            {
                "type": "object",
                "required": ["verified", "reason", "citations"],
                "properties": {
                    "verified": {"type": "boolean"},
                    "reason": {"type": "string"},
                    "citations": {"type": "array", "items": {"type": "string"}},
                },
            },
        ),
        (
            "operator_groupby",
            "Partition the following items by this key: category\n"
            "Output a list of groups. Each group has a string "
            "`key_value` and an `items` array carrying each item's "
            "full content INLINE.\n\n"
            'Items:\n[{"id": "1", "name": "apple", "category": "fruit"}, '
            '{"id": "2", "name": "car", "category": "vehicle"}]',
            {
                "type": "object",
                "required": ["groups"],
                "properties": {
                    "groups": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["key_value", "items"],
                            "properties": {
                                "key_value": {"type": "string"},
                                "items": {"type": "array", "items": {"type": "object"}},
                            },
                        },
                    },
                },
            },
        ),
        (
            "operator_aggregate",
            "Reduce each group of items into a per-group summary using "
            "this reducer instruction: count the items\n\n"
            "Output one aggregate per input group, preserving the "
            "group's `key_value` verbatim.\n\n"
            'Groups:\n[{"key_value": "fruit", "items": [{"id": "1", "name": "apple"}]}]',
            {
                "type": "object",
                "required": ["aggregates"],
                "properties": {
                    "aggregates": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["key_value", "summary"],
                            "properties": {
                                "key_value": {"type": "string"},
                                "summary": {"type": "string"},
                            },
                        },
                    },
                },
            },
        ),
    ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "operator_name, prompt, schema", _CASES, ids=[c[0] for c in _CASES],
    )
    async def test_cheap_tier_produces_schema_conforming_output(
        self, operator_name: str, prompt: str, schema: dict,
    ) -> None:
        if not self._claude_auth_available():
            pytest.skip("claude CLI not on PATH or not authenticated -- live dispatch skipped")

        import jsonschema

        from nexus.operators.dispatch import claude_dispatch
        from nexus.operators.model_tiers import resolve_model_for_tier

        result = await claude_dispatch(
            prompt, schema, timeout=60.0,
            model=resolve_model_for_tier("cheap"), operator=operator_name,
        )

        jsonschema.validate(result, schema)
