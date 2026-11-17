# SPDX-License-Identifier: AGPL-3.0-or-later
"""The harness of the engine-backed identity tests (RDR-253 Step 3).

``world`` is a real engine tenant (the session substrate's, minted by ``t2_tenant``), a ``FakeProcesses`` table whose
registry files live under ``tmp_path`` (never the real ``~/.claude``), an identity directory under ``tmp_path``
(never the real ``~/.config/nexus``), and two engine clients: the tenant credential the hook uses and the data
credential the MCP server uses. Clocks are fixed; the engine's own clock is not ours, so a lapse is staged by
backdating the identity's last renewal in PG, the one thing a real engine cannot be told.

A test module that asks for ``world`` carries ``pytestmark = pytest.mark.t2_tenant`` (every test in it mints one).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx
import pytest

from nexus.identity.binder import BindOutcome, LaunchFacts, SessionBinder
from nexus.identity.bind_client import IdentityClient
from nexus.identity.holder import normalise_instant
from tests.identity.fakes import HOST, FakeProcesses

T0 = "2026-11-17T08:00:00Z"
VERSION = "9.9.9-test"


class World:
    """One test's engine tenant, process table and clients."""

    def __init__(self, tmp_path: Path, tenant: str, state: dict[str, Any], base_url: str, tenant_token: str) -> None:
        self.tmp = tmp_path
        self.tenant = tenant
        self.state = state
        self.base_url = base_url
        self.procs = FakeProcesses(tmp_path / "claude-sessions")
        self.identity_dir = tmp_path / "config" / "identity"
        self.config_dir = tmp_path / "config"
        self.client = IdentityClient(base_url, tenant_token)
        self._tenant_token = tenant_token
        self._data: IdentityClient | None = None
        self.now = 1_000.0

    # ── clients ─────────────────────────────────────────────────────────────

    @property
    def data_client(self) -> IdentityClient:
        """The MCP server's credential: a short-lived ``data`` token minted for this tenant. It may start, resume,
        fork, refresh and end a binding and create an ephemeral identity; the engine refuses it clear, release,
        takeover and ``gone``."""
        if self._data is None:
            issued = httpx.post(f"{self.base_url}/v1/service-tokens/issue", json={"tenant": self.tenant, "scope": "mint"},
                                headers={"Authorization": f"Bearer {self.state['bearer']}"}, timeout=30)
            assert issued.status_code == 200, issued.text
            minted = httpx.post(f"{self.base_url}/v1/data-tokens/mint", json={"tenant": self.tenant},
                                headers={"Authorization": f"Bearer {issued.json()['token']}"}, timeout=30)
            assert minted.status_code == 200, minted.text
            self._data = IdentityClient(self.base_url, minted.json()["data_token"])
        return self._data

    # ── staging ─────────────────────────────────────────────────────────────

    def seat(self, name: str) -> str:
        return self.client.create_seat(name)["id"]

    def process(self, pid: int, *, os_start: str = T0, started_at: int | None = None, name: str = "",
                entrypoint: str = "cli", session_id: str = "s-0", registry: bool = True, host: str = HOST) -> None:
        """A running Claude process on this host with a registry file."""
        self.procs.start(pid, os_start, started_at=started_at, name=name, entrypoint=entrypoint, session_id=session_id,
                         registry=registry)

    def facts(self, pid: int, source: str, session_id: str, *, seat: str | None = None, os_start: str | None = None,
              with_registry: bool = True) -> LaunchFacts:
        f = LaunchFacts(source=source, session_id=session_id, host=HOST, claude_pid=pid,
                        proc_start=normalise_instant(os_start or self.procs.os_start(pid)), client_version=VERSION, seat=seat)
        if with_registry:
            from nexus.identity.holder import read_registry_entry

            entry = read_registry_entry(pid, self.procs.sessions_dir)
            assert entry is not None, f"process {pid} has no registry file; call world.process first"
            f.with_registry(entry)
        return f

    def hook(self, pid: int, source: str, session_id: str, *, seat: str | None = None, client: IdentityClient | None = None,
             privileged: bool = True, env: dict[str, str] | None = None) -> BindOutcome:
        """One SessionStart bind for process *pid*, as the hook (tenant credential) unless *client* says otherwise."""
        binder = SessionBinder(client=client or self.client, procs=self.procs, identity_dir=self.identity_dir,
                               allow_privileged=privileged, clock=lambda: self.now, sleep=lambda s: None)
        self.procs.set_session(pid, session_id)
        return binder.bind(self.facts(pid, source, session_id, seat=seat), env=env)

    # ── reads ───────────────────────────────────────────────────────────────

    def identity(self, identity: str) -> dict[str, Any]:
        return self.client.show(identity)

    def log(self, identity: str) -> list[dict[str, Any]]:
        return list(self.client._request("GET", f"/{identity}/bindings")["bindings"])

    def events(self, identity: str) -> list[str]:
        return [row["event"] for row in self.log(identity)]

    def indexed(self, session_id: str) -> dict[str, Any]:
        return self.client.session(session_id)

    # ── the engine's clock ──────────────────────────────────────────────────

    def lapse(self, identity: str, seconds_ago: int = 400) -> None:
        """Backdate the identity's last renewal by *seconds_ago*, so the engine's next read lapses it (300 s) and, for
        another host, ends its 10-minute reservation (900 s). The engine reads the column, not our clock."""
        from tests import _tenant_pool

        proc = _tenant_pool.psql(
            self.state,
            "UPDATE nexus.identity_records SET last_renewal = now() - (:'secs' || ' seconds')::interval, "
            "state = CASE WHEN state = 'lapsed' THEN 'live' ELSE state END, lapsed_at = NULL "
            "WHERE tenant_id = :'tenant' AND id = :'id' RETURNING id;",
            secs=str(seconds_ago), tenant=self.tenant, id=identity)
        assert proc.returncode == 0 and identity in proc.stdout, f"could not backdate {identity}: {proc.stderr} {proc.stdout}"


@pytest.fixture
def world(tmp_path: Path, t2_tenant: str) -> World:
    from tests._engine_substrate import ensure_engine

    return World(tmp_path, t2_tenant, ensure_engine(), os.environ["NX_SERVICE_URL"], os.environ["NX_SERVICE_TOKEN"])
