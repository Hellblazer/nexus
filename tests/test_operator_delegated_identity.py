# SPDX-License-Identifier: AGPL-3.0-or-later
"""Delegated identities for ``claude -p`` children (RDR-253 Step 4, nexus-hczdo.1.5). Tenant-free: the engine is
:class:`tests.identity.fake_engine.FakeEngine`, the spawn is the dispatch tests' own mock process, and the machine is a
fake registry tree under ``tmp_path``. (``tests/test_operator_dispatch.py`` is over the test size cap, hence this file.)
What the real engine records for a delegated identity is checked by ``tests/identity/test_identity_step4.py``.

Covers: the child's environment and token file at spawn; the token file gone and an ``end`` row on every exit path
(success, harness failure, timeout, spawn failure); renewal and the half-life refresh; no delegation without a parent
identity or for a tool-free dispatch; and the DISCOVER decision: a dead dispatcher's delegated identity gets a ``gone``
row from the SessionStart sweep and its token file is removed.
"""
from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.hooks.identity_bind import bind_at_session_start
from nexus.identity.bind_client import IdentityApiError, IdentityClient, IdentityUnavailableError
from nexus.identity.delegated import (
    check_dispatcher, clean_delegated_files, delegated_dir, delegated_token_path, sweep_delegated,
)
from nexus.identity.holder import GONE, OTHER_HOST, RUNNING, epoch_seconds
from nexus.identity.sweep import sweep_host
from nexus.identity.token_file import TokenFile, read_token_file, token_path, write_token_file
from nexus.operators.dispatch import claude_dispatch
from nexus.operators.delegated_identity import DelegatedIdentity, start_delegation
from tests._module_seam import patch_in
from tests.identity.fake_engine import FakeEngine
from tests.identity.fakes import HOST, FakeProcesses

T0 = "2026-11-17T08:00:00Z"
T1 = "2026-11-17T09:00:00Z"
SCHEMA = {"type": "object"}


