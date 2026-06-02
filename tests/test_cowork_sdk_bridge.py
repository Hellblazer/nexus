# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-126 §5 (nexus-62a3p): Cowork SDK-bridge host-side surface.

The actual cross-process Cowork bridge (Claude Desktop -> Cowork VM via
the Anthropic SDK ``--mcp-config "type": "sdk"`` transport) can only be
exercised by hand: it needs a running Claude Desktop and a UI action.
What we CAN regression-test is the host-side surface the bridge rides on:
a write through ``nx-mcp``'s ``memory_put`` is visible to a subsequent
``memory_get`` against the same T2, and vice-versa. If that round-trip
ever breaks, the bridge cannot work regardless of the SDK channel.

This pins the bidirectional sentinel pattern documented in
``docs/desktop-deployment.md`` § Verification so a substrate regression
is caught even though the bridge itself is not unit-testable.

T2 is redirected to a tmp SQLite DB (no daemon, no network).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus.mcp import core

_SENTINEL_PROJECT = "_cowork_test"


@pytest.fixture
def isolated_t2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``t2_ctx`` to a tmp DB by patching ``default_db_path``
    (resolved at call time inside ``t2_ctx``)."""
    import nexus.mcp_infra as infra

    db = tmp_path / "cowork_t2.db"
    monkeypatch.setattr(infra, "default_db_path", lambda: db)
    monkeypatch.delenv("NX_AGENT", raising=False)
    monkeypatch.delenv("NX_SESSION_ID", raising=False)
    import nexus.db.t2.memory_store as _ms

    monkeypatch.setattr(_ms, "_read_session_id", lambda: None)
    return db


class TestHostSideRoundTrip:
    def test_put_then_get_round_trips_through_nx_mcp(self, isolated_t2: Path) -> None:
        """The host writes a sentinel (as the host CLI / Claude Code would);
        the Cowork-side read (same nx-mcp tools, same T2) sees it."""
        put = core.memory_put(
            content="cowork sentinel payload",
            project=_SENTINEL_PROJECT,
            title="host-to-vm",
            tags="cowork,sentinel",
            ttl=0,
        )
        assert put.startswith("Stored:")

        got = core.memory_get(project=_SENTINEL_PROJECT, title="host-to-vm")
        assert "cowork sentinel payload" in got

    def test_vm_write_visible_to_host_read(self, isolated_t2: Path) -> None:
        """Reverse direction: a write attributed to the VM agent is
        visible to a host-side read — the bridge is bidirectional."""
        put = core.memory_put(
            content="payload written from the VM side",
            project=_SENTINEL_PROJECT,
            title="vm-to-host",
            ttl=0,
            agent="cowork-vm",
        )
        assert put.startswith("Stored:")

        got = core.memory_get(project=_SENTINEL_PROJECT, title="vm-to-host")
        assert "payload written from the VM side" in got

    def test_overwrite_upserts_by_project_title(self, isolated_t2: Path) -> None:
        """A second write to the same (project, title) is seen by the
        next read — no stale-read regression in the shared substrate."""
        core.memory_put(
            content="v1", project=_SENTINEL_PROJECT, title="shared-key", ttl=0
        )
        core.memory_put(
            content="v2 updated", project=_SENTINEL_PROJECT, title="shared-key", ttl=0
        )
        got = core.memory_get(project=_SENTINEL_PROJECT, title="shared-key")
        assert "v2 updated" in got
        assert "v1" not in got.replace("v2 updated", "")

    def test_missing_sentinel_reports_not_found(self, isolated_t2: Path) -> None:
        """A read for an absent sentinel must not error — the diagnostic
        recipe relies on a clean 'not found' signal."""
        got = core.memory_get(project=_SENTINEL_PROJECT, title="never-written")
        assert "Error:" not in got
