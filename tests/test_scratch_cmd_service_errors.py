# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-h8rf6 (T1-401 finding): call-time service errors on nx scratch must
surface as clean, actionable ClickExceptions — not tracebacks.

The 401 case is load-bearing: service-backed T1 requires a MINTED session
token (session_tokens row). nexus-rn3wo.1: a bare CLI with no inherited live
MCP session now mints (and reuses, via a persisted CLI-dedicated session id)
its own token and self-heals once on a rotated-token 401 — so a 401 that
still reaches ``_clean_service_errors`` means that self-heal retry also
failed (persistent auth breakage) or a LIVE inherited MCP session's token
went stale (that path still never self-mints, since re-minting it would
rotate the token out from under the owning MCP server). Either way the CLI
must surface a crisp, actionable message rather than a raw traceback."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.commands.scratch import scratch
from nexus.db.http_scratch_store import SESSION_UNAUTHORIZED_MARKER, HttpScratchStore


def _t1_raising(exc: Exception) -> MagicMock:
    t1 = MagicMock()
    t1.put.side_effect = exc
    t1.search.side_effect = exc
    return t1


def test_call_time_401_is_clean_actionable_error() -> None:
    err = RuntimeError(
        f'{SESSION_UNAUTHORIZED_MARKER} on /v1/t1/put: {{"error":"unauthorized"}}'
    )
    with patch("nexus.commands.scratch._t1", return_value=_t1_raising(err)):
        result = CliRunner().invoke(scratch, ["put", "hello"])
    assert result.exit_code != 0
    # A clean failure is a ClickException-driven exit — never a propagated
    # RuntimeError (which click would render as a full traceback for users).
    assert not isinstance(result.exception, RuntimeError)
    assert "minted" in result.output
    assert "NX_T1_ISOLATED=1" in result.output


def test_http_401_raise_site_carries_the_marker() -> None:
    """Coupling tripwire (wave review #7): the store's actual 401 raise must
    contain SESSION_UNAUTHORIZED_MARKER — the detection in
    _clean_service_errors keys on it, so a wording drift at the raise site
    would silently lose the actionable guidance."""
    store = HttpScratchStore.__new__(HttpScratchStore)  # skip env-dependent __init__
    resp = MagicMock()
    resp.is_success = False
    resp.status_code = 401
    resp.text = '{"error":"unauthorized"}'
    store._client = MagicMock()
    store._client.post.return_value = resp

    with pytest.raises(RuntimeError) as exc_info:
        store._post("/v1/t1/put", {})
    assert SESSION_UNAUTHORIZED_MARKER in str(exc_info.value)

    with pytest.raises(RuntimeError) as exc_info:
        store._post_raw("/v1/t1/get", {})
    assert SESSION_UNAUTHORIZED_MARKER in str(exc_info.value)


def test_call_time_generic_service_error_is_clean() -> None:
    err = RuntimeError("HttpScratchStore: network error on /v1/t1/put: boom")
    with patch("nexus.commands.scratch._t1", return_value=_t1_raising(err)):
        result = CliRunner().invoke(scratch, ["put", "hello"])
    assert result.exit_code != 0
    assert not isinstance(result.exception, RuntimeError)
