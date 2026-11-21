# SPDX-License-Identifier: AGPL-3.0-or-later
"""What an identity owns, against the real engine (RDR-253 Step 6, nexus-hczdo.1.9): identity scratch, memberships,
per-identity T2 notes and background work.

The rows run inside the ONE tenant of tests/identity/test_identity_engine_journeys.py (``Rows``, ``run_journeys``, see
tests/identity/conftest.py); this module holds no test and mints no tenant. Each row is an independent journey on its own
scoped world. The tenant-free twins (the fake engine, the CLI verbs, the stale rule's every reader) are in
tests/identity/test_work_registry.py.

Rows that send an identity token need the Step 2 engine (``step2_skip_reason``); a jar without it FAILS the module,
and only ``NX_IDENTITY_REQUIRE_STEP2=0`` lets it skip (the nexus-moht0 doctrine).
"""
from __future__ import annotations

import importlib.util
import os
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from nexus.commands import scratch as scratch_cmd
from nexus.config import nexus_config_dir
from nexus.db import t1 as t1_mod
from nexus.db.http_scratch_store import IDENTITY_SCOPE_MAX_ENTRIES, IdentityScratchCapError
from nexus.db.t1 import mint_t1_session_token
from nexus.db.t2.http_memory_store import HttpMemoryStore, identity_project
from nexus.hooks import resume_line
from nexus.identity import membership, token_header, work
from nexus.identity.bind_client import IdentityApiError, IdentityClient
from nexus.identity.ops import CallerFacts, IdentityEnv, is_alive
from nexus.identity.scratch_scope import identity_scratch, scope_owner
from nexus.identity.token_file import read_token_file, token_path
from nexus.operators.delegated_identity import DelegatedIdentity
from tests.identity.conftest import T0, VERSION, World

T1 = "2026-11-17T09:00:00Z"
REQUIRE_ENV = "NX_IDENTITY_REQUIRE_STEP2"


def step2_skip_reason(world: World) -> str | None:
    """Why these rows cannot run on the engine, or None when it issues identity tokens (Step 2)."""
    probe_world = world.scoped("wrk-probe")
    probe_world.now = time.time()
    probe = probe_world.seat("probe-seat")
    probe_world.process(90, name="probe-seat", os_start=T1, session_id="probe")
    out = probe_world.hook(90, "startup", "probe")
    token = read_token_file(token_path(probe_world.identity_dir, 90, 90_000)).token
    probe_world.procs.stop(90, keep_registry=False)
    probe_world.client.bind(probe, "end", out.generation)
    return None if token else "the engine issues no identity tokens (Step 2, nexus-hczdo.1.2, is not in this jar)"


def _env(world: World) -> IdentityEnv:
    return IdentityEnv(client=world.client, procs=world.procs, identity_dir=world.identity_dir, environ={},
                       caller=lambda: CallerFacts(claude_pid=0, ancestors=()), clock=lambda: world.now,
                       client_version=VERSION)


def _as(monkeypatch: pytest.MonkeyPatch, world: World, pid: int) -> None:
    """Calls from here on are a Bash child of Claude process *pid*: its token file is the identity."""
    monkeypatch.delenv("NX_IDENTITY_TOKEN", raising=False)
    monkeypatch.setattr(token_header, "_discover", lambda: token_path(world.identity_dir, pid, pid * 1000))
    token_header.reset_for_tests()


