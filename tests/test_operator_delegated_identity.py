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
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from structlog.testing import capture_logs

from nexus.hooks.identity_bind import bind_at_session_start
from nexus.identity.bind_client import IdentityApiError, IdentityClient, IdentityUnavailableError
from nexus.identity.delegated import (
    check_dispatcher, clean_delegated_files, clean_delegated_tmp, delegated_dir, delegated_token_path,
    no_identity_path, sweep_delegated,
)
from nexus.identity.holder import GONE, OTHER_HOST, RUNNING, epoch_seconds
from nexus.identity.sweep import sweep_host
from nexus.identity.token_file import TokenFile, read_token_file, token_path, write_token_file
from nexus.operators import dispatch as dispatch_module
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
    assert handle.path == path
    if os.name == "posix":  # Windows has no POSIX mode bits
        assert path.stat().st_mode & 0o777 == 0o600
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


def test_a_token_file_that_cannot_be_written_ends_the_identity_it_just_created(box: Machine, monkeypatch: pytest.MonkeyPatch) -> None:
    """The create succeeded and no child will ever carry its token: it must not stay live until it lapses, and the
    client begin opened is closed."""
    box.as_child_of_claude_100(monkeypatch)

    def disk_full(*a: Any, **k: Any) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("nexus.identity.token_file.write_token_file", disk_full)
    assert DelegatedIdentity.begin(environ={}) is None
    (delegated,) = box.delegated_ids()
    assert box.events(delegated) == ["start", "end"] and box.engine.bound(delegated)["state"] == "ended"
    assert ("close", ()) in box.engine.calls and not delegated_token_path(box.identity_dir, delegated).exists()


@pytest.mark.asyncio
async def test_a_hung_engine_neither_stalls_the_event_loop_nor_the_dispatchs_exit(box: Machine, monkeypatch: pytest.MonkeyPatch) -> None:
    """finish ran its HTTP and a lock wait on the nx-mcp event loop. Now the file goes at once, `end` runs in a thread, and
    the caller waits for it only up to a bound: other MCP calls keep being served, and `end` still lands when it can."""
    box.as_child_of_claude_100(monkeypatch)
    handle = DelegatedIdentity.begin(environ={})
    gate, real_bind = threading.Event(), box.engine.bind

    def hung(identity: str, event: str, *a: Any, **k: Any) -> Any:
        if event == "end":
            gate.wait(10)
        return real_bind(identity, event, *a, **k)

    box.engine.bind = hung                                                  # type: ignore[method-assign]
    ticks = {"n": 0}

    async def ticker() -> None:
        while True:
            await asyncio.sleep(0.005)
            ticks["n"] += 1

    other_call = asyncio.ensure_future(ticker())
    started = time.monotonic()
    await handle.afinish(timeout_s=0.2)
    waited = time.monotonic() - started
    assert waited < 2.0, "the wait is bounded"
    assert ticks["n"] >= 10, "the loop served other work while the engine hung"
    assert not handle.path.exists(), "the token file is gone before the engine is asked"
    assert box.events(handle.identity) == ["start"]
    gate.set()                                                               # the engine answers: `end` still lands
    for _ in range(200):
        if box.events(handle.identity) == ["start", "end"]:
            break
        await asyncio.sleep(0.01)
    other_call.cancel()
    assert box.events(handle.identity) == ["start", "end"]


@pytest.mark.asyncio
async def test_a_tool_granting_dispatch_whose_create_is_refused_runs_with_the_sentinel(box: Machine, monkeypatch: pytest.MonkeyPatch) -> None:
    box.as_child_of_claude_100(monkeypatch)
    box.engine.refuse["create_delegated"] = IdentityApiError(403, "forbidden", "no", {})
    seen = await _dispatch(box, spawn=_ok)
    assert "raised" not in seen and seen["token_env"] == str(no_identity_path(box.identity_dir))
    assert box.delegated_ids() == [] and not seen["file_at_spawn"]


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
async def test_a_child_with_no_delegated_identity_names_a_token_file_that_does_not_exist(
        box: Machine, monkeypatch: pytest.MonkeyPatch) -> None:
    """No parent identity, a tool-free dispatch, or an engine that refuses: the child's NX_IDENTITY_TOKEN names the
    sentinel, so it acts as no identity and never falls back to a walk up to the parent's Claude and its token file
    (the explicit token file is read first; its hook binds nothing; ``nx`` sends no token)."""
    sentinel = str(no_identity_path(box.identity_dir))
    env = {"PATH": "/usr/bin", "NX_IDENTITY_TOKEN": "/parent/i4.token"}
    assert await start_delegation(env, enabled=True) is None and env["NX_IDENTITY_TOKEN"] == sentinel, "no parent identity"
    box.as_child_of_claude_100(monkeypatch)
    env = {"PATH": "/usr/bin"}
    assert await start_delegation(env, enabled=False) is None and env["NX_IDENTITY_TOKEN"] == sentinel, "a tool-free child"
    box.engine.refuse["create_delegated"] = IdentityApiError(403, "forbidden", "no", {})
    env = {"PATH": "/usr/bin"}
    assert await start_delegation(env, enabled=True) is None and env["NX_IDENTITY_TOKEN"] == sentinel, "a refused create"
    assert not Path(sentinel).exists() and box.delegated_ids() == [] and env["PATH"] == "/usr/bin"
    # what the sentinel means to the code that reads it: no token file, so nothing to send and nothing to resolve
    assert read_token_file(Path(sentinel)) is None


