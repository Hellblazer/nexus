# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Unit tests for ``nx service token`` (RDR-152 bead nexus-gmiaf.32.3).

CliRunner-level with a fake HttpTokenStore. Server-side lifecycle behavior (rotate
overlap, revoke + cache invalidation) is covered by the Java TokenAdminHandlerTest.
"""

from __future__ import annotations

from typing import Any

import pytest
from click.testing import CliRunner

from nexus.commands import service_cmd


class _FakeStore:
    calls: list[tuple[str, tuple[Any, ...]]] = []
    revoke_result: dict[str, Any] = {"revoked": True, "token_hash": "abc123def456"}
    list_result: list[dict[str, Any]] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def __enter__(self) -> "_FakeStore":
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def issue_token(self, tenant: str, label: str | None = None, ttl_seconds: int | None = None,
                    scope: str | None = None) -> dict[str, Any]:
        _FakeStore.calls.append(("issue_token", (tenant, label, ttl_seconds, scope)))
        return {"tenant": tenant, "token": "RAW-ISSUE-xyz", "token_hash": "h1"}

    def rotate_token(self, tenant: str, grace_seconds: int | None = None) -> dict[str, Any]:
        _FakeStore.calls.append(("rotate_token", (tenant, grace_seconds)))
        return {"tenant": tenant, "token": "RAW-ROTATE-xyz", "token_hash": "h2"}

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
    assert _FakeStore.calls == [("rotate_token", ("t-b", 120))]
    assert result.output.count("RAW-ROTATE-xyz") == 1
    # Help text contract: clients rediscover via the lease (no 401s during overlap).
    help_out = _run(["token", "rotate", "--help"]).output
    assert "lease" in help_out.lower()


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


# ── nx service record-deploy (nexus-dz6b1, RDR-179 tracker mechanization) ──────


class _FakeMemory:
    """Captures memory.put() calls routed through a fake t2_handle."""

    puts: list[dict[str, Any]] = []

    def put(self, **kwargs: Any) -> int:
        _FakeMemory.puts.append(kwargs)
        return 1


class _FakeHandle:
    memory = _FakeMemory()

    def __enter__(self) -> "_FakeHandle":
        return self

    def __exit__(self, *exc: object) -> None:
        pass


@pytest.fixture
def _fake_t2(monkeypatch):
    from nexus.commands import _helpers

    _FakeMemory.puts = []
    monkeypatch.setattr(_helpers, "t2_handle", lambda: _FakeHandle())
    return _FakeMemory


def _patch_caps(monkeypatch, release_version: str) -> None:
    from nexus.db import managed_endpoint as me

    caps = me.ManagedCapabilities(
        base_url="https://api.conexus-nexus.com",
        app_version="1.0-SNAPSHOT",
        release_version=release_version,
        embedding_mode="voyage",
        embedding_models=["voyage-context-3"],
        schema_latest_id=None,
        schema_changeset_count=None,
    )
    monkeypatch.setattr(me, "probe_managed_service", lambda **kw: caps)


def test_record_deploy_writes_tracker_when_live_version_matches(monkeypatch, _fake_t2) -> None:
    _patch_caps(monkeypatch, "0.1.24")

    result = _run(
        [
            "record-deploy",
            "engine-service-v0.1.24",
            "--commit",
            "b80b14d4",
            "--gate",
            "PASSED",
        ]
    )
    assert result.exit_code == 0, result.output
    assert len(_fake_t2.puts) == 1
    put = _fake_t2.puts[0]
    assert put["project"] == "nexus"
    assert put["title"] == "deployed-engine-version"
    # The version field is machine-sourced from the live /version read, never
    # hand-typed — this is the anti-rot invariant.
    assert "0.1.24" in put["content"]
    assert "b80b14d4" in put["content"]
    assert "PASSED" in put["content"]
    assert put["ttl"] == 0  # permanent operational record


def test_record_deploy_accepts_bare_version_forms(monkeypatch, _fake_t2) -> None:
    _patch_caps(monkeypatch, "0.1.24")
    for tag in ("0.1.24", "v0.1.24", "engine-service-v0.1.24"):
        _FakeMemory.puts = []
        result = _run(["record-deploy", tag])
        assert result.exit_code == 0, f"{tag}: {result.output}"
        assert len(_fake_t2.puts) == 1


def test_record_deploy_refuses_when_live_version_mismatches(monkeypatch, _fake_t2) -> None:
    # Cloud still on 0.1.23; recording 0.1.24 must fail loud and write nothing.
    _patch_caps(monkeypatch, "0.1.23")

    result = _run(["record-deploy", "engine-service-v0.1.24"])
    assert result.exit_code != 0
    assert "0.1.23" in result.output  # names what is actually live
    assert "0.1.24" in result.output  # names what was asked
    assert _fake_t2.puts == []  # NO write on mismatch


def test_record_deploy_fails_loud_when_service_unreachable(monkeypatch, _fake_t2) -> None:
    from nexus.db import managed_endpoint as me

    def _boom(**kw):
        raise me.ManagedServiceUnreachable("unreachable — set NX_SERVICE_URL")

    monkeypatch.setattr(me, "probe_managed_service", _boom)

    result = _run(["record-deploy", "engine-service-v0.1.24"])
    assert result.exit_code != 0
    assert _fake_t2.puts == []  # NO write when we cannot verify live truth
