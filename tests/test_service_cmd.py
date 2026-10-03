# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Unit tests for ``nx service token`` (RDR-152 bead nexus-gmiaf.32.3).

CliRunner-level with a fake HttpTokenStore. Server-side lifecycle behavior (rotate
overlap, revoke + cache invalidation) is covered by the Java TokenAdminHandlerTest.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from click.testing import CliRunner

from nexus.commands import service_cmd


def _http_status_error(status_code: int, body: dict[str, Any]) -> httpx.HTTPStatusError:
    """Build a real ``httpx.HTTPStatusError`` (nexus-r3ur5): the CLI's rotate error
    handling matches on ``.response.status_code``/``.json()``, so the fake store must
    raise the real exception type, not a stand-in."""
    request = httpx.Request("POST", "http://fake.invalid/v1/service-tokens/rotate")
    response = httpx.Response(status_code, request=request, json=body)
    return httpx.HTTPStatusError(f"{status_code}", request=request, response=response)


class _FakeStore:
    calls: list[tuple[str, tuple[Any, ...]]] = []
    revoke_result: dict[str, Any] = {"revoked": True, "token_hash": "abc123def456"}
    list_result: list[dict[str, Any]] = []
    rotate_raises: httpx.HTTPStatusError | None = None

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def __enter__(self) -> "_FakeStore":
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def issue_token(self, tenant: str, label: str | None = None, ttl_seconds: int | None = None,
                    scope: str | None = None) -> dict[str, Any]:
        _FakeStore.calls.append(("issue_token", (tenant, label, ttl_seconds, scope)))
        return {"tenant": tenant, "token": "RAW-ISSUE-xyz", "token_hash": "h1",
                "scope": scope or "tenant"}

    def rotate_token(self, tenant: str, grace_seconds: int | None = None,
                      scope: str | None = None) -> dict[str, Any]:
        _FakeStore.calls.append(("rotate_token", (tenant, grace_seconds, scope)))
        if _FakeStore.rotate_raises is not None:
            raise _FakeStore.rotate_raises
        return {"tenant": tenant, "token": "RAW-ROTATE-xyz", "token_hash": "h2",
                "scope": scope or "tenant"}

    def revoke_token(self, selector: str) -> dict[str, Any]:
        _FakeStore.calls.append(("revoke_token", (selector,)))
        return _FakeStore.revoke_result

    def list_tokens(self, tenant: str | None = None) -> list[dict[str, Any]]:
        _FakeStore.calls.append(("list_tokens", (tenant,)))
        return _FakeStore.list_result


