# SPDX-License-Identifier: AGPL-3.0-or-later
"""The engine-backed checks of RDR-253 Step 4 (nexus-hczdo.1.5), as rows of the single identity journeys test.

Each ``row_*`` takes a scoped :class:`~tests.identity.conftest.World` on the journeys test's one tenant and checks that
the REAL engine records what the Step 4 client sends: a take-over's ``takeover`` row and new generation, a release's
``release`` row with the verdict, promote, rename, purge's refusals, a delegated identity's one ``start`` row and its
``end``, a dead dispatcher's ``gone`` row and token-file cleanup, and a delegated child that writes its own threads
entry, reads its parent's and is refused on its parent's. This module holds no test and mints no tenant.

One ``Journey`` entry of tests/identity/test_identity_engine_journeys.py runs ``ROWS`` on the one tenant (tag ``st4``;
``NX_IDENTITY_REQUIRE_STEP2=1`` makes a skip for want of Step 2 a failure). The tenant-free tests of the same behaviour,
against a stand-in engine, are tests/test_identity_cmd.py and tests/test_operator_delegated_identity.py.
"""
from __future__ import annotations

import os
import time

import pytest

from nexus.db.t2.http_memory_store import HttpMemoryStore
from nexus.identity import token_header
from nexus.identity.bind_client import IdentityApiError
from nexus.identity.delegated import clean_delegated_files, delegated_token_path, sweep_delegated
from nexus.identity.ops import CallerFacts, IdentityCommandError, IdentityEnv, claim, promote, purge, release, rename
from nexus.identity.sweep import sweep_host
from nexus.identity.token_file import read_token_file, token_path
from nexus.operators.delegated_identity import DelegatedIdentity
from tests.identity.conftest import VERSION, World

T1 = "2026-11-17T09:00:00Z"
REQUIRE_ENV = "NX_IDENTITY_REQUIRE_STEP2"


def step2_skip_reason(world: World) -> str | None:
    """Why these rows cannot run on the engine, or None when it issues identity tokens (Step 2): the delegated-child
    fence needs them. It stages and ends one seat on a scoped world of its own, so no row sees it."""
    probe_world = world.scoped("st4-probe")
    probe_world.now = time.time()
    probe = probe_world.seat("probe-seat")
    probe_world.process(90, name="probe-seat", os_start=T1, session_id="probe")
    out = probe_world.hook(90, "startup", "probe")
    token = read_token_file(token_path(probe_world.identity_dir, 90, 90_000)).token
    probe_world.procs.stop(90, keep_registry=False)
    probe_world.client.bind(probe, "end", out.generation)
    return None if token else "the engine issues no identity tokens (Step 2, nexus-hczdo.1.2, is not in this jar)"


def _env(world: World, claude_pid: int = 0, ancestors: tuple[int, ...] = ()) -> IdentityEnv:
    return IdentityEnv(client=world.client, procs=world.procs, identity_dir=world.identity_dir, environ={},
                       caller=lambda: CallerFacts(claude_pid=claude_pid, ancestors=ancestors or ((claude_pid,) if claude_pid else ())),
                       clock=lambda: world.now, client_version=VERSION)


def _entry(identity: str, session: str) -> str:
    return f"session: {session}\nidentity: {identity}\n\n## work\nref: none\n"


# ── take-over, release ──────────────────────────────────────────────────────


def row_a_take_over_records_a_takeover_row_a_new_generation_and_ends_the_callers_ephemeral(world: World) -> None:
    alpha = world.seat("alpha")
    world.process(100, name="alpha")
    assert world.hook(100, "startup", "s1").identity == alpha
    world.process(200, os_start=T1, session_id="s2")
    mine = world.hook(200, "startup", "s2")
    assert mine.action == "ephemeral"

    lines = claim(_env(world, 200), world.q("alpha"))

    assert any("took over" in line for line in lines), lines
    view = world.identity(alpha)
    assert (view["generation"], view["state"], view["holder"]["pid"]) == (2, "live", 200)
    assert world.events(alpha) == ["start", "takeover"]
    assert world.events(mine.identity) == ["start", "end"] and world.identity(mine.identity)["state"] == "ended"
    assert read_token_file(token_path(world.identity_dir, 200, 200_000)).identity == alpha
    assert world.indexed("s2")["identity"] == alpha, "the session index follows the take-over"


