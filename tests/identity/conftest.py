# SPDX-License-Identifier: AGPL-3.0-or-later
"""The harness of the engine-backed identity tests (RDR-253 Step 3).

``world`` is a real engine tenant (the session substrate's, minted by ``t2_tenant``), a ``FakeProcesses`` table whose
registry files live under ``tmp_path`` (never the real ``~/.claude``), an identity directory under ``tmp_path``
(never the real ``~/.config/nexus``), and two engine clients: the tenant credential the hook uses and the data
credential the MCP server uses. Clocks are fixed; the engine's own clock is not ours, so a lapse is staged by
backdating the identity's last renewal in PG, the one thing a real engine cannot be told.

The one test that asks for ``world`` is tests/identity/test_identity_engine_journeys.py, marked ``t2_tenant``; it
mints the only tenant of these modules (the tenant ceiling, tests/storage_touch_ceiling.toml). Rows run through
:class:`Rows`. Each row gets a ``scoped`` world on the shared
tenant: its own host name (the engine looks holders up by host and pid, so rows never meet each other's bindings),
its own process table, identity directory and clock, and a tag that qualifies every seat name, ``-n`` name and
session id the harness sends (the engine keeps those per tenant). A row reads as if it had the tenant to itself; the
one place the tag shows is where it asserts text that names a seat, which goes through ``world.q``. A failing row
does not stop the others: ``Rows.check`` fails once, naming every failed row with its traceback.

Step 3's modules go one step further: the whole suite of engine-backed identity journeys mints ONE tenant, not one per
module (tests/identity/test_identity_engine_journeys.py, Sam's ruling, T2 nexus/sam-ruling-rdr253-step3-tenants-2026-11-18).
Each module keeps its row functions and its ``ROWS`` table and holds no test; the one test hands every table to
:func:`run_journeys`, which runs each with its own ``Rows`` (a tag prefix per module, so no two rows of the run share a
host, seat name or session id) and fails once, naming ``module::row`` for every failure.
"""
from __future__ import annotations

