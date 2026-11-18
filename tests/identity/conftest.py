# SPDX-License-Identifier: AGPL-3.0-or-later
"""The harness of the engine-backed identity tests (RDR-253 Step 3).

``world`` is a real engine tenant (the session substrate's, minted by ``t2_tenant``), a ``FakeProcesses`` table whose
registry files live under ``tmp_path`` (never the real ``~/.claude``), an identity directory under ``tmp_path``
(never the real ``~/.config/nexus``), and two engine clients: the tenant credential the hook uses and the data
credential the MCP server uses. Clocks are fixed; the engine's own clock is not ours, so a lapse is staged by
backdating the identity's last renewal in PG, the one thing a real engine cannot be told.

A test module that asks for ``world`` carries ``pytestmark = pytest.mark.t2_tenant`` (every test in it mints one).

One module, one tenant (the tenant ceiling, tests/storage_touch_ceiling.toml): a module with several independent rows
runs them as ONE scenario test through ``rows`` (:class:`Rows`). Each row gets a ``scoped`` world on the shared
tenant: its own host name (the engine looks holders up by host and pid, so rows never meet each other's bindings),
its own process table, identity directory and clock, and a tag that qualifies every seat name, ``-n`` name and
session id the harness sends (the engine keeps those per tenant). A row reads as if it had the tenant to itself; the
one place the tag shows is where it asserts text that names a seat, which goes through ``world.q``. A failing row
does not stop the others: ``Rows.check`` fails once, naming every failed row with its traceback.
"""
from __future__ import annotations

import contextlib
import os
import traceback
from collections.abc import Callable, Iterator
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
    """One test's engine tenant, process table and clients; or, with *tag*, one row's slice of a shared tenant."""

    def __init__(self, tmp_path: Path, tenant: str, state: dict[str, Any], base_url: str, tenant_token: str, *,
                 tag: str = "", parent: World | None = None) -> None:
        self.tag = tag
        self.host = f"{tag}-{HOST}" if tag else HOST
        self.tmp = tmp_path / tag if tag else tmp_path
        self.tenant = tenant
        self.state = state
        self.base_url = base_url
        self.procs = FakeProcesses(self.tmp / "claude-sessions", host=self.host)
        self.identity_dir = self.tmp / "config" / "identity"
        self.config_dir = self.tmp / "config"
        self.client = IdentityClient(base_url, tenant_token)
        self._tenant_token = tenant_token
        self._parent = parent
        self._data: IdentityClient | None = None
        self.now = parent.now if parent is not None else 1_000.0

    def scoped(self, tag: str) -> World:
        """A row's world on this tenant: own host, process table, directories and clock; names qualified by *tag*."""
        return World(self.tmp, self.tenant, self.state, self.base_url, self._tenant_token, tag=tag, parent=self)

    def q(self, name: str) -> str:
        """*name* as the engine sees it in this world: a seat name, ``-n`` name or session id carries the row's tag."""
        return f"{self.tag}-{name}" if self.tag and name else name

    # ── clients ─────────────────────────────────────────────────────────────

    @property
    def data_client(self) -> IdentityClient:
        """The MCP server's credential: a short-lived ``data`` token minted for this tenant. It may start, resume,
        fork, refresh and end a binding and create an ephemeral identity; the engine refuses it clear, release,
        takeover and ``gone``."""
        if self._parent is not None:
            return self._parent.data_client
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
        return self.client.create_seat(self.q(name))["id"]

    def process(self, pid: int, *, os_start: str = T0, started_at: int | None = None, name: str = "",
                entrypoint: str = "cli", session_id: str = "s-0", registry: bool = True) -> None:
        """A running Claude process on this host with a registry file."""
        self.procs.start(pid, os_start, started_at=started_at, name=self.q(name), entrypoint=entrypoint,
                         session_id=self.q(session_id), registry=registry)

    def registry(self, pid: int, started_at: int, os_start: str, *, session_id: str = "s-0", name: str = "") -> None:
        """Rewrite *pid*'s registry file (an in-place ``/restart``, a ``/branch``)."""
        self.procs.write_registry(pid, started_at, os_start, session_id=self.q(session_id), name=self.q(name))

    def facts(self, pid: int, source: str, session_id: str, *, seat: str | None = None, os_start: str | None = None,
              with_registry: bool = True) -> LaunchFacts:
        """The launch facts for *pid*; *session_id* and *seat* are the engine's spelling (already ``q``-qualified)."""
        f = LaunchFacts(source=source, session_id=session_id, host=self.host, claude_pid=pid,
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
        session_id = self.q(session_id)
        seat = self.q(seat) if seat else seat
        env = {k: (self.q(v) if k == "NX_SEAT" else v) for k, v in env.items()} if env else env
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
        return self.client.session(self.q(session_id))

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


class Rows:
    """The independent rows of one scenario test on one tenant. ``with rows.row("name") as world:`` runs a row on its
    own scoped world; an exception in it is kept, not raised, so the rows after it still run. ``check`` fails once,
    naming every failed row with its traceback, and also when fewer rows ran than the test expects (a row dropped from
    the list must not read as a pass)."""

    def __init__(self, world: World) -> None:
        self._base = world
        self.ran: list[str] = []
        self.failed: dict[str, str] = {}

    @contextlib.contextmanager
    def row(self, name: str) -> Iterator[World]:
        assert name not in self.ran, f"row {name!r} ran twice"
        self.ran.append(name)
        try:
            yield self._base.scoped(f"r{len(self.ran):02d}")
        except Exception:  # noqa: BLE001 - kept and reported by check(), so one row's failure does not hide the next
            self.failed[name] = traceback.format_exc()

    def run(self, table: tuple[Callable[[World], None], ...], namespace: dict[str, Any]) -> None:
        """Run every row function of *table* and fail once with the rows that failed. *namespace* is the module's
        ``globals()``: a ``row_*`` function that is not in *table* never runs, so it fails here instead of passing."""
        defined = sorted(n for n, o in namespace.items() if n.startswith("row_") and callable(o))
        listed = sorted(fn.__name__ for fn in table)
        assert defined == listed, f"row functions not in the table: {sorted(set(defined) - set(listed))}"
        for fn in table:
            with self.row(fn.__name__) as world:
                fn(world)
        self.check(expect=len(table))

    def check(self, expect: int) -> None:
        if len(self.ran) != expect:
            pytest.fail(f"{len(self.ran)} rows ran, the test expects {expect}: ran {self.ran}", pytrace=False)
        if self.failed:
            body = "\n\n".join(f"ROW {name!r} FAILED:\n{tb}" for name, tb in self.failed.items())
            pytest.fail(f"{len(self.failed)} of {expect} rows failed: {sorted(self.failed)}\n\n{body}", pytrace=False)


@pytest.fixture
def world(tmp_path: Path, t2_tenant: str) -> World:
    from tests._engine_substrate import ensure_engine

    return World(tmp_path, t2_tenant, ensure_engine(), os.environ["NX_SERVICE_URL"], os.environ["NX_SERVICE_TOKEN"])


@pytest.fixture
def rows(world: World) -> Rows:
    return Rows(world)