def row_a_claim_of_an_unheld_seat_starts_it_and_sends_no_takeover(world: World) -> None:
    """RDR-253 Phase 2 (kestrel, 2026-11-22): a seat nobody holds is bound with ``start``, the event a data credential may
    send, so a running Desktop session on a data-scoped credential can claim a seat made for it. ``takeover`` is a
    tenant-credential event and is kept for a seat another process holds (the row above)."""
    seat = world.seat("kestrel")
    world.process(200, os_start=T1, session_id="s2")
    mine = world.hook(200, "startup", "s2")
    assert mine.action == "ephemeral"

    lines = claim(_env(world, 200), world.q("kestrel"))

    assert any("claimed" in line for line in lines), lines
    view = world.identity(seat)
    assert (view["state"], view["holder"]["pid"]) == ("live", 200)
    assert world.events(seat) == ["start"], "an unheld seat is started, never taken over"
    assert world.events(mine.identity) == ["start", "end"]
    assert read_token_file(token_path(world.identity_dir, 200, 200_000)).identity == seat


def row_a_take_over_releases_the_callers_previous_seat_bound_under_a_vanished_hostname(world: World) -> None:
    """GitHub issue 1679: the caller holds the seat ``beta``, bound before the host key under a hostname the network has
    since changed. The engine reads the release as the holder's own only when it names that host, so the client sends the
    host the row records (proved by the hook's token file), and ``beta`` ends unheld instead of staying with a dead binding."""
    alpha, beta = world.seat("alpha"), world.seat("beta")
    legacy = world.q("legacy-102")
    key = world.host
    world.process(200, os_start=T1, session_id="s2")
    world.host = world.procs.host = legacy
    assert world.hook(200, "startup", "s2", seat="beta").identity == beta
    world.host = world.procs.host = key
    lines = claim(_env(world, 200), world.q("alpha"))
    # alpha is unheld, so the claim starts it (the row above): what this row checks is the previous seat's release.
    assert any("claimed" in line for line in lines) and any("closed the previous identity" in line for line in lines), lines
    assert world.identity(beta)["state"] == "unheld" and world.events(beta) == ["start", "release"]
    assert world.identity(alpha)["holder"]["pid"] == 200


def row_release_frees_a_gone_holders_seat_with_the_verdict_and_is_refused_while_the_holder_runs(world: World) -> None:
    alpha, beta = world.seat("alpha"), world.seat("beta")
    for pid, name in ((100, "alpha"), (101, "beta")):
        world.process(pid, name=name)
        world.hook(pid, "startup", f"s{pid}")
    world.procs.stop(100, keep_registry=True)                       # crashed

    plain_shell = _env(world)                                       # no Claude ancestor: release has no guard
    release(plain_shell, world.q("alpha"))
    assert world.identity(alpha)["state"] == "unheld" and world.events(alpha) == ["start", "release"]
    assert world.log(alpha)[-1]["holder_gone"]["pid"] == 100
    with pytest.raises(IdentityCommandError):
        release(plain_shell, world.q("beta"))
    assert world.identity(beta)["state"] == "live"


# ── promote, rename, purge ──────────────────────────────────────────────────