import contextlib
import os
import sys
import time
import traceback
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from nexus.identity.binder import BindOutcome, LaunchFacts, SessionBinder
from nexus.identity.bind_client import SCOPE_DATA, IdentityClient
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
        # a row's world shares its parent's client: it holds no per-row state (the host, pids and names go in each call)
        self.client = parent.client if parent is not None else IdentityClient(base_url, tenant_token)
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
            # scope=data as IdentityClient.from_config gives a minted data token: a data credential's refresh and fork
            # carry the identity's own token (engine 0.1.198), and a client left at the default tenant scope sends none
            self._data = IdentityClient(self.base_url, minted.json()["data_token"], scope=SCOPE_DATA)
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
    own scoped world; an assertion or other exception in it, and a ``pytest.fail`` or a ``pytest.raises`` that did not
    raise, is kept, not raised, so the rows after it still run. A ``pytest.skip`` or ``KeyboardInterrupt`` is never
    swallowed: it ends the run, and ``run`` reports the failures recorded so far first (a skip with failures behind it
    fails the test, naming them). ``check`` fails once, naming every failed row with its traceback and duration, and
    also when fewer rows ran than the test expects (a row dropped from the list must not read as a pass).

    ``NX_ROWS=<substring>`` runs only the rows of a table whose function name contains the substring, to rerun one
    row of a long table (``NX_ROWS=session_start_bind::lapsed uv run pytest
    tests/identity/test_identity_engine_journeys.py``); a value that matches no row fails the test. Unset, every row runs."""

    def __init__(self, world: World, *, prefix: str = "") -> None:
        self._base = world
        self._prefix = f"{prefix}-" if prefix else ""     # rows of different modules must never share a tag
        self.ran: list[str] = []
        self.failed: dict[str, str] = {}
        self.seconds: dict[str, float] = {}

    @contextlib.contextmanager
    def row(self, name: str) -> Iterator[World]:
        assert name not in self.ran, f"row {name!r} ran twice"
        self.ran.append(name)
        started = time.perf_counter()
        try:
            yield self._base.scoped(f"{self._prefix}r{len(self.ran):02d}")
        except pytest.exit.Exception:  # pytest.exit() ends the session, never a row failure (Exit subclasses Exception)
            raise
        except (Exception, pytest.fail.Exception):  # noqa: BLE001 - kept and reported by check(), so one row's failure does not hide the next; Skipped and KeyboardInterrupt are not caught
            self.failed[name] = f"{traceback.format_exc()}(row ran {time.perf_counter() - started:.2f}s)"
        finally:
            self.seconds[name] = time.perf_counter() - started

    def select(self, table: tuple[Callable[[World], None], ...], namespace: dict[str, Any], *, label: str = "",
               only: str | None = None) -> list[Callable[[World], None]]:
        """The rows of *table* to run. *namespace* is the module's ``globals()``: a ``row_*`` function that is not in
        *table* never runs, so it fails here instead of passing. *only* (``NX_ROWS`` when None) keeps the rows whose
        ``label::name`` contains it; with no *label* the name alone. An empty result is the caller's to judge."""
        assert table, "the row table is empty: a test with no rows passes nothing"
        defined = sorted(n for n, o in namespace.items() if n.startswith("row_") and callable(o))
        listed = sorted(fn.__name__ for fn in table)
        assert defined == listed, f"row functions not in the table: {sorted(set(defined) - set(listed))}"
        only = os.environ.get("NX_ROWS", "") if only is None else only
        return [fn for fn in table if only in (f"{label}::{fn.__name__}" if label else fn.__name__)]

    def execute(self, selected: Sequence[Callable[[World], None]]) -> None:
        """Run each row on its own scoped world, keeping failures in ``failed``. A skip or interrupt ends the run."""
        for fn in selected:
            with self.row(fn.__name__) as world:
                fn(world)

    def run(self, table: tuple[Callable[[World], None], ...], namespace: dict[str, Any]) -> None:
        """Run every row function of *table* and fail once with the rows that failed."""
        selected = self.select(table, namespace)
        assert selected, f"NX_ROWS={os.environ.get('NX_ROWS', '')!r} matches no row of {sorted(fn.__name__ for fn in table)}"
        try:
            self.execute(selected)
        except BaseException as stopped:  # a Skipped or KeyboardInterrupt ended the run; the failures so far still show
            if self.failed:
                report = self._report(len(selected)) + f"\n\nthen the run was stopped by {stopped!r}"
                if isinstance(stopped, pytest.skip.Exception):
                    pytest.fail(report, pytrace=False)
                sys.stderr.write(report + "\n")
            raise
        self.check(expect=len(selected))

    def _report(self, expect: int) -> str:
        body = "\n\n".join(f"ROW {name!r} FAILED:\n{tb}" for name, tb in self.failed.items())
        times = ", ".join(f"{name} {secs:.2f}s" for name, secs in self.seconds.items())
        return f"{len(self.failed)} of {expect} rows failed: {sorted(self.failed)}\n\n{body}\n\nrow times: {times}"

    def check(self, expect: int) -> None:
        if len(self.ran) != expect:
            pytest.fail(f"{len(self.ran)} rows ran, the test expects {expect}: ran {self.ran}", pytrace=False)
        if self.failed:
            pytest.fail(self._report(expect), pytrace=False)


@dataclass(frozen=True)
class Journey:
    """One module's rows in :func:`run_journeys`: its name in the report, a short unique tag prefix, its ``ROWS`` table
    and ``globals()``. *precheck* returns the reason the module cannot run on this engine, or None when it can (it
    gets the shared world; use ``world.scoped`` for anything it stages). Such a reason is a failure, so a gate cannot pass
    vacuously (nexus-moht0); only *require_env* set to ``0`` in the environment lets it skip. *cleanup* runs after the module's
    rows, pass or fail, for state the rows leave process-global (a cache)."""

    module: str
    tag: str
    table: tuple[Callable[[World], None], ...]
    namespace: dict[str, Any]
    precheck: Callable[[World], str | None] | None = None
    require_env: str = ""
    cleanup: Callable[[], None] | None = None


@dataclass
class JourneyReport:
    ran: list[str] = field(default_factory=list)                  # ``module::row`` of every row that ran
    skipped: dict[str, str] = field(default_factory=dict)         # module -> why its rows did not run
    seconds: dict[str, float] = field(default_factory=dict)       # ``module::row`` -> duration
    failed: dict[str, str] = field(default_factory=dict)          # ``module::row`` (or ``module``) -> traceback

    def times(self) -> str:
        return ", ".join(f"{name} {secs:.2f}s" for name, secs in self.seconds.items())

    def text(self) -> str:
        skipped = "".join(f"\nSKIPPED module {m!r}: {why}" for m, why in self.skipped.items())
        return f"{len(self.ran)} rows ran{skipped}\nrow times: {self.times()}"