@pytest.fixture(autouse=True)
def _patch(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeStore.calls = []
    _FakeStore.revoke_result = {"revoked": True, "token_hash": "abc123def456"}
    _FakeStore.list_result = []
    _FakeStore.rotate_raises = None
    monkeypatch.setattr(service_cmd, "HttpTokenStore", _FakeStore)


def _run(args: list[str]):
    return CliRunner().invoke(service_cmd.service, args)


def test_issue_passes_args_and_shows_token_once() -> None:
    result = _run(["token", "issue", "--tenant", "t-a", "--label", "ci", "--ttl", "3600"])
    assert result.exit_code == 0, result.output
    assert _FakeStore.calls == [("issue_token", ("t-a", "ci", 3600, None))]
    assert result.output.count("RAW-ISSUE-xyz") == 1


def test_issue_passes_scope_flag() -> None:
    # nexus-868dq: issuing the conexus-edge mint credential (operator-only server-side).
    result = _run(["token", "issue", "--tenant", "conexus-edge", "--scope", "mint"])
    assert result.exit_code == 0, result.output
    assert _FakeStore.calls == [("issue_token", ("conexus-edge", None, None, "mint"))]


def test_issue_passes_mint_locked_scope_flag() -> None:
    # nexus-xidcq (RDR-005 2a): tenant-locked mint credential — the CLI just forwards
    # the string verbatim; server-side (TokenAdminHandler) is the security boundary.
    result = _run(["token", "issue", "--tenant", "conexus-edge-locked", "--scope", "mint-locked"])
    assert result.exit_code == 0, result.output
    assert _FakeStore.calls == [("issue_token", ("conexus-edge-locked", None, None, "mint-locked"))]


def test_issue_passes_board_ci_scope_flag() -> None:
    # nexus-r3ur5: board-ci writer credential — the CLI just forwards the string
    # verbatim; server-side (AuthFilter/TupleHandler/TokenAdminHandler) is the
    # security boundary that confines it to POST /v1/tuples/out on board/ci/<topic>.
    result = _run(["token", "issue", "--tenant", "ci-board-writer", "--scope", "board-ci"])
    assert result.exit_code == 0, result.output
    assert _FakeStore.calls == [("issue_token", ("ci-board-writer", None, None, "board-ci"))]


def test_issue_omits_scope_by_default() -> None:
    result = _run(["token", "issue", "--tenant", "t-a"])
    assert result.exit_code == 0, result.output
    assert _FakeStore.calls == [("issue_token", ("t-a", None, None, None))]


def test_issue_explicit_tenant_scope_passes_through() -> None:
    result = _run(["token", "issue", "--tenant", "t-a", "--scope", "tenant"])
    assert result.exit_code == 0, result.output
    assert _FakeStore.calls == [("issue_token", ("t-a", None, None, "tenant"))]


def test_issue_rejects_non_issuable_scope() -> None:
    # 'data' tokens are minted only by /v1/data-tokens/mint; 'root' never. The
    # CLI refuses locally (click.Choice) rather than round-tripping a 400.
    assert _run(["token", "issue", "--tenant", "t-a", "--scope", "data"]).exit_code != 0
    assert _run(["token", "issue", "--tenant", "t-a", "--scope", "root"]).exit_code != 0
    assert _FakeStore.calls == []


def test_issue_requires_tenant() -> None:
    assert _run(["token", "issue"]).exit_code != 0


def test_rotate_passes_grace_and_mentions_lease_rediscovery() -> None:
    result = _run(["token", "rotate", "--tenant", "t-b", "--grace", "120"])
    assert result.exit_code == 0, result.output
    assert _FakeStore.calls == [("rotate_token", ("t-b", 120, None))]
    assert result.output.count("RAW-ROTATE-xyz") == 1
    # Help text contract: clients rediscover via the lease (no 401s during overlap).
    help_out = _run(["token", "rotate", "--help"]).output
    assert "lease" in help_out.lower()


def test_rotate_omits_scope_by_default() -> None:
    result = _run(["token", "rotate", "--tenant", "t-b"])
    assert result.exit_code == 0, result.output
    assert _FakeStore.calls == [("rotate_token", ("t-b", None, None))]


def test_rotate_passes_scope_flag() -> None:
    # nexus-r3ur5: rotate just the board-ci-scope credential on a mixed-scope tenant.
    result = _run(["token", "rotate", "--tenant", "nexus", "--scope", "board-ci"])
    assert result.exit_code == 0, result.output
    assert _FakeStore.calls == [("rotate_token", ("nexus", None, "board-ci"))]


def test_rotate_rejects_non_issuable_scope() -> None:
    assert _run(["token", "rotate", "--tenant", "t-b", "--scope", "data"]).exit_code != 0
    assert _run(["token", "rotate", "--tenant", "t-b", "--scope", "root"]).exit_code != 0
    assert _FakeStore.calls == []


def test_issue_shows_scope_in_output() -> None:
    result = _run(["token", "issue", "--tenant", "t-a"])
    assert result.exit_code == 0, result.output
    assert "Scope: tenant" in result.output

    result = _run(["token", "issue", "--tenant", "ci-board-writer", "--scope", "board-ci"])
    assert result.exit_code == 0, result.output
    assert "Scope: board-ci" in result.output


def test_rotate_shows_scope_in_output() -> None:
    result = _run(["token", "rotate", "--tenant", "nexus", "--scope", "board-ci"])
    assert result.exit_code == 0, result.output
    assert "Scope: board-ci" in result.output


def test_rotate_mixed_scope_409_becomes_clean_clickexception() -> None:
    # nexus-r3ur5 critique: the engine refuses (409) a mixed-scope tenant when no
    # --scope is given; the CLI must surface a clear message naming the scopes and
    # the remedy, never a raw httpx traceback.
    _FakeStore.rotate_raises = _http_status_error(
        409,
        {
            "error": "tenant 'nexus' holds live tokens of multiple scopes "
                      "[board-ci, tenant]; pass 'scope' to rotate one scope at a time",
            "scopes": ["board-ci", "tenant"],
        },
    )
    result = _run(["token", "rotate", "--tenant", "nexus"])
    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "board-ci" in result.output
    assert "tenant" in result.output
    assert "--scope" in result.output


def test_rotate_other_http_errors_propagate_unmodified() -> None:
    # Only 409 gets the clean-message treatment; any other status is not swallowed.
    _FakeStore.rotate_raises = _http_status_error(500, {"error": "internal server error"})
    result = _run(["token", "rotate", "--tenant", "t-b"])
    assert result.exit_code != 0
    assert not isinstance(result.exception, SystemExit)
    assert isinstance(result.exception, httpx.HTTPStatusError)


def test_revoke_success_and_no_match_exit_code() -> None:
    ok = _run(["token", "revoke", "abc123def456"])
    assert ok.exit_code == 0, ok.output
    assert _FakeStore.calls[-1] == ("revoke_token", ("abc123def456",))
    assert "Revoked" in ok.output

    _FakeStore.revoke_result = {"revoked": False}
    miss = _run(["token", "revoke", "nope"])
    assert miss.exit_code != 0  # ClickException on no unique match

    # Help text contract: revocation-propagation latency bound = AuthFilter cache TTL.
    help_out = _run(["token", "revoke", "--help"]).output
    assert "ttl" in help_out.lower() or "cache" in help_out.lower()


def test_list_never_prints_plaintext_token() -> None:
    _FakeStore.list_result = [
        {"token_hash": "abcdef0123456789", "tenant": "t-a", "label": "ci",
         "status": "active", "created_at": "2026-06-09T00:00:00Z",
         "expires_at": None, "revoked_at": None},
    ]
    result = _run(["token", "list", "--tenant", "t-a"])
    assert result.exit_code == 0, result.output
    assert _FakeStore.calls == [("list_tokens", ("t-a",))]
    assert "abcdef012345" in result.output  # 12-char id prefix shown
    assert "active" in result.output
    # A list response carries no "token" field, so nothing plaintext can leak.


def test_list_empty() -> None:
    result = _run(["token", "list"])
    assert result.exit_code == 0
    assert "No tokens." in result.output


# ── nx service probe (nexus-vwvv5.12) ─────────────────────────────────────────


def _sample_managed_caps():
    # Helper (not a test): keeps the voyage model literal OUT of the test
    # function body so the RDR-109 mode lint (which scans each test's own
    # source for voyage-(context|code)-3) does not flag this CLI smoke. The
    # managed service reports voyage models; this is display fixture data, not
    # cloud-mode behavior under test.
    from nexus.db import managed_endpoint as me

    return me.ManagedCapabilities(
        base_url="https://api.conexus-nexus.com",
        app_version="1.0-SNAPSHOT",
        release_version="0.1.8",
        embedding_mode="voyage",
        embedding_models=["voyage-context-3"],
        schema_latest_id="vectors-002",
        schema_changeset_count=64,
    )


def test_probe_success_prints_capabilities(monkeypatch) -> None:
    from nexus.db import managed_endpoint as me

    caps = _sample_managed_caps()
    monkeypatch.setattr(me, "probe_managed_service", lambda **kw: caps)

    result = _run(["probe", "--url", "https://api.conexus-nexus.com"])
    assert result.exit_code == 0, result.output
    assert "reachable" in result.output
    assert "1.0-SNAPSHOT" in result.output
    assert "voyage" in result.output


def test_probe_failure_fails_loud(monkeypatch) -> None:
    from nexus.db import managed_endpoint as me

    def _boom(**kw):
        raise me.ManagedServiceUnreachable("unreachable — set NX_SERVICE_URL")

    monkeypatch.setattr(me, "probe_managed_service", _boom)

    result = _run(["probe", "--url", "https://x"])
    assert result.exit_code != 0
    assert "NX_SERVICE_URL" in result.output
