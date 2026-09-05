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
    assert "nx daemon service start" in result.output


def test_http_401_raise_site_carries_the_marker() -> None:
    """Coupling tripwire (wave review #7): the store's actual 401 raise must
    contain SESSION_UNAUTHORIZED_MARKER — the detection in
    _clean_service_errors keys on it, so a wording drift at the raise site
    would silently lose the actionable guidance."""
    store = HttpScratchStore.__new__(HttpScratchStore)  # skip env-dependent __init__
    # nexus-a2qhz round 3: _post's mutates=True branch (the default) now
    # reads self._base_url to call guard_production_write before it ever
    # reaches the HTTP call this test is exercising — __new__ skips
    # __init__ entirely, so the attribute must be set by hand here, same
    # as _client below.
    store._base_url = "http://127.0.0.1:0"
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


def test_post_mutates_guard_reads_base_url() -> None:
    """nexus-a2qhz round 3: _post's mutates=True branch (the default —
    put/flag/unflag/delete/clear/close_session) must call
    guard_production_write with THIS store's own _base_url, not a stale
    or missing value. Pins the read at src/nexus/db/http_scratch_store.py's
    ``_post`` guard call site directly (the coupling
    test_http_401_raise_site_carries_the_marker exercises only
    incidentally, via __new__ needing the attribute set at all)."""
    store = HttpScratchStore.__new__(HttpScratchStore)  # skip env-dependent __init__
    store._base_url = "http://127.0.0.1:54321"
    store._session_token = ""
    resp = MagicMock()
    resp.is_success = True
    resp.status_code = 200
    resp.json.return_value = {}
    store._client = MagicMock()
    store._client.post.return_value = resp

    with patch(
        "nexus.db.service_endpoint.guard_production_write"
    ) as mock_guard, patch.object(
        HttpScratchStore, "_current_authorization_header", return_value="Bearer x",
    ):
        store._post("/v1/t1/put", {})

    mock_guard.assert_called_once_with("http://127.0.0.1:54321")


def test_call_time_generic_service_error_is_clean() -> None:
    err = RuntimeError("HttpScratchStore: network error on /v1/t1/put: boom")
    with patch("nexus.commands.scratch._t1", return_value=_t1_raising(err)):
        result = CliRunner().invoke(scratch, ["put", "hello"])
    assert result.exit_code != 0
    assert not isinstance(result.exception, RuntimeError)
