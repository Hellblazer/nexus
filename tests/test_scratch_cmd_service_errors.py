# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-h8rf6 (T1-401 finding): call-time service errors on nx scratch must
surface as clean, actionable ClickExceptions — not tracebacks.

The 401 case is load-bearing: service-backed T1 requires a MINTED session
token (session_tokens row); re-minting ROTATES the token (TokenStore
issueSessionToken ON CONFLICT DO UPDATE), so the bare CLI can never safely
self-mint for a session an MCP may own. The only correct CLI behavior is a
crisp explanation of the two sanctioned paths (run inside a session that
minted, or NX_T1_ISOLATED=1)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from nexus.commands.scratch import scratch


def _t1_raising(exc: Exception) -> MagicMock:
    t1 = MagicMock()
    t1.put.side_effect = exc
    t1.search.side_effect = exc
    return t1


def test_call_time_401_is_clean_actionable_error() -> None:
    err = RuntimeError(
        'HttpScratchStore: /v1/t1/put returned HTTP 401: {"error":"unauthorized"}'
    )
    with patch("nexus.commands.scratch._t1", return_value=_t1_raising(err)):
        result = CliRunner().invoke(scratch, ["put", "hello"])
    assert result.exit_code != 0
    # A clean failure is a ClickException-driven exit — never a propagated
    # RuntimeError (which click would render as a full traceback for users).
    assert not isinstance(result.exception, RuntimeError)
    assert "minted" in result.output
    assert "NX_T1_ISOLATED=1" in result.output


def test_call_time_generic_service_error_is_clean() -> None:
    err = RuntimeError("HttpScratchStore: network error on /v1/t1/put: boom")
    with patch("nexus.commands.scratch._t1", return_value=_t1_raising(err)):
        result = CliRunner().invoke(scratch, ["put", "hello"])
    assert result.exit_code != 0
    assert not isinstance(result.exception, RuntimeError)