class Machine:
    """A parent identity held by a Claude process, a dispatcher (this process) and an engine, all in ``tmp_path``."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.engine = FakeEngine()
        self.procs = FakeProcesses(tmp_path / "claude-sessions", host=HOST)
        self.config = tmp_path / "config"
        self.identity_dir = self.config / "identity"
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(self.config))
        monkeypatch.setattr("nexus.session.find_immediate_claude_pid", lambda *a, **k: 0)
        # the dispatcher is this process: the fake table knows its pid and a fixed start time
        self.dispatcher = os.getpid()
        self.procs.procs[self.dispatcher] = T1
        monkeypatch.setattr("nexus.identity.holder.Processes", lambda *a, **k: self.procs)
        monkeypatch.setattr(IdentityClient, "from_config", classmethod(lambda cls, **kw: self.engine))
        self.parent = self.engine.stage(kind="seat", name="alpha", state="live", generation=3,
                                        holder={"host": HOST, "pid": 100, "started_at": 100_000, "proc_start": T0,
                                                "session_id": "sid-100"})

    def parent_token_file(self) -> Path:
        """The parent's token file, which is where a dispatcher under that Claude finds its identity."""
        path = token_path(self.identity_dir, 100, 100_000)
        write_token_file(path, TokenFile(identity=self.parent, generation=3, token="parent-token", expires_at=9e9,
                                         session_id="sid-100", pid=100, started_at=100_000, proc_start=T0, host=HOST))
        return path

    def as_child_of_claude_100(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """This process runs under Claude 100: its nearest Claude ancestor's registry file and token file name the parent."""
        self.procs.start(100, T0, session_id="sid-100", name="alpha", started_at=100_000)
        self.parent_token_file()
        monkeypatch.setattr("nexus.session.find_immediate_claude_pid", lambda *a, **k: 100)

    def delegated_ids(self) -> list[str]:
        return [i for i, r in self.engine.records.items() if r["kind"] == "delegated"]

    def events(self, identity: str) -> list[str]:
        return [row["event"] for row in self.engine.logs[identity]]


@pytest.fixture
def box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Machine:
    return Machine(tmp_path, monkeypatch)


# ── begin ───────────────────────────────────────────────────────────────────


def test_no_parent_identity_makes_no_engine_call_and_no_file(box: Machine) -> None:
    assert DelegatedIdentity.begin(environ={}) is None
    assert box.engine.calls == [] and not delegated_dir(box.identity_dir).exists()


def test_a_dispatcher_under_a_claude_session_creates_a_delegated_identity_and_a_0600_token_file(
        box: Machine, monkeypatch: pytest.MonkeyPatch) -> None:
    box.as_child_of_claude_100(monkeypatch)
    handle = DelegatedIdentity.begin(label="extract", environ={})
    assert handle is not None
    (parent, display, evidence), = box.engine.ids_called("create_delegated")
    assert (parent, display) == (box.parent, "extract")
    assert (evidence["host"], evidence["pid"], evidence["proc_start"]) == (HOST, box.dispatcher, T1), "the DISPATCHER's process"
    assert evidence["started_at"] == epoch_seconds(T1) * 1000, "its OS start time in milliseconds"
    view = box.engine.bound(handle.identity)
    assert (view["kind"], view["parent"], view["generation"], view["state"]) == ("delegated", box.parent, 1, "live")
    assert box.events(handle.identity) == ["start"]
    path = delegated_token_path(box.identity_dir, handle.identity)
    assert handle.path == path and path.stat().st_mode & 0o777 == 0o600
    record = read_token_file(path)
    assert (record.identity, record.generation, record.pid, record.host) == (handle.identity, 1, box.dispatcher, HOST)
    assert record.token == f"tok-{handle.identity}-1" and record.expires_at


def test_a_dispatcher_that_is_itself_a_delegated_child_takes_its_parent_from_its_token_file(
        box: Machine, monkeypatch: pytest.MonkeyPatch) -> None:
    mine = delegated_token_path(box.identity_dir, "i77")
    write_token_file(mine, TokenFile(identity="i77", generation=1, token="t", expires_at=9e9, session_id=None, pid=1,
                                     started_at=1, proc_start=None, host=HOST))
    box.engine.records["i77"] = {**box.engine.records[box.parent], "id": "i77", "kind": "delegated"}
    box.engine.logs["i77"] = []
    handle = DelegatedIdentity.begin(environ={"NX_IDENTITY_TOKEN": str(mine)})
    assert handle is not None and box.engine.ids_called("create_delegated")[0][0] == "i77"
    assert DelegatedIdentity.begin(environ={"NX_IDENTITY_TOKEN": str(mine) + ".missing"}) is None, "a token file it cannot read names no parent"


def test_an_engine_that_refuses_the_create_leaves_the_dispatch_as_it_was(box: Machine, monkeypatch: pytest.MonkeyPatch) -> None:
    box.as_child_of_claude_100(monkeypatch)
    box.engine.refuse["create_delegated"] = IdentityApiError(403, "forbidden", "a 'data' credential may not call DELEGATED", {})
    assert DelegatedIdentity.begin(environ={}) is None
    assert not delegated_dir(box.identity_dir).exists()
    assert ("close", ()) in box.engine.calls, "a client begin opened is closed when the create fails"


# ── renewal, refresh, finish ────────────────────────────────────────────────


def test_a_tick_renews_and_a_refresh_at_half_life_rewrites_the_file(box: Machine, monkeypatch: pytest.MonkeyPatch) -> None:
    box.as_child_of_claude_100(monkeypatch)
    now = {"t": 10_000.0}
    handle = DelegatedIdentity.begin(environ={}, clock=lambda: now["t"])
    assert handle is not None
    first = read_token_file(handle.path)
    assert handle.tick() is True
    assert box.engine.ids_called("renew") == [(handle.identity, 1)]
    assert read_token_file(handle.path).token == first.token, "a fresh token is not refreshed"
    now["t"] += 1_801.0                                                      # past half of the hour
    assert handle.tick() is True
    second = read_token_file(handle.path)
    assert second.token != first.token and second.generation == 1 and second.expires_at > first.expires_at
    assert [b[1] for b in box.engine.ids_called("bind")] == ["refresh"]
    assert box.events(handle.identity) == ["start", "refresh"]


def test_a_renewal_the_engine_says_is_no_longer_ours_stops_the_loop(box: Machine, monkeypatch: pytest.MonkeyPatch) -> None:
    box.as_child_of_claude_100(monkeypatch)
    handle = DelegatedIdentity.begin(environ={})
    box.engine.refuse["renew"] = IdentityApiError(409, "generation_superseded", "superseded", {})
    assert handle.tick() is False
    box.engine.refuse["renew"] = IdentityApiError(503, "unavailable", "try again", {})
    assert handle.tick() is True, "a transient failure is tried again on the next beat"


def test_finish_stops_renewal_deletes_the_file_and_appends_end_once(box: Machine, monkeypatch: pytest.MonkeyPatch) -> None:
    box.as_child_of_claude_100(monkeypatch)
    handle = DelegatedIdentity.begin(environ={})
    handle.finish()
    assert not handle.path.exists() and box.events(handle.identity) == ["start", "end"]
    assert box.engine.bound(handle.identity)["state"] == "ended"
    assert handle.tick() is False, "a closed handle never renews or rewrites its file"
    assert not handle.path.exists()
    handle.finish()                                                          # idempotent: the second `end` is the engine's no-op


@pytest.mark.asyncio
async def test_the_renewal_task_renews_while_the_child_runs_and_stops_at_finish(box: Machine, monkeypatch: pytest.MonkeyPatch) -> None:
    box.as_child_of_claude_100(monkeypatch)
    handle = DelegatedIdentity.begin(environ={}, interval_s=0.01)
    handle.start_renewal()
    await asyncio.sleep(0.15)
    renewed = len(box.engine.ids_called("renew"))
    assert renewed >= 2, "renewed on its beat while the child ran"
    handle.finish()
    await asyncio.sleep(0.05)
    assert len(box.engine.ids_called("renew")) in (renewed, renewed + 1), "a beat in flight may land; none after"
    after = len(box.engine.ids_called("renew"))
    await asyncio.sleep(0.05)
    assert len(box.engine.ids_called("renew")) == after


# ── start_delegation: the child's environment ───────────────────────────────


@pytest.mark.asyncio
async def test_start_delegation_names_the_childs_own_token_file_in_place_of_the_parents(
        box: Machine, monkeypatch: pytest.MonkeyPatch) -> None:
    box.as_child_of_claude_100(monkeypatch)
    env = {"PATH": "/usr/bin", "NX_IDENTITY_TOKEN": "/parent/i4.token"}
    handle = await start_delegation(env, enabled=True, label="rank")
    try:
        assert handle is not None and env["NX_IDENTITY_TOKEN"] == str(handle.path) != "/parent/i4.token"
        assert env["PATH"] == "/usr/bin"
    finally:
        handle.finish()


@pytest.mark.asyncio
async def test_a_dispatch_that_grants_no_tools_or_has_no_parent_is_left_alone(box: Machine, monkeypatch: pytest.MonkeyPatch) -> None:
    env = {"PATH": "/usr/bin"}
    assert await start_delegation(env, enabled=True) is None and env == {"PATH": "/usr/bin"}, "no parent identity"
    box.as_child_of_claude_100(monkeypatch)
    assert await start_delegation(env, enabled=False) is None and env == {"PATH": "/usr/bin"}, "a tool-free child cannot reach nexus"
    assert box.delegated_ids() == []


@pytest.mark.asyncio
async def test_a_cancellation_while_the_identity_is_being_created_still_ends_it(box: Machine, monkeypatch: pytest.MonkeyPatch) -> None:
    """The create runs in an uninterruptible thread; cancelling the dispatch meanwhile must not leave the identity live
    and its token file on disk (nothing else would ever end it while the dispatcher lives)."""
    box.as_child_of_claude_100(monkeypatch)
    gate = threading.Event()
    real_begin = DelegatedIdentity.begin

    def slow_begin(**kw: Any) -> Any:
        gate.wait(5)
        return real_begin(**kw)

    monkeypatch.setattr(DelegatedIdentity, "begin", staticmethod(slow_begin))
    env: dict[str, str] = {}
    task = asyncio.ensure_future(start_delegation(env, enabled=True))
    await asyncio.sleep(0.05)
    task.cancel()
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    (delegated,) = box.delegated_ids()
    assert box.events(delegated) == ["start", "end"] and box.engine.bound(delegated)["state"] == "ended"
    assert not delegated_token_path(box.identity_dir, delegated).exists() and env == {}


# ── claude_dispatch: every exit path ────────────────────────────────────────


@pytest.fixture(autouse=True)
def _dispatch_stand_ins(monkeypatch: pytest.MonkeyPatch) -> None:
    """What the dispatch tests stub, as tests/test_llm_backend.py does: the T1 mint a tool-granting dispatch makes, and
    its close (``_close_dispatch_session`` builds an ``HttpScratchStore`` and an ``HttpTokenStore``, which would reach an
    engine this tenant-free file does not have: the storage-touch guard errors the test)."""
    monkeypatch.setattr("nexus.db.t1.mint_t1_session_token",
                        lambda session_id, *, context: {"session_token": f"fake-dispatch-tok-{session_id}"})
    monkeypatch.setattr("nexus.operators.dispatch._close_dispatch_session", lambda session_id, session_token: None)


def _reader(data: bytes) -> Any:
    state = {"served": False}

    async def read(n: int = -1) -> bytes:
        if state["served"]:
            return b""
        state["served"] = True
        return data

    return read


def _proc(stdout: bytes = b'{"ok": true}', returncode: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = MagicMock()
    proc.stdout.read = _reader(stdout)
    proc.stderr = MagicMock()
    proc.stderr.read = _reader(b"boom" if returncode else b"")
    proc.wait = AsyncMock(return_value=returncode)
    proc.kill = MagicMock()
    return proc


async def _dispatch(box: Machine, *, spawn: Any, tools: list[str] | None = ("Bash",), timeout: float = 5.0) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    async def intercept(*args: Any, **kwargs: Any) -> Any:
        env = kwargs["env"]
        seen["token_env"] = env.get("NX_IDENTITY_TOKEN")
        path = Path(env["NX_IDENTITY_TOKEN"]) if env.get("NX_IDENTITY_TOKEN") else None
        seen["file_at_spawn"] = path is not None and path.exists()
        seen["mode"] = path.stat().st_mode & 0o777 if seen["file_at_spawn"] else None
        seen["record"] = read_token_file(path) if seen["file_at_spawn"] else None
        return await spawn(*args, **kwargs)

    with patch_in("nexus.operators.dispatch", "asyncio.create_subprocess_exec", side_effect=intercept):
        try:
            await claude_dispatch("prompt", SCHEMA, timeout=timeout, allowed_tools=list(tools) if tools else None,
                                  operator="extract")
        except BaseException as exc:  # noqa: BLE001 — the exit path under test; the assertions below read what it left
            seen["raised"] = exc
    return seen


async def _ok(*a: Any, **k: Any) -> Any:
    return _proc()


async def _harness_failure(*a: Any, **k: Any) -> Any:
    return _proc(stdout=b"", returncode=1)


async def _spawn_fails(*a: Any, **k: Any) -> Any:
    raise OSError("fork/exec failed")


async def _hangs(*a: Any, **k: Any) -> Any:
    proc = _proc()
    calls = {"n": 0}

    async def hang(n: int = -1) -> bytes:
        calls["n"] += 1
        if calls["n"] == 1:
            await asyncio.sleep(999)
        return b""

    proc.stdout.read = hang
    proc.stderr.read = hang
    return proc


@pytest.mark.asyncio
@pytest.mark.parametrize("spawn,timeout,raises", [(_ok, 5.0, False), (_harness_failure, 5.0, True),
                                                   (_hangs, 0.02, True), (_spawn_fails, 5.0, True)],
                         ids=["success", "harness-failure", "timeout-kill", "spawn-failure"])
async def test_the_childs_token_file_is_in_its_environment_and_gone_with_an_end_row_on_every_exit(
        box: Machine, monkeypatch: pytest.MonkeyPatch, spawn: Any, timeout: float, raises: bool) -> None:
    up = delegated_token_path(box.identity_dir, "i-up")                      # a nested dispatcher: its parent is a delegated child
    write_token_file(up, TokenFile(identity=box.parent, generation=3, token="x", expires_at=9e9, session_id=None, pid=1,
                                   started_at=1, proc_start=None, host=HOST))
    monkeypatch.setenv("NX_IDENTITY_TOKEN", str(up))

    seen = await _dispatch(box, spawn=spawn, timeout=timeout)

    (delegated,) = box.delegated_ids()
    path = delegated_token_path(box.identity_dir, delegated)
    if spawn is not _spawn_fails:
        assert seen["token_env"] == str(path) != str(up), "the child names ITS token file, not its parent's"
        assert seen["file_at_spawn"] and seen["mode"] == 0o600 and seen["record"].identity == delegated
    assert ("raised" in seen) is raises
    assert not path.exists(), "the token file is gone"
    assert box.events(delegated) == ["start", "end"] and box.engine.bound(delegated)["state"] == "ended"


@pytest.mark.asyncio
async def test_cancelling_the_dispatch_while_the_child_runs_ends_the_identity_and_removes_its_file(
        box: Machine, monkeypatch: pytest.MonkeyPatch) -> None:
    box.as_child_of_claude_100(monkeypatch)
    task = asyncio.ensure_future(_dispatch(box, spawn=_hangs, timeout=60.0))
    for _ in range(300):                                          # the child "runs": its identity exists and its file is on disk
        await asyncio.sleep(0.01)
        ids = box.delegated_ids()
        if ids and delegated_token_path(box.identity_dir, ids[0]).exists():
            break
    (delegated,) = box.delegated_ids()
    task.cancel()
    seen = await task                                             # _dispatch keeps the exception the cancelled dispatch raised
    assert isinstance(seen["raised"], asyncio.CancelledError)
    assert not delegated_token_path(box.identity_dir, delegated).exists()
    assert box.events(delegated) == ["start", "end"] and box.engine.bound(delegated)["state"] == "ended"


@pytest.mark.asyncio
async def test_a_tool_free_dispatch_gets_no_delegated_identity_and_never_the_parents_token(
        box: Machine, monkeypatch: pytest.MonkeyPatch) -> None:
    box.as_child_of_claude_100(monkeypatch)
    monkeypatch.setenv("NX_IDENTITY_TOKEN", "/parent/i4.token")
    seen = await _dispatch(box, spawn=_ok, tools=None)
    assert "raised" not in seen and seen["token_env"] is None, "the parent's token is stripped from every claude child"
    assert box.delegated_ids() == [] and box.engine.ids_called("create_delegated") == []


# ── a dead dispatcher: the sweep and the hook's cleanup ─────────────────────


def _delegated(box: Machine, pid: int, *, proc_start: str = T1, host: str = HOST, state: str = "live") -> str:
    ident = box.engine.stage(kind="delegated", state=state, generation=1, parent=box.parent,
                             holder={"host": host, "pid": pid, "started_at": pid * 1000, "proc_start": proc_start,
                                     "session_id": None})
    path = delegated_token_path(box.identity_dir, ident)
    write_token_file(path, TokenFile(identity=ident, generation=1, token="t", expires_at=9e9, session_id=None, pid=pid,
                                     started_at=pid * 1000, proc_start=proc_start, host=host))
    return ident


def test_check_dispatcher_is_the_probe_and_the_start_time_compare_alone(box: Machine) -> None:
    box.procs.procs[300] = T1
    box.procs.procs[301] = T0
    holder = lambda pid, **kw: {"host": HOST, "pid": pid, "started_at": pid * 1000, "proc_start": T1, **kw}  # noqa: E731
    assert check_dispatcher(holder(300), procs=box.procs).state == RUNNING
    assert check_dispatcher(holder(299), procs=box.procs).state == GONE, "not running"
    assert check_dispatcher(holder(301), procs=box.procs).state == GONE, "the pid was reused"
    assert check_dispatcher(holder(300, host="elsewhere"), procs=box.procs).state == OTHER_HOST
    assert check_dispatcher(holder(300, proc_start=None), procs=box.procs).state == RUNNING, "no recorded start time"
    box.procs.os_start = lambda pid: None                                    # type: ignore[method-assign]
    assert check_dispatcher(holder(300), procs=box.procs).state != GONE, "an unreadable start time is never gone"


def test_the_sweep_records_gone_for_a_dead_dispatchers_delegated_identity_and_leaves_the_rest(box: Machine) -> None:
    box.procs.procs[100] = T0                                                # the parent seat's holder runs
    box.procs.procs[300] = T1                                                # a running dispatcher
    box.procs.procs[302] = T0                                                # pid 302 was reused
    alive = _delegated(box, 300)
    dead = _delegated(box, 301)
    reused = _delegated(box, 302)
    remote = _delegated(box, 303, host="elsewhere")

    report = sweep_host(box.engine, box.procs, host=HOST, own=None, identity_dir=box.identity_dir)
    assert report.skipped_delegated == 3 and box.engine.ids_called("gone") == [], "the main sweep still skips them"
    recorded = sweep_delegated(box.engine, box.procs, report.delegated_views, host=HOST)

    assert sorted(recorded) == sorted([dead, reused])
    for ident in (dead, reused):
        assert box.engine.bound(ident)["state"] == "lapsed" and box.engine.bound(ident)["gone_at"]
        assert box.events(ident) == ["gone"]
    assert box.engine.bound(alive)["state"] == "live" and box.engine.bound(remote)["state"] == "live"
    # a second sweep records nothing more: the engine answers recorded:false and the sweep reports none
    again = sweep_delegated(box.engine, box.procs, sweep_host(box.engine, box.procs, host=HOST, own=None,
                                                              identity_dir=box.identity_dir).delegated_views, host=HOST)
    assert again == []


def test_a_credential_that_may_not_report_gone_ends_the_delegated_sweep_quietly(box: Machine) -> None:
    dead = _delegated(box, 301)
    views = [box.engine.show(dead)]
    box.engine.gone = lambda *a, **k: (_ for _ in ()).throw(IdentityApiError(403, "forbidden", "no", {}))  # type: ignore[method-assign]
    assert sweep_delegated(box.engine, box.procs, views, host=HOST) == []


def test_the_hooks_cleanup_removes_a_dead_dispatchers_token_file_and_only_that(box: Machine) -> None:
    box.procs.procs[300] = T1
    alive = _delegated(box, 300)
    dead = _delegated(box, 301)
    unreadable = delegated_token_path(box.identity_dir, "i99")
    unreadable.write_text("not json", encoding="utf-8")
    removed = clean_delegated_files(box.identity_dir, box.procs, host=HOST)
    assert removed == [f"{dead}.token"]
    assert delegated_token_path(box.identity_dir, alive).exists() and unreadable.exists()
    assert not delegated_token_path(box.identity_dir, dead).exists()
    assert clean_delegated_files(box.identity_dir / "missing", box.procs, host=HOST) == []


def test_the_session_start_hook_runs_both_for_a_dead_dispatcher(box: Machine, tmp_path: Path) -> None:
    """The hook leg end to end: its own bind, then the sweep, then the delegated sweep and the file cleanup."""
    box.procs.start(500, T1, session_id="sid-500", name="", started_at=500_000)
    box.procs.procs[300] = T1
    box.procs.procs[100] = T0                                                # the parent seat's holder runs (no registry file)
    alive = _delegated(box, 300)
    dead = _delegated(box, 301)

    result = bind_at_session_start("sid-500", "startup", env={}, claude_pid=500, procs=box.procs, client=box.engine,
                                   config_dir=box.config, clock=lambda: 1_000.0, sleep=lambda s: None)

    assert result.outcome is not None and result.outcome.action == "ephemeral"
    assert [c[0] for c in box.engine.ids_called("gone")] == [dead]
    assert box.engine.bound(dead)["state"] == "lapsed" and box.engine.bound(alive)["state"] == "live"
    assert not delegated_token_path(box.identity_dir, dead).exists() and delegated_token_path(box.identity_dir, alive).exists()


def _forbid_gone(box: Machine) -> None:
    def refuse(*a: Any, **k: Any) -> Any:
        raise IdentityApiError(403, "forbidden", "a 'data' credential may not call GONE", {})

    box.engine.gone = refuse                                                # type: ignore[method-assign]


@pytest.mark.parametrize("main_sweep_hits_the_403", [True, False], ids=["main-sweep-403", "delegated-sweep-403"])
def test_a_hook_sweep_that_ended_on_a_403_deletes_no_delegated_token_file(box: Machine, main_sweep_hits_the_403: bool) -> None:
    """RDR "Retention": a run whose sweep ended on a 403 deletes no token file. The 403 may come from the main sweep (a
    gone seat holder) or from the first delegated row."""
    box.procs.start(500, T1, session_id="sid-500", name="", started_at=500_000)
    box.procs.procs[100] = T0                                                # the parent seat's holder runs
    dead = _delegated(box, 301)
    if main_sweep_hits_the_403:
        box.engine.stage(kind="seat", name="crashed", state="live", generation=1,
                         holder={"host": HOST, "pid": 700, "started_at": 700_000, "proc_start": T0, "session_id": "x"})
    _forbid_gone(box)
    bind_at_session_start("sid-500", "startup", env={}, claude_pid=500, procs=box.procs, client=box.engine,
                          config_dir=box.config, clock=lambda: 1_000.0, sleep=lambda s: None)
    assert delegated_token_path(box.identity_dir, dead).exists()
    assert box.engine.bound(dead)["state"] == "live"


def test_one_bad_delegated_row_does_not_stop_the_rest_of_the_sweep(box: Machine) -> None:
    first, second = _delegated(box, 301), _delegated(box, 302)
    real_gone = box.engine.gone

    def flaky(identity: str, *a: Any, **k: Any) -> Any:
        if identity == first:
            raise IdentityApiError(500, "internal", "boom", {})
        return real_gone(identity, *a, **k)

    box.engine.gone = flaky                                                 # type: ignore[method-assign]
    views = [box.engine.show(first), box.engine.show(second)]
    assert sweep_delegated(box.engine, box.procs, views, host=HOST) == [second]
    assert box.engine.bound(first)["state"] == "live" and box.engine.bound(second)["state"] == "lapsed"
    box.engine.gone = lambda *a, **k: (_ for _ in ()).throw(IdentityUnavailableError("down"))   # type: ignore[method-assign]
    assert sweep_delegated(box.engine, box.procs, [box.engine.show(first)], host=HOST) == []


def test_the_hook_binds_nothing_for_a_delegated_child(box: Machine) -> None:
    box.procs.start(500, T1, session_id="sid-500", started_at=500_000)
    result = bind_at_session_start("sid-500", "startup", env={"NX_IDENTITY_TOKEN": "/x/i9.token"}, claude_pid=500,
                                   procs=box.procs, client=box.engine, config_dir=box.config)
    assert result.outcome is None and box.engine.calls == []
