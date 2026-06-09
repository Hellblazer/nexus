# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Unit tests for ``nx tenant`` (RDR-152 bead nexus-gmiaf.32.3).

CliRunner-level: the HttpTokenStore is replaced with a fake so these tests assert CLI
wiring and output (the raw token is shown once), independent of a running service. The
real server-side behavior is covered by the Java TokenAdminHandlerTest.
"""

from __future__ import annotations

from typing import Any

import pytest
from click.testing import CliRunner

from nexus.commands import tenant_cmd


class _FakeStore:
    """Records calls; returns canned responses. Usable as a context manager."""

    calls: list[tuple[str, Any]] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def __enter__(self) -> "_FakeStore":
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def create_tenant(self, name: str) -> dict[str, Any]:
        _FakeStore.calls.append(("create_tenant", name))
        return {"tenant": name, "token": "RAW-TOKEN-abc123", "token_hash": "deadbeef"}


@pytest.fixture(autouse=True)
def _reset_calls() -> None:
    _FakeStore.calls = []


def test_tenant_create_invokes_client_and_shows_token_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tenant_cmd, "HttpTokenStore", _FakeStore)
    result = CliRunner().invoke(tenant_cmd.tenant, ["create", "tenant-x"])
    assert result.exit_code == 0, result.output
    assert _FakeStore.calls == [("create_tenant", "tenant-x")]
    # Raw token shown exactly once; hash never shown.
    assert result.output.count("RAW-TOKEN-abc123") == 1
    assert "deadbeef" not in result.output
    assert "tenant-x" in result.output


def test_tenant_create_requires_name() -> None:
    result = CliRunner().invoke(tenant_cmd.tenant, ["create"])
    assert result.exit_code != 0  # missing required argument
