# SPDX-License-Identifier: AGPL-3.0-or-later
"""``ManagedCredentials`` (nexus-csayy.3): where the cloud export gets its bearer, and what it does when the engine
rejects it. ``tests/test_cloud_export.py`` drives the export with a scripted stand-in for the credentials; this file
tests the real thing, so a ``rejected()`` that always answers False, or a bearer that is no longer re-read, fails here.

The static leg reads ``NX_SERVICE_TOKEN`` (env first, then config) on every call; the mint leg asks the data token
manager, and a rejected token is invalidated there so the next ``bearer()`` mints a fresh one.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from nexus.backup import cloud_export as ce
from nexus.backup.cloud_export import ExportRefused, ManagedCredentials, run_export
from nexus.backup.tenant_file import verify_file
from tests._fake_export_engine import FakeEngine, Reply, standard_tables

BASE = "http://127.0.0.1:1"


class FakeManager:
    """The data token manager's surface the credentials use."""

    def __init__(self, *, configured: bool, tokens: list[str] | None = None, remint: bool = True) -> None:
        self.configured = configured
        self.tokens = list(tokens or [])
        self.remint = remint
        self.bearer_calls: list[tuple[str, str]] = []
        self.invalidated: list[tuple[str, str, str]] = []

    def is_configured(self) -> bool:
        return self.configured

    def bearer_for(self, base_url: str, tenant: str) -> str | None:
        self.bearer_calls.append((base_url, tenant))
        return self.tokens[0] if self.tokens else None

    def invalidate_if_current(self, base_url: str, tenant: str, sent: str) -> bool:
        self.invalidated.append((base_url, tenant, sent))
        if self.remint and self.tokens and self.tokens[0] == sent:
            self.tokens.pop(0)
            return True
        return False


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch) -> FakeManager:
    m = FakeManager(configured=False)
    monkeypatch.setattr("nexus.db.data_token.get_data_token_manager", lambda: m)
    return m


def _static(monkeypatch: pytest.MonkeyPatch, token: str) -> None:
    monkeypatch.setenv("NX_SERVICE_URL", BASE)
    monkeypatch.setenv("NX_SERVICE_TOKEN", token)


def test_the_static_token_is_re_read_on_every_call(manager: FakeManager, monkeypatch: pytest.MonkeyPatch) -> None:
    _static(monkeypatch, "first")
    creds = ManagedCredentials(BASE)
    assert creds.bearer() == "first"
    _static(monkeypatch, "second")
    assert creds.bearer() == "second", "a rotated token is picked up without a new client"


def test_a_rejected_static_token_is_replaced_only_when_the_configured_one_changed(
    manager: FakeManager, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _static(monkeypatch, "old")
    creds = ManagedCredentials(BASE)
    assert creds.rejected("old") is False, "the same token again: nothing to try"
    _static(monkeypatch, "new")
    assert creds.rejected("old") is True, "a different token is configured now"
    assert creds.bearer() == "new"
    assert creds.rejected("new") is False


def test_with_no_token_configured_the_bearer_is_a_refusal_saying_how_to_set_one(
    t2_tenant: str, manager: FakeManager, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NX_SERVICE_URL", BASE)
    monkeypatch.delenv("NX_SERVICE_TOKEN", raising=False)
    with pytest.raises(ExportRefused, match="service_token"):
        ManagedCredentials(BASE).bearer()
    assert ManagedCredentials(BASE).rejected("anything") is False


def test_a_mint_armed_install_gets_a_minted_data_token_for_its_base_url_and_tenant(
    manager: FakeManager, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager.configured, manager.tokens = True, ["minted-1", "minted-2"]
    _static(monkeypatch, "static-is-ignored")
    creds = ManagedCredentials(BASE)
    assert creds.bearer() == "minted-1"
    assert manager.bearer_calls == [(BASE, "default")]


def test_a_rejected_minted_token_is_invalidated_so_the_next_bearer_is_a_fresh_mint(
    manager: FakeManager, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager.configured, manager.tokens = True, ["minted-1", "minted-2"]
    creds = ManagedCredentials(BASE)
    assert creds.rejected("minted-1") is True
    assert manager.invalidated == [(BASE, "default", "minted-1")]
    assert creds.bearer() == "minted-2"


def test_a_minted_token_the_manager_will_not_re_mint_is_not_retried(manager: FakeManager) -> None:
    manager.configured, manager.tokens, manager.remint = True, ["minted-1"], False
    assert ManagedCredentials(BASE).rejected("minted-1") is False


def test_a_failed_mint_is_a_refusal_not_a_silent_static_fallback(
    manager: FakeManager, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus.db.data_token import DataTokenMintError  # noqa: PLC0415

    manager.configured = True

    def boom(base_url: str, tenant: str) -> str:
        raise DataTokenMintError("the mint endpoint answered 403", status=403)

    manager.bearer_for = boom  # type: ignore[method-assign]
    _static(monkeypatch, "a-static-token-that-must-not-be-used")
    with pytest.raises(ExportRefused, match="data token"):
        ManagedCredentials(BASE).bearer()


def test_connect_binds_the_credentials_to_the_resolved_base_url(manager: FakeManager, monkeypatch: pytest.MonkeyPatch) -> None:
    _static(monkeypatch, "tok")
    client = ce.connect()
    try:
        assert client._base == BASE and isinstance(client._creds, ManagedCredentials)
    finally:
        client.close_http()


def test_an_export_survives_the_static_token_being_rotated_mid_run(
    manager: FakeManager, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The whole path: the engine stops accepting the old token at the third page; the operator has already put the
    new one in the environment; the client's own credentials re-read it on the 401 and resume from the same key."""
    with FakeEngine(standard_tables(), accepted={"old"}) as engine:
        monkeypatch.setenv("NX_SERVICE_URL", engine.base_url)
        monkeypatch.setenv("NX_SERVICE_TOKEN", "old")

        def rotate(req) -> Reply | None:
            if req.kind == "rows" and req.nth == 2:
                engine.accepted.clear()
                engine.accepted.add("new")
                os.environ["NX_SERVICE_TOKEN"] = "new"
            return None

        engine.script.append(rotate)
        client = ce.connect()
        try:
            run_export(client, tmp_path / "x.nxtenant")
        finally:
            client.close_http()
        rows = engine.requests("rows")
        assert len(engine.opened) == 1, "the session is the tenant's, not the token's"
    assert [r.bearer for r in rows] == ["old", "old", "old", "new", "new", "new"]
    assert rows[3].query == rows[2].query == {"table": "memory", "after": "memory:1"}
    verify_file(tmp_path / "x.nxtenant")