def test_the_sentinel_stops_the_ancestor_walk_for_the_token_header_and_the_hook(box: Machine, monkeypatch: pytest.MonkeyPatch) -> None:
    """With the parent's Claude token file in reach (a child whose own process is not recognised as a Claude), a child
    that names the sentinel sends no token and the hook binds nothing: neither falls back to the ancestor's file."""
    from nexus.identity import token_header

    box.as_child_of_claude_100(monkeypatch)                                    # the parent's token file exists
    monkeypatch.setattr(token_header, "_ancestor", lambda: (100, box.identity_dir))
    token_header.reset_for_tests()
    monkeypatch.delenv("NX_IDENTITY_TOKEN", raising=False)
    monkeypatch.setattr(token_header, "ancestor_token_path", lambda d, pid, sessions_dir=None: token_path(d, pid, 100_000))
    assert token_header.current() is not None, "without the sentinel the walk finds the parent's token"
    monkeypatch.setenv("NX_IDENTITY_TOKEN", str(no_identity_path(box.identity_dir)))
    token_header.reset_for_tests()
    assert token_header.current() is None, "with it, no token is sent"
    box.procs.start(500, T1, session_id="sid-500", started_at=500_000)
    result = bind_at_session_start("sid-500", "startup", env={"NX_IDENTITY_TOKEN": str(no_identity_path(box.identity_dir))},
                                   claude_pid=500, procs=box.procs, client=box.engine, config_dir=box.config)
    assert result.outcome is None and box.engine.calls == []
    token_header.reset_for_tests()


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
    assert not delegated_token_path(box.identity_dir, delegated).exists() and "NX_IDENTITY_TOKEN" not in env


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
        assert seen["file_at_spawn"] and seen["record"].identity == delegated
        if os.name == "posix":  # Windows has no POSIX mode bits
            assert seen["mode"] == 0o600
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


def _gate_end(box: Machine, gate: threading.Event, started: threading.Event, wait_s: float = 2.0) -> None:
    """The engine's ``end`` blocks on *gate* (at most *wait_s*), and says when it has started."""
    real_bind = box.engine.bind

    def gated(identity: str, event: str, *a: Any, **k: Any) -> Any:
        if event == "end":
            started.set()
            gate.wait(wait_s)
        return real_bind(identity, event, *a, **k)

    box.engine.bind = gated                                                 # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_the_dispatch_exit_waits_for_end_off_the_event_loop(box: Machine, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pins claude_dispatch to ``afinish``: with the engine's ``end`` blocked, a dispatch run to completion leaves the
    event loop serving other tasks through its bounded wait. The synchronous ``finish()`` would block the loop on the
    engine's call (the ticker would stand still for the whole gate)."""
    monkeypatch.setattr("nexus.operators.delegated_identity.END_TIMEOUT_S", 0.4)
    box.as_child_of_claude_100(monkeypatch)
    gate, started = threading.Event(), threading.Event()
    _gate_end(box, gate, started)
    ticks, at_end = {"n": 0}, {}

    async def ticker() -> None:
        while True:
            await asyncio.sleep(0.005)
            ticks["n"] += 1
            if started.is_set() and "n" not in at_end:
                at_end["n"] = ticks["n"]

    other_call = asyncio.ensure_future(ticker())
    seen = await _dispatch(box, spawn=_ok)
    during_exit = ticks["n"] - at_end.get("n", ticks["n"])
    other_call.cancel()
    gate.set()
    assert "raised" not in seen
    assert during_exit >= 20, f"the loop ticked {during_exit} times while `end` was blocked: it served other work"
    (delegated,) = box.delegated_ids()
    for _ in range(300):                                                    # `end` lands once the engine answers
        if box.events(delegated) == ["start", "end"]:
            break
        await asyncio.sleep(0.01)
    assert box.events(delegated) == ["start", "end"]


