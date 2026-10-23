# SPDX-License-Identifier: AGPL-3.0-or-later
"""``claude_dispatch`` -> ``CliBackend`` move is byte-identical (nexus-2k66t.3 P1).

``GOLDEN`` was recorded by running ``capture_all()`` against the tree BEFORE
the dispatch body moved into ``CliBackend`` (develop ab583e34c); the test
re-runs the same matrix after the move and compares. A regression in argv,
env, cwd, the stdin file, the return value or the usage sink shows up as a
diff against the recorded literal, not against a re-derivation of itself.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests._module_seam import patch_in

from nexus.operators.dispatch import DispatchUsage, claude_dispatch

_SCHEMA = {"type": "object", "properties": {"result": {"type": "string"}}}
_FIXED_UUID = "00000000-0000-4000-8000-000000000001"

_RESULT_EVENT = json.dumps({
    "type": "result",
    "is_error": False,
    "result": "",
    "structured_output": {"result": "ok"},
    "total_cost_usd": 0.0123,
    "num_turns": 2,
    "usage": {"input_tokens": 3, "output_tokens": 4},
    "modelUsage": {"claude-haiku-5-5": {"inputTokens": 3, "outputTokens": 4, "costUSD": 0.0123}},
}).encode()

#: (label, claude_dispatch kwargs, extra env). Together they touch every argv
#: branch: default tool-free, model, budget (param and env), isolated=False,
#: and the tool-granted path (mint, --mcp-config, --allowedTools).
CASES: list[tuple[str, dict, dict[str, str]]] = [
    ("default", {}, {}),
    ("model", {"model": "haiku", "operator": "operator_rank"}, {}),
    ("budget_param", {"max_budget_usd": 0.25}, {}),
    ("budget_env", {"model": "sonnet"}, {"NX_DISPATCH_MAX_BUDGET_USD": "0.5"}),
    ("not_isolated", {"isolated": False}, {}),
    (
        "tool_grant",
        {
            "allowed_tools": ["Read", "Grep", "mcp__nx"],
            "mcp_servers": {"nx": {"command": "nx-mcp", "args": ["--x"]}},
            "operator": "nx_plan_audit",
        },
        {},
    ),
]


def _one_shot_reader(data: bytes):
    state = {"served": False}

    async def _read(n: int = -1) -> bytes:
        if state["served"]:
            return b""
        state["served"] = True
        return data

    return _read


def _proc(stdout: bytes) -> MagicMock:
    proc = MagicMock()
    proc.returncode = 0
    proc.stdout = MagicMock()
    proc.stdout.read = _one_shot_reader(stdout)
    proc.stderr = MagicMock()
    proc.stderr.read = _one_shot_reader(b"")
    proc.wait = AsyncMock(return_value=0)
    proc.kill = MagicMock()
    return proc


def run_case(kwargs: dict, extra_env: dict[str, str]) -> dict:
    """Run one claude_dispatch against a fake subprocess; return what the
    spawn saw plus what the caller got back. Pure of ambient state: the env
    is replaced, cwd/session/mint/uuid are pinned."""
    seen: dict = {}

    async def intercept(*args, **kw):
        seen["argv"] = list(args)
        seen["env"] = dict(kw["env"])
        seen["cwd"] = kw["cwd"]
        stdin = kw.get("stdin")
        seen["stdin_text"] = Path(stdin.name).read_text() if hasattr(stdin, "name") else None
        return _proc(_RESULT_EVENT)

    base_env = {
        "PATH": "/usr/bin",
        "HOME": "/home/x",
        "NX_SESSION_ID": "parent-session-1",
        "NX_T1_HOST": "h",
        "NX_T1_PORT": "1",
        "NX_T1_SESSION": "stale-token",
        "NX_T1_SESSION_ID": "stale-id",
        "NX_T1_ISOLATED": "1",
    }
    sink: list[DispatchUsage] = []
    with (
        patch.dict(os.environ, {**base_env, **extra_env}, clear=True),
        patch_in("nexus.operators.dispatch", "asyncio.create_subprocess_exec", side_effect=intercept),
        patch("nexus.util.stable_cwd.current_project_root", return_value="/proj"),
        patch("nexus.util.stable_cwd.cwd_was_healed", return_value=False),
        patch("nexus.operators.dispatch.uuid4", return_value=_FIXED_UUID),
        patch("nexus.operators.dispatch._close_dispatch_session"),
        patch(
            "nexus.db.t1.mint_t1_session_token",
            side_effect=lambda sid, *, context: {"session_token": f"tok-{sid}"},
        ),
    ):
        returned = asyncio.run(claude_dispatch("the prompt", _SCHEMA, usage_sink=sink, **kwargs))
    return {
        "argv": seen["argv"],
        "env": seen["env"],
        "cwd": seen["cwd"],
        "stdin_text": seen["stdin_text"],
        "returned": returned,
        "usage_n": len(sink),
        "usage_cost": sink[0].cost_usd if sink else None,
        "usage_model": sink[0].model if sink else None,
    }


def capture_all() -> dict[str, dict]:
    return {label: run_case(kwargs, env) for label, kwargs, env in CASES}


#: Recorded on the pre-move tree. See module docstring.
GOLDEN: dict[str, dict] = {'budget_env': {'argv': ['claude',
                         '-p',
                         '--output-format',
                         'stream-json',
                         '--verbose',
                         '--include-partial-messages',
                         '--json-schema',
                         '{"type": "object", "properties": {"result": {"type": "string"}}}',
                         '--no-session-persistence',
                         '--strict-mcp-config',
                         '--setting-sources',
                         '',
                         '--model',
                         'sonnet',
                         '--max-budget-usd',
                         '0.500000',
                         '--tools',
                         ''],
                'cwd': '/proj',
                'env': {'HOME': '/home/x',
                        'NX_DISPATCH_MAX_BUDGET_USD': '0.5',
                        'NX_SESSION_ID': 'parent-session-1',
                        'PATH': '/usr/bin'},
                'returned': {'result': 'ok'},
                'stdin_text': 'the prompt',
                'usage_cost': 0.0123,
                'usage_model': 'claude-haiku-5-5',
                'usage_n': 1},
 'budget_param': {'argv': ['claude',
                           '-p',
                           '--output-format',
                           'stream-json',
                           '--verbose',
                           '--include-partial-messages',
                           '--json-schema',
                           '{"type": "object", "properties": {"result": {"type": "string"}}}',
                           '--no-session-persistence',
                           '--strict-mcp-config',
                           '--setting-sources',
                           '',
                           '--max-budget-usd',
                           '0.250000',
                           '--tools',
                           ''],
                  'cwd': '/proj',
                  'env': {'HOME': '/home/x',
                          'NX_SESSION_ID': 'parent-session-1',
                          'PATH': '/usr/bin'},
                  'returned': {'result': 'ok'},
                  'stdin_text': 'the prompt',
                  'usage_cost': 0.0123,
                  'usage_model': 'claude-haiku-5-5',
                  'usage_n': 1},
 'default': {'argv': ['claude',
                      '-p',
                      '--output-format',
                      'stream-json',
                      '--verbose',
                      '--include-partial-messages',
                      '--json-schema',
                      '{"type": "object", "properties": {"result": {"type": "string"}}}',
                      '--no-session-persistence',
                      '--strict-mcp-config',
                      '--setting-sources',
                      '',
                      '--tools',
                      ''],
             'cwd': '/proj',
             'env': {'HOME': '/home/x', 'NX_SESSION_ID': 'parent-session-1', 'PATH': '/usr/bin'},
             'returned': {'result': 'ok'},
             'stdin_text': 'the prompt',
             'usage_cost': 0.0123,
             'usage_model': 'claude-haiku-5-5',
             'usage_n': 1},
 'model': {'argv': ['claude',
                    '-p',
                    '--output-format',
                    'stream-json',
                    '--verbose',
                    '--include-partial-messages',
                    '--json-schema',
                    '{"type": "object", "properties": {"result": {"type": "string"}}}',
                    '--no-session-persistence',
                    '--strict-mcp-config',
                    '--setting-sources',
                    '',
                    '--model',
                    'haiku',
                    '--tools',
                    ''],
           'cwd': '/proj',
           'env': {'HOME': '/home/x', 'NX_SESSION_ID': 'parent-session-1', 'PATH': '/usr/bin'},
           'returned': {'result': 'ok'},
           'stdin_text': 'the prompt',
           'usage_cost': 0.0123,
           'usage_model': 'claude-haiku-5-5',
           'usage_n': 1},
 'not_isolated': {'argv': ['claude',
                           '-p',
                           '--output-format',
                           'stream-json',
                           '--verbose',
                           '--include-partial-messages',
                           '--json-schema',
                           '{"type": "object", "properties": {"result": {"type": "string"}}}',
                           '--no-session-persistence',
                           '--strict-mcp-config',
                           '--tools',
                           ''],
                  'cwd': '/proj',
                  'env': {'HOME': '/home/x',
                          'NX_SESSION_ID': 'parent-session-1',
                          'PATH': '/usr/bin'},
                  'returned': {'result': 'ok'},
                  'stdin_text': 'the prompt',
                  'usage_cost': 0.0123,
                  'usage_model': 'claude-haiku-5-5',
                  'usage_n': 1},
 'tool_grant': {'argv': ['claude',
                         '-p',
                         '--output-format',
                         'stream-json',
                         '--verbose',
                         '--include-partial-messages',
                         '--json-schema',
                         '{"type": "object", "properties": {"result": {"type": "string"}}}',
                         '--no-session-persistence',
                         '--strict-mcp-config',
                         '--setting-sources',
                         '',
                         '--mcp-config',
                         '{"mcpServers": {"nx": {"command": "nx-mcp", "args": ["--x"]}}}',
                         '--allowedTools',
                         'Read,Grep,mcp__nx'],
                'cwd': '/proj',
                'env': {'HOME': '/home/x',
                        'NX_SESSION_ID': 'parent-session-1',
                        'NX_T1_SESSION': 'tok-00000000-0000-4000-8000-000000000001',
                        'NX_T1_SESSION_ID': '00000000-0000-4000-8000-000000000001',
                        'PATH': '/usr/bin'},
                'returned': {'result': 'ok'},
                'stdin_text': 'the prompt',
                'usage_cost': 0.0123,
                'usage_model': 'claude-haiku-5-5',
                'usage_n': 1}}


@pytest.mark.parametrize("label,kwargs,extra_env", CASES, ids=[c[0] for c in CASES])
def test_cli_path_is_byte_identical_to_the_pre_move_recording(label, kwargs, extra_env) -> None:
    assert label in GOLDEN, f"no recording for {label}; the matrix and GOLDEN drifted apart"
    assert run_case(kwargs, extra_env) == GOLDEN[label]


def test_recording_is_non_vacuous() -> None:
    """The golden matrix covers every argv branch it claims to."""
    assert set(GOLDEN) == {c[0] for c in CASES}
    argvs = {k: v["argv"] for k, v in GOLDEN.items()}
    assert "--model" not in argvs["default"] and ["--tools", ""] == argvs["default"][-2:]
    assert "--model" in argvs["model"]
    assert "--max-budget-usd" in argvs["budget_param"]
    assert "--max-budget-usd" in argvs["budget_env"]
    assert "--setting-sources" not in argvs["not_isolated"]
    assert "--mcp-config" in argvs["tool_grant"] and "--allowedTools" in argvs["tool_grant"]
    assert GOLDEN["tool_grant"]["env"]["NX_T1_SESSION_ID"] == _FIXED_UUID
    assert "NX_T1_SESSION_ID" not in GOLDEN["default"]["env"]
    assert all(v["returned"] == {"result": "ok"} and v["usage_n"] == 1 for v in GOLDEN.values())