def row_promote_keeps_the_binding_and_the_next_launch_that_names_the_seat_binds_it(world: World) -> None:
    world.process(100, name="")
    eph = world.hook(100, "startup", "s1").identity
    promote(_env(world), eph, world.q("gamma"))
    view = world.identity(eph)
    assert (view["kind"], view["name"], view["generation"], view["holder"]["pid"]) == ("seat", world.q("gamma"), 1, 100)
    assert world.events(eph) == ["start"], "a promote is not a binding"
    # the restart script's path: the old process exits, the relaunch names the seat and finds the holder gone
    world.procs.stop(100, keep_registry=False)
    world.process(101, os_start=T1, name="gamma")
    again = world.hook(101, "startup", "s2", seat="gamma")
    assert (again.identity, again.generation) == (eph, 2)
    with pytest.raises(IdentityCommandError):
        promote(_env(world), eph, "i7")


def row_rename_retires_the_old_alias_appends_a_rename_row_and_a_new_name_is_never_id_shaped(world: World) -> None:
    alpha = world.seat("alpha")
    rename(_env(world), world.q("alpha"), world.q("omega"))
    assert world.identity(alpha)["name"] == world.q("omega") and world.events(alpha) == ["rename"]
    old = world.client.alias(world.q("alpha"))
    assert old["state"] == "retired" and old["current_name"] == world.q("omega") and old.get("notice")
    with pytest.raises(IdentityCommandError):
        rename(_env(world), world.q("omega"), "i9")


def row_purge_is_refused_for_a_live_identity_and_removes_an_ended_ones_state(world: World) -> None:
    world.process(100, name="")
    eph = world.hook(100, "startup", "s1").identity
    with pytest.raises(IdentityCommandError):
        purge(_env(world), eph)
    with pytest.raises(IdentityApiError) as live:
        world.client.purge(eph)
    assert live.value.status == 409, "the engine refuses it too"
    world.client.bind(eph, "end", 1)
    assert "purged" in purge(_env(world), eph)[0]


# ── delegation ──────────────────────────────────────────────────────────────


def _parent(world: World, pid: int = 100) -> tuple[str, str]:
    """A seat held by a running Claude process, and the path of its token file (the dispatcher's parent identity)."""
    world.now = time.time()
    seat = world.seat("parent")
    world.process(pid, name="parent")
    world.hook(pid, "startup", "sp")
    return seat, str(token_path(world.identity_dir, pid, pid * 1000))


def row_a_delegated_identity_has_one_start_row_the_dispatchers_process_and_ends_with_its_token_file_gone(world: World) -> None:
    parent, parent_file = _parent(world)
    dispatcher = os.getpid()
    world.procs.procs[dispatcher] = T1                                # the dispatcher, a running process with no registry file
    handle = DelegatedIdentity.begin(label="row", environ={"NX_IDENTITY_TOKEN": parent_file}, client=world.client,
                                     procs=world.procs, identity_dir=world.identity_dir, clock=lambda: world.now)
    assert handle is not None
    view = world.identity(handle.identity)
    assert (view["kind"], view["parent"], view["generation"], view["state"]) == ("delegated", parent, 1, "live")
    assert view["holder"]["pid"] == dispatcher and view["holder"]["proc_start"] == T1
    assert world.events(handle.identity) == ["start"]
    path = delegated_token_path(world.identity_dir, handle.identity)
    assert path.stat().st_mode & 0o777 == 0o600 and read_token_file(path).token

    assert handle.tick() is True                                      # a renewal
    handle.finish()
    assert not path.exists() and world.events(handle.identity) == ["start", "end"]
    assert world.identity(handle.identity)["state"] == "ended"