def _nobody(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NX_IDENTITY_TOKEN", raising=False)
    monkeypatch.setattr(token_header, "_discover", lambda: None)
    monkeypatch.setattr(token_header, "has_claude_ancestor", lambda: False)  # a Claude-ancestored run (a laptop or agent session) must still read as nobody
    token_header.reset_for_tests()


def _two_seats(world: World) -> tuple[str, str]:
    world.now = time.time()
    alpha, beta = world.seat("alpha"), world.seat("beta")
    for pid, name in ((100, "alpha"), (101, "beta")):
        world.process(pid, name=name)
        world.hook(pid, "startup", f"s{pid}")
    return alpha, beta


def _session(world: World):
    """The session token an ``nx`` call of this session would carry: the engine's gate admits only a minted one."""
    return SimpleNamespace(_session_token=mint_t1_session_token(world.q("scratch-row"), context="work rows")["session_token"])


def _refused(call) -> str:
    with pytest.raises(Exception) as err:
        call()
    return str(err.value)


# ── memberships ─────────────────────────────────────────────────────────────


def row_a_membership_write_by_a_non_owner_is_refused_and_the_owners_stays(world: World) -> None:
    alpha, beta = _two_seats(world)
    with pytest.MonkeyPatch.context() as monkeypatch:
        _as(monkeypatch, world, 101)                                              # beta writes alpha's membership
        err = pytest.raises(IdentityApiError, membership.join, world.client, alpha, "reviewers", "leader")
        assert err.value.status == 403 and err.value.code == "not_owner", str(err.value)
        _nobody(monkeypatch)                                                      # a shell with no identity at all
        err = pytest.raises(IdentityApiError, membership.join, world.client, alpha, "reviewers", "leader")
        assert err.value.status == 403 and err.value.code == "identity_token_required", str(err.value)
        assert membership.groups(world.client, alpha) == []

        _as(monkeypatch, world, 100)                                              # the owner
        joined = membership.join(world.client, alpha, "reviewers", "member")
        assert (joined.group, joined.role) == ("reviewers", "member") and joined.joined_at
        changed = membership.join(world.client, alpha, "reviewers", "leader")
        assert changed.role == "leader" and changed.joined_at == joined.joined_at, "a role change keeps the join time"
        assert [(m.group, m.role) for m in membership.groups(world.client, alpha)] == [("reviewers", "leader")]
        assert [m.identity for m in membership.roster(world.client, "reviewers")] == [alpha]
        _as(monkeypatch, world, 101)
        pytest.raises(IdentityApiError, membership.leave, world.client, alpha, "reviewers")     # leaving is fenced too
        _as(monkeypatch, world, 100)
        assert membership.leave(world.client, alpha, "reviewers") is True
        assert membership.leave(world.client, alpha, "reviewers") is False, "idempotent"
        assert membership.groups(world.client, alpha) == []


# ── identity scratch ────────────────────────────────────────────────────────


def row_identity_scratch_survives_a_clear_and_a_session_close_and_is_refused_to_a_non_owner(world: World) -> None:
    alpha, beta = _two_seats(world)
    session = _session(world)
    with pytest.MonkeyPatch.context() as monkeypatch:
        _as(monkeypatch, world, 100)
        mine = identity_scratch(session, client=world.client)
        assert mine.session_id == f"identity:{alpha}"
        mine.put(content="working state that must cross /clear", tags="rdr253")

        world.hook(100, "clear", "s2")                                            # /clear: a new session id, the same generation
        after = identity_scratch(session, client=world.client)
        assert [e["content"] for e in after.list_entries()] == ["working state that must cross /clear"]
        closed = after._post("/v1/t1/session/close", {"session_id": f"identity:{alpha}"})
        assert closed.get("deleted") == 0, "a session close never closes the identity scope"
        assert len(after.list_entries()) == 1

        _as(monkeypatch, world, 101)                                              # beta: reads are open, writes are the owner's
        theirs = HttpScratchStoreFor(alpha, session)
        assert [e["content"] for e in theirs.list_entries()] == ["working state that must cross /clear"]
        message = _refused(lambda: theirs.put(content="not yours", tags=""))
        assert "403" in message and "not_owner" in message, message
        entry = theirs.list_entries()[0]["id"]
        assert "403" in _refused(lambda: theirs.delete(entry))
        _nobody(monkeypatch)
        assert "403" in _refused(lambda: HttpScratchStoreFor(alpha, session).put(content="anonymous", tags=""))
        assert len(HttpScratchStoreFor(alpha, session).list_entries()) == 1, "nothing was written"


def HttpScratchStoreFor(owner: str, session) -> object:  # noqa: N802 — a factory named for what it returns
    """A store on *owner*'s identity scope that does not go through ``scope_owner`` (the caller is not the owner)."""
    from nexus.db.http_scratch_store import HttpScratchStore

    return HttpScratchStore(session_id=f"identity:{owner}", _session_token=session._session_token)


def row_a_delegated_child_reads_its_parents_identity_scratch_and_cannot_write_it(world: World) -> None:
    world.now = time.time()
    parent = world.seat("parent")
    world.process(100, name="parent")
    world.hook(100, "startup", "sp")
    parent_file = str(token_path(world.identity_dir, 100, 100_000))
    session = _session(world)
    real_discover = token_header._discover                                            # before _as replaces it
    with pytest.MonkeyPatch.context() as monkeypatch:
        _as(monkeypatch, world, 100)
        identity_scratch(session, client=world.client).put(content="the parent's note", tags="")

        handle = DelegatedIdentity.begin(environ={"NX_IDENTITY_TOKEN": parent_file}, client=world.client, procs=world.procs,
                                         identity_dir=world.identity_dir, clock=lambda: world.now)
        assert handle is not None
        monkeypatch.setenv("NX_IDENTITY_TOKEN", str(handle.path))
        monkeypatch.setattr(token_header, "_discover", real_discover)                 # the explicit token comes first
        token_header.reset_for_tests()
        assert token_header.current().identity == handle.identity != parent, "the child's own token file is the caller's"
        assert scope_owner(world.client) == parent, "a delegated child's scope is its parent's"
        child = identity_scratch(session, client=world.client)
        assert child.session_id == f"identity:{parent}"
        assert [e["content"] for e in child.list_entries()] == ["the parent's note"], "the child reads it"
        message = _refused(lambda: child.put(content="the child writes", tags=""))
        assert "403" in message and "not_owner" in message, message
        assert len(child.list_entries()) == 1
    handle.finish()


def row_a_process_with_no_identity_has_no_identity_scratch(world: World) -> None:
    from nexus.identity.scratch_scope import IdentityScopeError

    with pytest.MonkeyPatch.context() as monkeypatch:
        _nobody(monkeypatch)
        with pytest.raises(IdentityScopeError, match="no identity is bound"):
            scope_owner(world.client)


# ── per-identity T2 ─────────────────────────────────────────────────────────


def row_a_note_in_the_identity_project_is_still_found_after_a_rename(world: World) -> None:
    alpha = world.seat("alpha")
    store = HttpMemoryStore(base_url=world.base_url, tenant="default", _token=os.environ["NX_SERVICE_TOKEN"])
    project = identity_project(alpha)
    assert project == f"identity/{alpha}"
    with pytest.MonkeyPatch.context() as monkeypatch:
        _nobody(monkeypatch)    # a Claude-ancestored run must not reach the identity-notes guard with the dev's own token
        store.put(project, "plan", "the seat's own note")
    world.client.rename(world.q("alpha"), world.q("omega"))
    assert world.identity(alpha)["name"] == world.q("omega")
    found = store.get(identity_project(alpha), "plan")
    assert found is not None and found["content"] == "the seat's own note", "the project is keyed by id: a rename moves nothing"
    assert store.get(f"identity/{world.q('alpha')}", "plan") is None, "and never by name"


# ── background work ─────────────────────────────────────────────────────────


def row_work_rows_follow_the_identity_and_a_crashed_owners_running_row_is_stale(world: World) -> None:
    world.now = time.time()
    alpha, beta = world.seat("alpha"), world.seat("beta")
    for pid, name in ((100, "alpha"), (101, "beta")):
        world.process(pid, name=name)
        world.hook(pid, "startup", f"s{pid}")
    env = _env(world)

    work.register(world.client, alpha, "worktree", "/wt/alpha")
    work.register(world.client, alpha, "tmux", "sock-a")
    work.register(world.client, beta, "worktree", "/wt/alpha")                    # two owners may name one resource
    assert [(w.kind, w.ref, w.state) for w in work.of(world.client, alpha)] == [("tmux", "sock-a", "running"),
                                                                                ("worktree", "/wt/alpha", "running")]
    assert sorted(w.identity for w in work.owners(world.client, "worktree", "/wt/alpha")) == sorted([alpha, beta])
    assert is_alive(env, alpha) and is_alive(env, beta)
    assert sorted(w.identity for w in work.live_owners(world.client, "worktree", "/wt/alpha", lambda i: is_alive(env, i))) == sorted([alpha, beta])

    done = work.mark_done(world.client, beta, "worktree", "/wt/alpha")
    assert done.state == "done"
    assert [w.identity for w in work.live_owners(world.client, "worktree", "/wt/alpha", lambda i: is_alive(env, i))] == [alpha]

    world.procs.stop(100, keep_registry=True)                                      # alpha crashes: its row is still "running"
    assert {w.identity: w.state for w in work.owners(world.client, "worktree", "/wt/alpha")}[alpha] == "running"
    assert not is_alive(env, alpha), "the holder check says the process is gone"
    assert work.running(world.client, alpha, lambda i: is_alive(env, i)) == []
    assert work.live_owners(world.client, "worktree", "/wt/alpha", lambda i: is_alive(env, i)) == []
    assert not is_alive(env, "i999999"), "an identity the engine does not know is not alive"

    assert work.forget(world.client, beta, "worktree", "/wt/alpha") is True
    assert work.forget(world.client, beta, "worktree", "/wt/alpha") is False, "idempotent"
    refused = pytest.raises(IdentityApiError, world.client.put_work, alpha, "daemon", "x")
    assert refused.value.code == "bad_kind"


def row_the_work_limit_forgets_finished_rows_once_and_registers(world: World) -> None:
    alpha = world.seat("alpha")
    world.process(100, name="alpha")
    world.hook(100, "startup", "s1")
    current, old = world.q("s1"), str(uuid.uuid5(uuid.NAMESPACE_URL, world.q("s-old")))   # an earlier SESSION: a UUID, the shape a wait for one has
    world.client.put_work(alpha, "wait", current, "running")                    # the current session's wait: kept
    world.client.put_work(alpha, "wait", old, "running")                        # a wait from an earlier session: dropped
    for n in range(198):
        world.client.put_work(alpha, "labrun", f"w{n}", "done" if n < 120 else "running")
    err = pytest.raises(IdentityApiError, world.client.put_work, alpha, "labrun", "fresh")
    assert err.value.status == 409 and err.value.code == "work_limit"
    row = work.register(world.client, alpha, "labrun", "fresh")
    assert row.state == "running"
    refs = {w.ref for w in work.of(world.client, alpha)}
    assert "fresh" in refs and "w197" in refs and not any(f"w{n}" in refs for n in range(120))
    assert current in refs and old not in refs, "the engine's holder session decides which wait is left over"


def row_identity_scratch_follows_an_in_place_restart_to_the_new_generations_token_file(world: World) -> None:
    """nexus-hczdo: an MCP server outlives an in-place ``/restart``. The restart keeps the Claude pid and gives the
    registry file a new ``startedAt``, so the generation-2 token lives in ``<pid>.<new startedAt>.token`` while the
    server's earlier calls had named ``<pid>.<old startedAt>.token``. The server's identity-scope put and list must
    send generation 2's token, not the still-unexpired generation-1 file's: the engine answers the old token's write
    ``409 generation_superseded`` (a read passes, a token that resolves is never a 401). Discovery is the real one (the
    registry names the file), not the ``_discover`` seam."""
    from nexus.identity import holder

    world.now = time.time()
    alpha = world.seat("alpha")
    world.process(100, name="alpha")
    assert world.hook(100, "startup", "s-restart").generation == 1
    session = _session(world)
    first = token_path(world.identity_dir, 100, 100_000)
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.delenv("NX_IDENTITY_TOKEN", raising=False)
        monkeypatch.setattr(token_header, "_ancestor", lambda: (100, world.identity_dir))
        monkeypatch.setattr(holder, "claude_sessions_dir", lambda: world.procs.sessions_dir)
        token_header.reset_for_tests()
        assert token_header.token_file_path() == first
        before = identity_scratch(session, client=world.client)
        before.put(content="written at generation 1", tags="rdr253")

        world.registry(100, 999_000, T0, session_id="s-restart", name="alpha")    # the in-place /restart: same pid, new startedAt
        out = world.hook(100, "resume", "s-restart")
        assert out.generation == 2, out
        second = token_path(world.identity_dir, 100, 999_000)
        assert read_token_file(second).token != read_token_file(first).token and first.exists(), "both files are on disk"

        after = identity_scratch(session, client=world.client)                    # the MCP builds a store per call
        assert [e["content"] for e in after.list_entries()] == ["written at generation 1"]
        after.put(content="written at generation 2", tags="rdr253")
        assert sorted(e["content"] for e in after.list_entries()) == ["written at generation 1", "written at generation 2"]
        assert len(before.list_entries()) == 2, "a store built before the restart reads on the new token too"
        assert token_header.token_file_path() == second, "the server follows its registry file to the new token file"


def row_a_clear_prints_the_resume_pointer_the_hook_wrote_and_prunes_the_one_before(world: World) -> None:
    """RDR-253 Step 6 part 2: the writer (the real ``nx scratch --scope identity`` commands, driven through
    ``_resume_pointer.write``) and the reader (``nexus.hooks.resume_line``, its default opener) meet in the engine. The writer
    leaves one ``resume-pointer`` entry, the next write puts first and prunes the old one, a ``/clear`` binds a new session id,
    and SessionStart's reader prints exactly that line, including when the new session id has no T1 lease yet (the MCP
    watcher publishes it 0-5 s after SessionStart, so at ``/clear`` it never has one): the identity scope needs no session,
    so the reader reads it with no session header, never calls ``get_t1_database`` and creates no session (nexus-hczdo).
    Other sources print nothing. The 2 KB and 20-entry caps are held against the real store."""
    script = Path(__file__).resolve().parents[2] / "conexus" / "hooks" / "scripts" / "_resume_pointer.py"
    spec = importlib.util.spec_from_file_location("resume_pointer_journey", script)
    assert spec and spec.loader
    rp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rp)

    world.now = time.time()
    alpha = world.seat("alpha")
    world.process(100, name="alpha")
    world.hook(100, "startup", "s1")
    session = _session(world)
    log: list[tuple[str, str]] = []

    def nx(args, cwd, timeout):
        """What ``nx`` does for the hook: the real click command, on this world's engine and identity."""
        result = CliRunner().invoke(scratch_cmd.scratch, args[1:])
        return result.output, (None if result.exit_code == 0 else f"`nx {' '.join(args[:4])}` exited {result.exit_code}: {result.output.strip()[-160:]}")

    with pytest.MonkeyPatch.context() as monkeypatch:
        _as(monkeypatch, world, 100)
        monkeypatch.setattr(IdentityClient, "from_config", classmethod(lambda cls: IdentityClient(world.base_url, world._tenant_token)))
        for name in ("NX_T1_SESSION", "NX_T1_SESSION_ID", "NX_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
            monkeypatch.delenv(name, raising=False)

        board = "## The thread the seat was on\nref: nexus-row01\nstate: halfway\nworktree: /w/row\nwaiting_on: review\n"
        first = rp.compose(board, [], "/w/row")
        assert first and rp.write(first, nx, None, lambda lvl, msg: log.append((lvl, msg))) == "identity", log
        second = rp.compose(board.replace("halfway", "nearly done"), [], "/w/row")
        assert rp.write(second, nx, None, lambda lvl, msg: log.append((lvl, msg))) == "identity", log
        mine = identity_scratch(session, client=world.client)
        pointers = [e for e in mine.list_entries() if e["tags"] == "resume-pointer"]
        assert [e["content"] for e in pointers] == [second], "put first, then the earlier pointer is deleted: one remains"

        world.hook(100, "clear", "s2")                                            # /clear: a new session id, the same identity
        s2 = world.q("s2")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", s2)                          # what Claude Code sets: an explicit id with NO lease, NO NX_T1_SESSION
        config_dir = nexus_config_dir()
        leases_before = sorted(p.name for p in config_dir.glob("t1_session_lease.*"))
        assert not [n for n in leases_before if s2 in n], "no lease for the new session id exists before the read"
        with pytest.raises(t1_mod.T1ServerNotFoundError):                          # intended: get_t1_database stays strict about an explicit id with no lease
            t1_mod.get_t1_database()                                              # (the pre-fix reader's path), so the row is not vacuous. If it is ever made
                                                                                  # lenient this goes red for that reason, not because the reader regressed.
        minted: list[str] = []
        monkeypatch.setattr(t1_mod, "mint_t1_session_token", lambda sid, *, context: minted.append(sid) or {"session_token": "t"})
        line = resume_line.with_resume_line("GUIDANCE", {"session_id": s2, "source": "clear"})
        assert line and line.startswith("GUIDANCE\n\nResume pointer (identity scratch, written just now): thread: The thread the seat was on")
        assert "worktree: /w/row" in line and "state: nearly done" in line and "waiting_on: review" in line and line.count("\n") == 2
        assert minted == [], "the reader creates no T1 session"
        assert sorted(p.name for p in config_dir.glob("t1_session_lease.*")) == leases_before, "and publishes no lease"
        for source in ("resume", "startup", "compact", "fork"):
            assert resume_line.with_resume_line("GUIDANCE", {"source": source}) == "GUIDANCE", source

        # the caps, against the real store: 2 KB an entry; 20 non-pointer entries; the pointer's slot is its own
        with pytest.raises(IdentityScratchCapError, match="2048"):
            mine.put(content="x" * 2049, tags="working")
        for n in range(IDENTITY_SCOPE_MAX_ENTRIES):
            mine.put(content=f"note {n}", tags="working")
        with pytest.raises(IdentityScratchCapError, match="20"):
            mine.put(content="the 21st", tags="working")
        assert rp.write(rp.compose(board, [], "/w/row"), nx, None, lambda lvl, msg: log.append((lvl, msg))) == "identity", log
        assert len([e for e in mine.list_entries() if e["tags"] == "resume-pointer"]) == 1
        assert len(mine.list_entries()) == IDENTITY_SCOPE_MAX_ENTRIES + 1
        assert scope_owner(world.client) == alpha


def row_an_agent_is_a_kind_of_work_and_a_wait_for_a_ci_run_survives_the_work_limit_purge(world: World) -> None:
    """RDR-253 Step 6 part 1 (nexus-hczdo): the real engine accepts ``kind=agent`` (identity-005 widens the CHECK as well
    as the service's own list) and marks it done in place; an engine from before it answers ``bad_kind``, which the hooks
    log and carry on past. A ``nx ci wait`` row (``kind=wait``, ref ``ci:<branch>@<sha>``) is not a wait for a session, so
    the 200-row purge never drops it as a superseded one."""
    alpha = world.seat("alpha")
    world.process(100, name="alpha")
    world.hook(100, "startup", "s1")
    ref = "agent:a220cf773a02bf4d7"
    assert work.register(world.client, alpha, "agent", ref).state == "running"
    assert work.mark_done(world.client, alpha, "agent", ref).state == "done"
    assert [(w.kind, w.ref, w.state) for w in work.owners(world.client, "agent", ref)] == [("agent", ref, "done")]
    assert work.register_own("agent", "agent:b1", client=world.client, identity=alpha, purge=False) is True
    _, began = work.holder_facts(world.client, alpha)
    assert began == 100.0, "the engine's holder view carries the registry's startedAt in milliseconds (pid 100 * 1000)"
    live = {(w["kind"], w["ref"]): w["stale"] for w in work.list_rows(_env(world), alpha)["work"]}
    assert live[("agent", "agent:b1")] is False, "written after its holder began: not a ghost (the engine's ISO updated_at parses)"
    assert work.forget_own("agent", "agent:b1", client=world.client, identity=alpha) is True
    assert not [w for w in work.owners(world.client, "agent", "agent:b1")], "the stop's forget removed the real row"
    ci = "ci:develop@0123456789abcdef"
    work.register(world.client, alpha, "wait", ci)
    for n in range(198):                                                        # 2 rows above (the done agent, the ci wait) + 198 = the 200-row limit
        world.client.put_work(alpha, "labrun", f"c{n}", "done" if n < 150 else "running")
    assert work.register(world.client, alpha, "labrun", "fresh").state == "running", "the purge made room"
    assert ci in {w.ref for w in work.of(world.client, alpha)}, "a CI wait is not a superseded session wait"


ROWS = (
    row_a_membership_write_by_a_non_owner_is_refused_and_the_owners_stays,
    row_identity_scratch_survives_a_clear_and_a_session_close_and_is_refused_to_a_non_owner,
    row_a_delegated_child_reads_its_parents_identity_scratch_and_cannot_write_it,
    row_identity_scratch_follows_an_in_place_restart_to_the_new_generations_token_file,
    row_a_clear_prints_the_resume_pointer_the_hook_wrote_and_prunes_the_one_before,
    row_a_process_with_no_identity_has_no_identity_scratch,
    row_a_note_in_the_identity_project_is_still_found_after_a_rename,
    row_work_rows_follow_the_identity_and_a_crashed_owners_running_row_is_stale,
    row_the_work_limit_forgets_finished_rows_once_and_registers,
    row_an_agent_is_a_kind_of_work_and_a_wait_for_a_ci_run_survives_the_work_limit_purge,
)