def run_journeys(world: World, journeys: Sequence[Journey]) -> JourneyReport:
    """Run every module's rows on *world* (one tenant) and fail once, naming each failure as ``module::row``.

    Each module runs its own ``Rows`` with its own tag prefix, and its own missing-row check against its own
    ``globals()``. A module that cannot run on this engine (``precheck``) is a failure, unless its *require_env* is ``0``,
    which records it as skipped; the others still run either way. A run in which no row ran fails (a pass over nothing is not a pass). ``NX_ROWS=<substring>`` keeps the rows
    whose ``module::row`` name contains it. A ``pytest.skip`` inside a row ends that module's remaining rows and is
    recorded the same way; ``KeyboardInterrupt`` and ``pytest.exit`` end the run, after the failures so far go to
    stderr. Returns the report of a run that did not fail."""
    modules = [j.module for j in journeys]
    tags = [j.tag for j in journeys]
    assert len(set(modules)) == len(modules), f"two journeys share a module name: {modules}"
    assert len(set(tags)) == len(tags), f"two journeys share a tag prefix, so their rows would share hosts and seats: {tags}"
    report = JourneyReport()
    only = os.environ.get("NX_ROWS", "")
    try:
        for journey in journeys:
            rows = Rows(world, prefix=journey.tag)
            try:
                selected = rows.select(journey.table, journey.namespace, label=journey.module, only=only)
            except AssertionError as bad:                       # a row function missing from the table, or an empty one
                report.failed[journey.module] = f"{journey.module}: {bad}"
                continue
            if not selected:
                continue                                        # NX_ROWS names no row of this module
            reason: str | None = None
            if journey.precheck is not None:
                try:
                    reason = journey.precheck(world)
                except Exception:  # noqa: BLE001 - kept and reported: one module's broken precheck must not hide the others
                    report.failed[journey.module] = f"{journey.module}: precheck failed:\n{traceback.format_exc()}"
                    continue
            if reason:
                if journey.require_env and os.environ.get(journey.require_env) == "0":
                    report.skipped[journey.module] = f"{reason} ({journey.require_env}=0)"
                else:
                    allow = f"; {journey.require_env}=0 lets it skip" if journey.require_env else ""
                    report.failed[journey.module] = f"{journey.module}: {reason} (a module that cannot run is a failure{allow})"
                continue
            counted = None
            try:
                rows.execute(selected)
            except pytest.skip.Exception as skip:               # the rest of this module's rows are not run; the other modules' are
                counted = len(rows.ran) - 1                     # the row that skipped did not run to a verdict
                report.skipped[journey.module] = f"skipped at row {rows.ran[-1]!r} after {counted} of {len(selected)} rows: {skip}"
            finally:
                if journey.cleanup is not None:
                    journey.cleanup()
            for name in rows.ran[:counted]:
                report.ran.append(f"{journey.module}::{name}")
                report.seconds[f"{journey.module}::{name}"] = rows.seconds[name]
            for name, tb in rows.failed.items():
                report.failed[f"{journey.module}::{name}"] = tb
    except BaseException as stopped:  # an interrupt or pytest.exit ended the run; the failures so far still show
        if report.failed:
            sys.stderr.write(_journey_failure(report, len(report.ran)) + f"\n\nthen the run was stopped by {stopped!r}\n")
        raise
    if not report.ran and not report.failed:
        pytest.fail("no row ran: every module was skipped or none matched NX_ROWS=" + repr(only)
                    + "".join(f"\n  {m}: {why}" for m, why in report.skipped.items()), pytrace=False)
    if report.failed:
        pytest.fail(_journey_failure(report, len(report.ran)), pytrace=False)
    return report


def _journey_failure(report: JourneyReport, ran: int) -> str:
    body = "\n\n".join(f"ROW {name!r} FAILED:\n{tb}" for name, tb in report.failed.items())
    return f"{len(report.failed)} failed of {ran} rows run: {sorted(report.failed)}\n\n{body}\n\n{report.text()}"


@pytest.fixture
def world(tmp_path: Path, t2_tenant: str) -> Iterator[World]:
    from tests._engine_substrate import ensure_engine

    made = World(tmp_path, t2_tenant, ensure_engine(), os.environ["NX_SERVICE_URL"], os.environ["NX_SERVICE_TOKEN"])
    yield made
    made.client.close()
    if made._data is not None:
        made._data.close()