def row_a_dead_dispatchers_delegated_identity_gets_a_gone_row_and_its_token_file_is_removed(world: World) -> None:
    _, parent_file = _parent(world)
    dispatcher = os.getpid()
    world.procs.procs[dispatcher] = T1
    handle = DelegatedIdentity.begin(environ={"NX_IDENTITY_TOKEN": parent_file}, client=world.client, procs=world.procs,
                                     identity_dir=world.identity_dir, clock=lambda: world.now)
    assert handle is not None
    path = delegated_token_path(world.identity_dir, handle.identity)
    world.procs.stop(dispatcher, keep_registry=False)                 # the dispatcher died: nobody ended the identity

    report = sweep_host(world.client, world.procs, host=world.host, own=None, identity_dir=world.identity_dir)
    assert report.skipped_delegated == 1 and world.identity(handle.identity)["state"] == "live", "the main sweep still skips it"
    recorded = sweep_delegated(world.client, world.procs, report.delegated_views, host=world.host)
    assert recorded == [handle.identity]
    assert world.identity(handle.identity)["state"] == "lapsed" and world.identity(handle.identity)["gone_at"]
    assert world.events(handle.identity)[-1] == "gone"
    assert clean_delegated_files(world.identity_dir, world.procs, host=world.host) == [path.name] and not path.exists()
    handle._closed = True                                             # nothing left to end: a late finish must not resurrect it


def row_a_delegated_child_writes_its_own_threads_entry_reads_its_parents_and_is_refused_on_it(world: World) -> None:
    """Needs the Step 2 engine (``step2_skip_reason``). The child's token comes from its token file, named by
    ``NX_IDENTITY_TOKEN``; the parent's entry is a fenced family."""
    parent, parent_file = _parent(world)
    handle = DelegatedIdentity.begin(environ={"NX_IDENTITY_TOKEN": parent_file}, client=world.client, procs=world.procs,
                                     identity_dir=world.identity_dir, clock=lambda: world.now)
    assert handle is not None
    real_discover = token_header._discover
    with pytest.MonkeyPatch.context() as monkeypatch:        # a row's patches end with the row; the caches are reset after the rows

        def as_parent() -> None:
            monkeypatch.delenv("NX_IDENTITY_TOKEN", raising=False)
            monkeypatch.setattr(token_header, "_discover", lambda: token_path(world.identity_dir, 100, 100_000))
            token_header.reset_for_tests()

        def as_child() -> None:
            monkeypatch.setenv("NX_IDENTITY_TOKEN", str(handle.path))
            monkeypatch.setattr(token_header, "_discover", real_discover)           # the explicit token comes first
            token_header.reset_for_tests()

        store = HttpMemoryStore(base_url=world.base_url, tenant="default", _token=os.environ["NX_SERVICE_TOKEN"])
        as_parent()
        store.put("threads", world.q("parent"), _entry(parent, "sp"))               # the parent writes its entry
        as_child()
        assert store.get("threads", world.q("parent")) is not None                 # the child reads it: reads are not fenced
        store.put("threads", world.q("child"), _entry(handle.identity, "sc"))        # its own entry
        with pytest.raises(Exception) as refused:                                  # the parent's: not its to write
            store.put("threads", world.q("parent"), _entry(parent, "sc"))
        assert "403" in str(refused.value) and "not_owner" in str(refused.value), str(refused.value)
    handle.finish()


ROWS = (
    row_a_take_over_records_a_takeover_row_a_new_generation_and_ends_the_callers_ephemeral,
    row_a_claim_of_an_unheld_seat_starts_it_and_sends_no_takeover,
    row_a_take_over_releases_the_callers_previous_seat_bound_under_a_vanished_hostname,
    row_release_frees_a_gone_holders_seat_with_the_verdict_and_is_refused_while_the_holder_runs,
    row_promote_keeps_the_binding_and_the_next_launch_that_names_the_seat_binds_it,
    row_rename_retires_the_old_alias_appends_a_rename_row_and_a_new_name_is_never_id_shaped,
    row_purge_is_refused_for_a_live_identity_and_removes_an_ended_ones_state,
    row_a_delegated_identity_has_one_start_row_the_dispatchers_process_and_ends_with_its_token_file_gone,
    row_a_dead_dispatchers_delegated_identity_gets_a_gone_row_and_its_token_file_is_removed,
    row_a_delegated_child_writes_its_own_threads_entry_reads_its_parents_and_is_refused_on_it,
)