@pytest.mark.asyncio
async def test_a_second_cancel_during_the_bounded_wait_for_end_still_cleans_up(box: Machine, monkeypatch: pytest.MonkeyPatch) -> None:
    """The wait for `end` is cancellable, so it runs LAST in the dispatch's cleanup: a second cancel there must not skip
    the prompt temp file's unlink or leave the token file, and `end` (in a thread the cancel does not interrupt) still
    lands once the engine answers."""
    box.as_child_of_claude_100(monkeypatch)
    gate, started = threading.Event(), threading.Event()
    _gate_end(box, gate, started, wait_s=10.0)
    prompt_files: list[Path] = []
    real_write = dispatch_module._write_prompt_file

    def spy(prompt: str) -> Path:
        path = real_write(prompt)
        prompt_files.append(path)
        return path

    monkeypatch.setattr(dispatch_module, "_write_prompt_file", spy)
    task = asyncio.ensure_future(_dispatch(box, spawn=_hangs, timeout=60.0))
    for _ in range(300):                                                    # the child "runs"
        await asyncio.sleep(0.01)
        ids = box.delegated_ids()
        if ids and delegated_token_path(box.identity_dir, ids[0]).exists() and prompt_files:
            break
    (delegated,) = box.delegated_ids()
    assert prompt_files and prompt_files[0].exists(), "the prompt file is on disk while the child runs"
    task.cancel()                                                           # the first cancel: the dispatch unwinds
    for _ in range(300):
        if started.is_set():                                                # `end` is asked and blocked: afinish is waiting
            break
        await asyncio.sleep(0.01)
    assert started.is_set()
    task.cancel()                                                           # the second cancel, inside the bounded wait
    seen = await task
    assert isinstance(seen["raised"], asyncio.CancelledError)
    assert not prompt_files[0].exists(), "the prompt temp file was unlinked before the wait that a cancel can cut short"
    assert not delegated_token_path(box.identity_dir, delegated).exists()
    assert box.events(delegated) == ["start"], "`end` is still blocked in its thread"
    gate.set()
    for _ in range(300):
        if box.events(delegated) == ["start", "end"]:
            break
        await asyncio.sleep(0.01)
    assert box.events(delegated) == ["start", "end"] and box.engine.bound(delegated)["state"] == "ended"


@pytest.mark.asyncio
async def test_an_afinish_that_raises_does_not_mask_the_dispatch_result_and_is_logged(box: Machine, monkeypatch: pytest.MonkeyPatch) -> None:
    box.as_child_of_claude_100(monkeypatch)

    async def broken(self: DelegatedIdentity, timeout_s: float | None = None) -> None:
        self.close_local()
        raise RuntimeError("to_thread broke")

    monkeypatch.setattr(DelegatedIdentity, "afinish", broken)
    with capture_logs() as logs:
        seen = await _dispatch(box, spawn=_ok)
    assert "raised" not in seen, "the dispatch's own outcome stands"
    failed = [entry for entry in logs if entry["event"] == "operator_dispatch_delegated_end_failed"]
    assert len(failed) == 1 and "to_thread broke" in failed[0]["error"]


@pytest.mark.asyncio
async def test_a_tool_free_dispatch_gets_no_delegated_identity_and_never_the_parents_token(
        box: Machine, monkeypatch: pytest.MonkeyPatch) -> None:
    box.as_child_of_claude_100(monkeypatch)
    monkeypatch.setenv("NX_IDENTITY_TOKEN", "/parent/i4.token")
    seen = await _dispatch(box, spawn=_ok, tools=None)
    assert "raised" not in seen and seen["token_env"] == str(no_identity_path(box.identity_dir)), \
        "the parent's token is replaced by the sentinel: a child with no delegated identity acts as none"
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


def test_stale_temp_files_in_the_delegated_directory_are_removed_and_fresh_ones_are_not(box: Machine) -> None:
    """A crashed dispatcher's half-written token file (`<name>.<pid>.<uuid>.tmp`): removed by the top-level rule's age
    (older than 10 minutes), so the file a live dispatcher is writing this instant is never touched."""
    directory = delegated_dir(box.identity_dir)
    directory.mkdir(parents=True)
    stale, fresh, other = directory / "i5.token.123.abc.tmp", directory / "i6.token.124.def.tmp", directory / "i7.token"
    for path in (stale, fresh, other):
        path.write_text("x", encoding="utf-8")
    old = time.time() - 700
    os.utime(stale, (old, old))
    os.utime(other, (old, old))
    assert clean_delegated_tmp(box.identity_dir) == [stale.name]
    assert not stale.exists() and fresh.exists() and other.exists(), "only a stale temp file goes"
    assert clean_delegated_tmp(box.identity_dir / "missing") == []


def test_the_hook_removes_stale_delegated_temp_files_but_not_after_a_403(box: Machine) -> None:
    box.procs.start(500, T1, session_id="sid-500", name="", started_at=500_000)
    box.procs.procs[100] = T0
    directory = delegated_dir(box.identity_dir)
    directory.mkdir(parents=True)
    stale = directory / "i5.token.1.abc.tmp"
    stale.write_text("x", encoding="utf-8")
    os.utime(stale, (time.time() - 700, time.time() - 700))
    dead = _delegated(box, 301)
    hook = lambda: bind_at_session_start("sid-500", "startup", env={}, claude_pid=500, procs=box.procs, client=box.engine,  # noqa: E731
                                         config_dir=box.config, clock=lambda: 1_000.0, sleep=lambda s: None)
    _forbid_gone(box)
    hook()
    assert stale.exists() and delegated_token_path(box.identity_dir, dead).exists(), "a sweep that ended on a 403 cleans nothing"
    del box.engine.gone                                                       # the real route again
    hook()
    assert not stale.exists() and not delegated_token_path(box.identity_dir, dead).exists()


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
