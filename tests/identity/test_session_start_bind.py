# SPDX-License-Identifier: AGPL-3.0-or-later
"""The SessionStart policy table (RDR-253), one row per function, against a real engine.

The engine's compare-and-set and policy are real (``tests/identity/conftest.py``); the machine is a process table
under ``tmp_path``. The rows are independent journeys: each runs on its own scoped world (its own host, process
table, seat names and session ids) inside the ONE tenant of tests/identity/test_identity_engine_journeys.py
(tests/storage_touch_ceiling.toml); this module holds no test and mints no tenant. A failing row names itself in the
failure and does not stop the rows after it. A row's docstring names the row of the RDR's table it covers.
"""
from __future__ import annotations

import pytest

from nexus.identity.bind_client import IdentityApiError, evidence_body
from nexus.identity.resolver import IdentityConflictError, resolve_identity
from nexus.identity.token_file import TokenFile, read_token_file, token_path, write_token_file
from tests.identity.conftest import T0, World

T1 = "2026-11-17T09:00:00Z"
T2 = "2026-11-17T10:00:00Z"


def _held_by(world: World, seat: str, pid: int, *, host: str, sid: str | None = None, started_at: int | None = None) -> None:
    """Stage *seat* held by a process with the engine, without a registry file (for a holder on another host)."""
    world.client.bind(seat, "start", 0, evidence_body(
        session_id=world.q(sid or f"held-{pid}"), host=host, pid=pid,
        started_at=started_at if started_at is not None else pid * 1000, proc_start=T0, client_version="1", lease="leased"))


# ── startup ─────────────────────────────────────────────────────────────────


def row_startup_with_no_seat_named_binds_an_ephemeral_identity(world: World) -> None:
    """Row: ``startup``, no seat named: ephemeral, event ``start``."""
    world.process(100)
    out = world.hook(100, "startup", "sid-a")
    assert (out.action, out.kind, out.generation, out.event) == ("ephemeral", "ephemeral", 1, "start")
    shown = world.identity(out.identity)
    assert shown["state"] == "live" and shown["holder"]["pid"] == 100 and shown["name"] is None
    assert shown["holder"]["proc_start"] == T0 and shown["client_version"] == "9.9.9-test"
    assert world.indexed("sid-a")["identity"] == out.identity
    assert world.events(out.identity) == ["start"]
    on_disk = read_token_file(token_path(world.identity_dir, 100, 100_000))
    assert (on_disk.identity, on_disk.generation, on_disk.session_id) == (out.identity, 1, world.q("sid-a"))


def row_a_dash_n_name_that_is_no_seat_is_ephemeral_with_a_display_name_and_takes_no_alias(world: World) -> None:
    """Row: ``startup`` with a non-seat ``-n``: ephemeral, the name is its ``display_name``, no alias is consumed, so
    two launches under one display name never collide."""
    world.process(100, name="my-task")
    world.process(101, name="my-task", os_start=T1)
    first = world.hook(100, "startup", "sid-a")
    second = world.hook(101, "startup", "sid-b")
    assert first.identity != second.identity and {first.kind, second.kind} == {"ephemeral"}
    assert world.identity(first.identity)["display_name"] == world.q("my-task")
    with pytest.raises(IdentityApiError) as err:
        world.client.alias(world.q("my-task"))
    assert err.value.status == 404


def row_a_dash_n_that_matches_a_live_seat_binds_it_and_nx_seat_wins_over_it(world: World) -> None:
    """Row: ``startup``, a seat named, unheld: bound at a new generation, event ``start``. ``NX_SEAT`` beats ``-n``."""
    alpha, beta = world.seat("alpha"), world.seat("beta")
    world.process(100, name="alpha")
    named = world.hook(100, "startup", "sid-a")
    assert (named.identity, named.action, named.generation, named.event) == (alpha, "bound", 1, "start")
    world.process(101, name="alpha", os_start=T1)
    wins = world.hook(101, "startup", "sid-b", seat="beta")
    assert (wins.identity, wins.generation) == (beta, 1)
    assert world.identity(alpha)["holder"]["pid"] == 100


def row_nx_seat_naming_no_seat_is_ephemeral_and_says_so(world: World) -> None:
    """Row: ``startup``, ``NX_SEAT`` naming no seat: ephemeral, and the hook says what it did."""
    world.process(100)
    out = world.hook(100, "startup", "sid-a", seat="nosuchseat")
    assert out.action == "ephemeral" and out.kind == "ephemeral"
    assert any(f"NX_SEAT={world.q('nosuchseat')} names no seat" in n for n in out.notices)


def row_nx_seat_naming_a_renamed_seat_is_ephemeral_and_the_notice_names_the_new_name(world: World) -> None:
    """Row: ``startup``, ``NX_SEAT`` naming no seat: "a seat that was renamed gets a notice naming its new name". The
    old name still resolves for the 7-day grace (as retired); the hook must not bind the seat through it."""
    seat = world.seat("old-name")
    world.client._request("POST", "/rename", json={"old": world.q("old-name"), "new": world.q("new-name")})
    world.process(100)
    out = world.hook(100, "startup", "sid-a", seat="old-name")
    assert out.action == "ephemeral" and out.kind == "ephemeral" and out.identity != seat
    assert any(f"NX_SEAT={world.q('old-name')} was renamed to {world.q('new-name')}" in n for n in out.notices), out.notices
    assert world.identity(seat)["state"] == "unheld" and "start" not in world.events(seat)


def row_startup_naming_an_ended_seat_from_a_process_with_no_identity_binds_the_next_generation(world: World) -> None:
    """Row: ``startup``, a seat named, unheld or ended: bound, new generation, event ``start``. The seat's last holder
    ended cleanly (``ended``, no holder); a new process that holds nothing names it."""
    alpha = world.seat("alpha")
    world.process(100, name="alpha")
    assert world.hook(100, "startup", "sid-a").identity == alpha
    world.client.bind(alpha, "end", 1)
    world.procs.stop(100, keep_registry=False)
    assert world.identity(alpha)["state"] == "ended"
    world.process(101, name="alpha", os_start=T1)
    out = world.hook(101, "startup", "sid-b")
    assert (out.action, out.identity, out.generation, out.event) == ("bound", alpha, 2, "start")
    shown = world.identity(alpha)
    assert shown["state"] == "live" and shown["holder"]["pid"] == 101
    assert world.events(alpha) == ["start", "end", "start"]


def row_startup_naming_a_seat_live_elsewhere_is_ephemeral_and_the_remedy_is_printed_only_interactively(world: World) -> None:
    """Row: ``startup``, a seat live in another process: ephemeral; the hook prints the holder and, in an
    interactive session only, the remedy ``nx identity claim <seat> --take-over``."""
    alpha = world.seat("alpha")
    world.process(100)
    assert world.hook(100, "startup", "sid-a", seat="alpha").identity == alpha
    world.process(101, os_start=T1, entrypoint="cli")
    world.process(102, os_start=T2, entrypoint="sdk-cli")
    interactive = world.hook(101, "startup", "sid-b", seat="alpha")
    child = world.hook(102, "startup", "sid-c", seat="alpha")
    for out in (interactive, child):
        assert out.action == "ephemeral" and out.identity != alpha
        assert any("held by pid 100" in n for n in out.notices)
    assert any(f"nx identity claim {world.q('alpha')} --take-over" in n for n in interactive.notices)
    assert not any("take-over" in n for n in child.notices)
    assert world.identity(alpha)["holder"]["pid"] == 100 and world.identity(alpha)["generation"] == 1


def row_startup_naming_a_lapsed_seat_whose_holder_still_runs_is_ephemeral(world: World) -> None:
    """Row: ``startup``, a seat lapsed, holder still running on this host (a stuck process, a long sleep): ephemeral,
    however long the lapse."""
    alpha = world.seat("alpha")
    world.process(100)
    world.hook(100, "startup", "sid-a", seat="alpha")
    world.lapse(alpha, seconds_ago=86_400)
    assert world.identity(alpha)["state"] == "lapsed"
    world.process(101, os_start=T1)
    out = world.hook(101, "startup", "sid-b", seat="alpha")
    assert out.action == "ephemeral" and out.identity != alpha
    assert world.identity(alpha)["generation"] == 1 and world.identity(alpha)["holder"]["pid"] == 100


def row_startup_naming_a_seat_whose_holder_is_gone_binds_the_next_generation_and_the_live_holder_needs_the_verdict(world: World) -> None:
    """Row: ``startup``, a seat whose holder no longer exists: bound, new generation, event ``start``. The engine
    still shows the seat ``live`` (the holder never wrote ``end``): without ``holder_gone`` the bind is 409 ``held``
    (the live-holder variant); the client's verdict, sent from the holder check, is what lets it through."""
    alpha = world.seat("alpha")
    world.process(100)
    world.hook(100, "startup", "sid-a", seat="alpha")
    world.procs.stop(100, keep_registry=True)            # a crash: the registry file stays, the pid is gone
    assert world.identity(alpha)["state"] == "live"
    with pytest.raises(IdentityApiError) as held:
        world.client.bind(alpha, "start", 1, evidence_body(session_id=world.q("x"), host=world.host, pid=101, started_at=101_000, proc_start=T1))
    assert (held.value.status, held.value.code) == (409, "held")
    world.process(101, os_start=T1)
    out = world.hook(101, "startup", "sid-b", seat="alpha")
    assert (out.action, out.identity, out.generation, out.event) == ("bound", alpha, 2, "start")
    row = world.log(alpha)[-1]
    assert row["event"] == "start" and row["holder_gone"]["pid"] == 100 and row["holder_gone"]["host"] == world.host


def row_a_seat_held_from_another_host_stays_reserved_for_ten_minutes_after_the_lapse(world: World) -> None:
    """Row: ``startup``, lapsed less than 10 minutes ago (another host): ephemeral; after it, bound. Judged by the
    lease alone: the check cannot read the other machine's files."""
    alpha = world.seat("alpha")
    elsewhere = world.q("elsewhere")
    _held_by(world, alpha, 7, host=elsewhere)
    world.lapse(alpha, seconds_ago=400)                  # lapsed (due at 300) and reserved until 900
    world.process(100)
    inside = world.hook(100, "startup", "sid-a", seat="alpha")
    assert inside.action == "ephemeral" and world.identity(alpha)["holder"]["host"] == elsewhere
    world.lapse(alpha, seconds_ago=1_000)
    world.process(101, os_start=T1)
    after = world.hook(101, "startup", "sid-b", seat="alpha")
    assert (after.action, after.identity, after.generation) == ("bound", alpha, 2)


# ── restart modes ───────────────────────────────────────────────────────────


def row_each_restart_mode_finds_the_old_holder_gone_and_binds_the_next_generation_with_no_release_step(world: World) -> None:
    """The three ways a seat's process is replaced: exit and relaunch (a new pid), an in-place ``/restart`` (the same
    pid and OS start time with a new ``startedAt``; its hook fires ``source: resume``) and ``--revive`` after a reboot
    (no registry files at all). One rule covers them: the holder check finds the old holder gone and ``resume``
    binds the next generation. No ``release`` row is written for any of them."""
    seats = {mode: world.seat(mode) for mode in ("relaunch", "inplace", "revive")}
    for pid, (mode, seat) in zip((100, 200, 300), seats.items()):
        world.process(pid, name=mode)
        assert world.hook(pid, "startup", f"{mode}-sid").identity == seat

    # exit and relaunch: the old process is gone for good, and the new pid resumes the conversation
    world.procs.stop(100, keep_registry=False)
    world.process(101, os_start=T1)
    relaunch = world.hook(101, "resume", "relaunch-sid")
    # in-place /restart: same pid and OS start time, a new startedAt, source resume, same session id
    world.registry(200, 999_000, T0, session_id="inplace-sid", name="inplace")
    inplace = world.hook(200, "resume", "inplace-sid")
    # --revive after a reboot: every process is gone, no registry file survives
    world.procs.stop(300, keep_registry=False)
    world.process(301, os_start=T2)
    revive = world.hook(301, "resume", "revive-sid")

    for out, mode in ((relaunch, "relaunch"), (inplace, "inplace"), (revive, "revive")):
        assert (out.action, out.identity, out.generation, out.event) == ("bound", seats[mode], 2, "resume"), mode
        events = world.events(seats[mode])
        assert events == ["start", "resume"] and "release" not in events, mode
    assert world.identity(seats["inplace"])["holder"]["started_at"] == 999_000


# ── resume ──────────────────────────────────────────────────────────────────


def row_resume_of_an_unindexed_session_id_is_treated_as_startup_with_the_seat_the_launch_names(world: World) -> None:
    """Row: ``resume``, the id is in no index (a session from before this RDR): treated as ``startup``."""
    alpha = world.seat("alpha")
    world.process(100, name="alpha")
    out = world.hook(100, "resume", "from-before-the-rdr")
    assert (out.action, out.identity, out.event) == ("bound", alpha, "start")
    assert world.indexed("from-before-the-rdr")["identity"] == alpha


def row_resume_of_an_ended_ephemeral_identity_is_treated_as_startup(world: World) -> None:
    """Row: ``resume``, the id's identity is an ephemeral one that has ended: treated as ``startup``."""
    world.process(100)
    first = world.hook(100, "startup", "sid-a")
    world.client.bind(first.identity, "end", 1)
    assert world.identity(first.identity)["state"] == "ended"
    world.process(101, os_start=T1)
    out = world.hook(101, "resume", "sid-a")
    assert out.action == "ephemeral" and out.identity != first.identity
    assert world.indexed("sid-a")["identity"] == out.identity          # the index follows the new binding


def row_resume_of_an_ended_delegated_identity_is_treated_as_startup_with_the_seat_the_launch_names(world: World) -> None:
    """Row: ``resume``, the id's identity is an ephemeral or delegated identity that has ended: treated as ``startup``
    with the seat the launch names (the ephemeral case is the row above)."""
    alpha, parent = world.seat("alpha"), world.seat("parent")
    child = world.client._request("POST", "/delegated", json={"parent": parent, "bind": evidence_body(
        session_id=world.q("child-sid"), host=world.host, pid=900, started_at=900_000, proc_start=T0)})["id"]
    world.client.bind(child, "end", 1)
    assert (world.identity(child)["kind"], world.identity(child)["state"]) == ("delegated", "ended")
    assert world.indexed("child-sid")["identity"] == child
    world.process(100, name="alpha", os_start=T1)
    out = world.hook(100, "resume", "child-sid")
    assert (out.action, out.identity, out.generation, out.event) == ("bound", alpha, 1, "start")
    assert world.indexed("child-sid")["identity"] == alpha                 # the index follows the new binding
    assert world.events(child) == ["start", "end"]                         # the delegated identity is left as it was


def row_resume_of_an_ended_or_an_unheld_seat_in_a_process_with_no_identity_binds_it_at_the_next_generation(world: World) -> None:
    """Row: ``resume``, the resumed id's seat unheld or ended: bound, new generation (the process is new), event
    ``resume``. One seat's holder ended cleanly; the other seat was released."""
    ended, unheld = world.seat("ended"), world.seat("unheld")
    world.process(100, name="ended")
    world.process(101, name="unheld", os_start=T1)
    assert world.hook(100, "startup", "ended-sid").identity == ended
    assert world.hook(101, "startup", "unheld-sid").identity == unheld
    world.client.bind(ended, "end", 1)
    world.client.bind(unheld, "release", 1, evidence_body(
        session_id=world.q("unheld-sid"), host=world.host, pid=101, started_at=101_000, proc_start=T1))
    world.procs.stop(100, keep_registry=False)
    world.procs.stop(101, keep_registry=False)
    assert (world.identity(ended)["state"], world.identity(unheld)["state"]) == ("ended", "unheld")

    world.process(200, os_start=T2)                                        # no name, no identity: only the index leads to the seat
    world.process(201, os_start="2026-11-17T11:00:00Z")
    for pid, sid, seat, was in ((200, "ended-sid", ended, "end"), (201, "unheld-sid", unheld, "release")):
        out = world.hook(pid, "resume", sid)
        assert (out.action, out.identity, out.generation, out.event) == ("bound", seat, 2, "resume"), sid
        assert world.identity(seat)["state"] == "live" and world.identity(seat)["holder"]["pid"] == pid, sid
        assert world.events(seat) == ["start", was, "resume"], sid


def row_resume_of_a_seat_live_in_this_same_process_is_unchanged(world: World) -> None:
    """Row: ``resume``, live in this same process: unchanged, no new generation."""
    alpha = world.seat("alpha")
    world.process(100, name="alpha")
    world.hook(100, "startup", "sid-a")
    out = world.hook(100, "resume", "sid-a")
    assert (out.action, out.identity, out.generation) == ("unchanged", alpha, 1)
    assert world.identity(alpha)["generation"] == 1


def row_resume_of_a_seat_live_in_another_process_is_ephemeral_with_the_holder(world: World) -> None:
    """Row: ``resume``, live in another process: ephemeral, with the holder (and, interactively, the remedy)."""
    alpha = world.seat("alpha")
    world.process(100, name="alpha")
    world.hook(100, "startup", "sid-a")
    world.process(101, os_start=T1)
    out = world.hook(101, "resume", "sid-a")
    assert out.action == "ephemeral" and out.identity != alpha
    assert any("held by pid 100" in n and f"nx identity claim {world.q('alpha')} --take-over" in n for n in out.notices)


def row_a_cross_seat_resume_switches_the_process_and_refuses_a_bash_child_from_before_the_switch(world: World) -> None:
    """Row: ``resume`` in a process that already holds a different identity, the resumed seat unheld: the process
    switches (its seat is released, the resumed seat is bound at a new generation); the marker carries the new
    identity; a Bash child started before the switch still has the old session id and is refused by the resolver."""
    alpha, gamma = world.seat("alpha"), world.seat("gamma")
    world.process(300, name="gamma", os_start=T1)
    world.hook(300, "startup", "g-old")
    world.client.bind(gamma, "end", 1)                                       # the conversation's old process exited cleanly
    world.procs.stop(300, keep_registry=False)
    world.process(100, name="alpha", session_id="a-sid")
    assert world.hook(100, "startup", "a-sid").identity == alpha

    out = world.hook(100, "resume", "g-old")
    assert (out.action, out.identity, out.generation, out.switched_from) == ("bound", gamma, 2, alpha)
    assert world.identity(alpha)["state"] == "unheld" and world.events(alpha)[-1] == "release"
    assert world.identity(gamma)["holder"]["pid"] == 100
    assert read_token_file(token_path(world.identity_dir, 100, 100_000)).identity == gamma
    assert out.handoff and out.token_file == token_path(world.identity_dir, 100, 100_000)

    with pytest.raises(IdentityConflictError) as err:
        resolve_identity(client=world.client, env={"CLAUDE_CODE_SESSION_ID": world.q("a-sid")}, claude_pid=100,
                         sessions_dir=world.procs.sessions_dir, identity_dir=world.identity_dir, host=world.host)
    assert {a.identity for a in err.value.answers} == {alpha, gamma}


def row_a_cross_seat_resume_of_a_seat_live_elsewhere_changes_nothing_and_prints_the_holder(world: World) -> None:
    """Row: ``resume`` in a process that already holds a different identity, the resumed seat live elsewhere:
    unchanged, the process keeps its identity, and the hook prints the holder."""
    alpha, delta = world.seat("alpha"), world.seat("delta")
    world.process(400, name="delta", os_start=T1)
    world.hook(400, "startup", "d-sid")
    world.process(100, name="alpha")
    world.hook(100, "startup", "a-sid")
    out = world.hook(100, "resume", "d-sid")
    assert (out.action, out.identity, out.generation) == ("unchanged", alpha, 1)
    assert any("held by pid 400" in n for n in out.notices)
    assert world.identity(alpha)["state"] == "live" and world.identity(delta)["holder"]["pid"] == 400


# ── clear, fork, compact ────────────────────────────────────────────────────


def row_clear_and_fork_keep_the_generation_and_a_lapsed_seat_is_revived(world: World) -> None:
    """Rows ``clear`` and ``fork``, live or lapsed in this process: the same generation, a new session id, event
    ``clear`` or ``fork``; a lapsed seat is revived. The index maps every id to the one generation."""
    alpha = world.seat("alpha")
    world.process(100, name="alpha")
    world.hook(100, "startup", "s1")
    world.lapse(alpha, seconds_ago=400)
    assert world.identity(alpha)["state"] == "lapsed"
    cleared = world.hook(100, "clear", "s2")
    forked = world.hook(100, "fork", "s3")
    assert (cleared.identity, cleared.generation, cleared.event) == (alpha, 1, "clear")
    assert (forked.identity, forked.generation, forked.event) == (alpha, 1, "fork")
    assert world.identity(alpha)["state"] == "live" and world.identity(alpha)["generation"] == 1
    assert world.events(alpha) == ["start", "lapse", "clear", "fork"]      # the engine's lazy lapse row, then ours
    for sid in ("s1", "s2", "s3"):
        assert (world.indexed(sid)["identity"], world.indexed(sid)["generation"]) == (alpha, 1), sid
    assert read_token_file(token_path(world.identity_dir, 100, 100_000)).session_id == world.q("s3")
    assert world.log(alpha)[2]["prior_session_id"] == world.q("s1")


def row_compact_changes_nothing_and_appends_no_row(world: World) -> None:
    """Row: ``compact``, any state: unchanged, no binding row."""
    world.process(100)
    first = world.hook(100, "startup", "sid-a")
    out = world.hook(100, "compact", "sid-a")
    assert out.action == "skipped" and out.row == "compact"
    assert world.events(first.identity) == ["start"]


# ── the in-place /restart rebind ────────────────────────────────────────────


def row_an_empty_in_place_restart_rebinds_the_same_identity_ended_included_and_skips_delegated_rows(world: World) -> None:
    """Row 1: ``startup`` after an in-place ``/restart`` with no exchange. The process keeps its pid and OS start time
    and gets a new ``startedAt``, a new session id and no ``-n`` name. The identity is found through the history of
    the process, whatever its state: ``ended`` (the old MCP server exited cleanly) or still ``live`` under the old
    ``startedAt`` (409 ``held`` unless ``holder_gone`` is sent). A delegated row for the same process is skipped; an
    identity another process holds now is skipped for the next."""
    ended, live, taken = world.seat("ended-seat"), world.seat("live-seat"), world.seat("taken-seat")
    for pid, name in ((500, "ended-seat"), (501, "live-seat")):
        world.process(pid, name=name, started_at=pid * 1000)
        assert world.hook(pid, "startup", f"old-{pid}").generation == 1
    world.client.bind(ended, "end", 1)                                  # a clean exit; the other never wrote `end`

    # a delegated child of the ended seat that names the same pid and start time must not be rebound
    world.client._request("POST", "/delegated", json={"parent": ended, "bind": evidence_body(
        session_id=world.q("child-sid"), host=world.host, pid=500, started_at=500_000, proc_start=T0)})

    world.registry(500, 888_000, T0, session_id="new-500")  # the in-place /restart: new startedAt, no name
    world.registry(501, 889_000, T0, session_id="new-501")
    rebound_ended = world.hook(500, "startup", "new-500")
    rebound_live = world.hook(501, "startup", "new-501")
    assert (rebound_ended.action, rebound_ended.identity, rebound_ended.generation, rebound_ended.row) == ("bound", ended, 2, "startup_rebind")
    assert (rebound_live.action, rebound_live.identity, rebound_live.generation) == ("bound", live, 2)
    assert world.log(live)[-1]["holder_gone"]["started_at"] == 501_000   # the old startedAt, found gone by the check
    assert world.identity(ended)["holder"]["started_at"] == 888_000

    # an identity that another process holds now is not taken back: pid 600 had `taken`, then pid 601 took it
    world.process(600, name="taken-seat", os_start=T1, started_at=600_000)
    world.hook(600, "startup", "old-600")
    world.procs.stop(600, keep_registry=False)
    world.process(601, os_start=T2, started_at=601_000)
    assert world.hook(601, "startup", "new-601", seat="taken-seat").identity == taken
    world.process(600, os_start=T1, started_at=777_000, session_id="x")   # pid 600 comes back, same OS start, new startedAt
    skipped = world.hook(600, "startup", "new-600")
    assert skipped.identity != taken and skipped.action == "ephemeral"


def row_a_rebind_considers_only_the_most_recent_identity_of_the_process(world: World) -> None:
    """The newest identity of the process is held by another process now; an older, ended identity of the same pid must
    not be rebound in its place (it belongs to an earlier life of the pid)."""
    older, newer = world.seat("older"), world.seat("newer")
    ev = lambda pid, started: evidence_body(session_id=world.q(f"p-{started}"), host=world.host, pid=pid, started_at=started, proc_start=T1)  # noqa: E731
    world.client.bind(older, "start", 0, ev(610, 610_000))
    world.client.bind(older, "end", 1)
    world.client.bind(newer, "start", 0, ev(610, 611_000))
    write_token_file(token_path(world.identity_dir, 610, 611_000), TokenFile(      # the file its hook would have written
        identity=newer, generation=1, token=None, expires_at=None, session_id=world.q("p-611000"), pid=610, started_at=611_000,
        proc_start=T1, host=world.host, registry_dir=str(world.procs.sessions_dir)))
    world.process(620, os_start=T2, name="newer")
    assert world.hook(620, "startup", "q-sid").identity == newer                # another process takes `newer`
    world.process(610, os_start=T1, started_at=612_000, session_id="p3")
    out = world.hook(610, "startup", "p3")
    assert out.action == "ephemeral" and out.identity not in (older, newer)
    assert world.identity(older)["state"] == "ended"


def row_a_data_credential_hook_forks_for_a_clear_keeps_its_seat_on_a_forbidden_switch_and_sweeps_quietly(world: World) -> None:
    """A mint-only install: the hook holds only the data credential, which may fork but not clear, release or report
    gone. /clear still moves the session id (as a fork row); a cross-seat /resume keeps the seat and says why; the sweep
    ends without raising."""
    from nexus.identity.sweep import sweep_host

    alpha, gamma, crashed = world.seat("alpha"), world.seat("gamma"), world.seat("crashed")
    world.process(300, name="gamma", os_start=T1, session_id="g-old")
    world.hook(300, "startup", "g-old")
    world.client.bind(gamma, "end", 1)
    world.procs.stop(300, keep_registry=False)
    world.process(400, name="crashed", os_start=T2, session_id="c-sid")
    world.hook(400, "startup", "c-sid")
    world.procs.stop(400, keep_registry=True)
    world.process(100, name="alpha", session_id="s1")
    world.hook(100, "startup", "s1")

    cleared = world.hook(100, "clear", "s2", client=world.data_client)
    assert (cleared.action, cleared.identity, cleared.generation, cleared.event) == ("bound", alpha, 1, "fork")
    assert world.events(alpha) == ["start", "fork"] and world.indexed("s2")["identity"] == alpha

    switched = world.hook(100, "resume", "g-old", client=world.data_client)
    assert (switched.action, switched.identity) == ("unchanged", alpha)
    assert any("may not release" in n for n in switched.notices)
    assert world.identity(alpha)["state"] == "live" and world.identity(gamma)["state"] == "ended"

    report = sweep_host(world.data_client, world.procs, host=world.host, own=(100, 100_000), identity_dir=world.identity_dir)
    assert report.forbidden and world.identity(crashed)["state"] == "live"


def row_a_resume_of_an_unindexed_id_in_a_process_that_holds_an_identity_reuses_it_or_closes_it_for_the_named_seat(world: World) -> None:
    """No second ephemeral beside the first: with no seat named the process keeps its identity and adds the session id
    (a fork row); naming another seat ends the ephemeral and binds the seat."""
    beta = world.seat("beta")
    world.process(100, session_id="sid-a")
    first = world.hook(100, "startup", "sid-a")
    reused = world.hook(100, "resume", "never-indexed")
    assert (reused.action, reused.identity, reused.generation, reused.row) == ("unchanged", first.identity, 1, "resume_unindexed_reuse")
    assert world.events(first.identity) == ["start", "fork"] and world.indexed("never-indexed")["identity"] == first.identity
    assert len(world.client.bindings_by_host(world.host, 100)) == 1

    world.process(101, os_start=T1, session_id="sid-b")
    second = world.hook(101, "startup", "sid-b")
    moved = world.hook(101, "resume", "also-unindexed", seat="beta")
    assert (moved.identity, moved.action, moved.switched_from) == (beta, "bound", second.identity)
    assert world.identity(second.identity)["state"] == "ended" and world.identity(beta)["holder"]["pid"] == 101


def row_a_cross_seat_switch_honours_the_other_host_reservation_like_a_start(world: World) -> None:
    """The resumed seat is held from another host and lapsed. Inside the 10-minute reservation the process keeps what it
    has; after it, the process switches."""
    alpha, gamma = world.seat("alpha"), world.seat("gamma")
    _held_by(world, gamma, 7, host=world.q("elsewhere"), sid="g-sid")
    world.process(100, name="alpha", session_id="a-sid")
    world.hook(100, "startup", "a-sid")
    world.lapse(gamma, seconds_ago=400)
    inside = world.hook(100, "resume", "g-sid")
    assert (inside.action, inside.identity) == ("unchanged", alpha) and world.identity(alpha)["state"] == "live"
    world.lapse(gamma, seconds_ago=1_000)
    after = world.hook(100, "resume", "g-sid")
    assert (after.action, after.identity, after.generation, after.switched_from) == ("bound", gamma, 2, alpha)
    assert world.identity(alpha)["state"] == "unheld"


def _under_name(world: World, name: str) -> str:
    """Make *world* name its machine *name* (a hostname the network later changed), returning the name it had."""
    before = world.host
    world.host = world.procs.host = name
    return before


def row_a_vanished_hostname_this_machine_bound_under_is_found_gone_and_a_stranger_is_not(world: World) -> None:
    """GitHub issue 1679. A binding written before the stable host key carries ``gethostname()``, a name the machine no
    longer has; the hook's own token file for the holder records it, which is the proof the row is this machine's. The
    holder's pid is dead, so a ``startup`` of the seat and a ``resume`` of the ephemeral both rebind the SAME identity and
    the real engine takes the verdict (its ``holder_gone`` names the host as the row records it). A row under the same
    name that nothing local wrote is another machine's: its live lease refuses the seat, though no such process exists here."""
    legacy, stranger = world.q("legacy-102"), world.q("stranger")
    alpha, beta, gamma = world.seat("alpha"), world.seat("beta"), world.seat("gamma")
    key = _under_name(world, legacy)
    world.process(100)
    world.hook(100, "startup", "sid-a", seat="alpha")
    world.process(110)
    ephemeral = world.hook(110, "startup", "sid-e")
    assert ephemeral.action == "ephemeral"
    _under_name(world, key)                                  # the network moved on; the machine's name is its key now
    world.procs.stop(100, keep_registry=True)
    world.procs.stop(110, keep_registry=True)
    _held_by(world, beta, 200, host=stranger)
    _held_by(world, gamma, 300, host=legacy)                 # the same name, and nothing local wrote it
    world.process(101, os_start=T1)
    mine = world.hook(101, "startup", "sid-b", seat="alpha")
    assert (mine.action, mine.identity, mine.generation) == ("bound", alpha, 2)
    row = world.log(alpha)[-1]
    assert row["event"] == "start" and row["holder_gone"]["host"] == legacy and row["holder_gone"]["pid"] == 100
    assert world.identity(alpha)["holder"]["host"] == key, "the new holder is recorded under the machine's key"
    world.process(111, os_start=T1)
    resumed = world.hook(111, "resume", "sid-e")
    assert (resumed.action, resumed.identity) == ("bound", ephemeral.identity), resumed.notices
    world.process(102, os_start=T2)
    assert world.hook(102, "startup", "sid-c", seat="beta").action == "ephemeral"
    world.process(103, os_start=T2)
    assert world.hook(103, "startup", "sid-d", seat="gamma").action == "ephemeral"
    assert world.identity(gamma)["holder"]["pid"] == 300 and world.identity(beta)["holder"]["pid"] == 200


def row_a_process_bound_before_the_host_key_keeps_its_identity_across_a_clear(world: World) -> None:
    """GitHub issue 1679, upgrade path: a Claude bound under a hostname the network has since changed is still running
    when the new client handles its ``/clear``. The engine compares ``host`` as a string on ``clear``, so the client speaks
    under the host the holder row records (proved by the hook's token file); sent under the key it would be 409
    ``holder_mismatch``, and the process would leave its identity for a new ephemeral."""
    legacy = world.q("legacy-102")
    world.process(100)
    key = _under_name(world, legacy)                          # the process bound under the old name
    first = world.hook(100, "startup", "sid-a")
    _under_name(world, key)                                   # the upgraded client now names the machine by its key
    cleared = world.hook(100, "clear", "sid-b")
    assert (cleared.action, cleared.row, cleared.identity, cleared.generation) == ("bound", "clear_in_process", first.identity, 1)
    assert world.identity(first.identity)["holder"]["host"] == legacy
    assert world.events(first.identity) == ["start", "clear"]
    assert read_token_file(token_path(world.identity_dir, 100, 100_000)).host == legacy
    forked = world.hook(100, "fork", "sid-c")                 # /branch: the same rule for a fork
    assert (forked.action, forked.row, forked.identity, forked.generation) == ("bound", "fork_in_process", first.identity, 1)
    assert world.events(first.identity) == ["start", "clear", "fork"] and world.identity(first.identity)["holder"]["host"] == legacy


# ── the table ───────────────────────────────────────────────────────────────

ROWS = (
    row_startup_with_no_seat_named_binds_an_ephemeral_identity,
    row_a_dash_n_name_that_is_no_seat_is_ephemeral_with_a_display_name_and_takes_no_alias,
    row_a_dash_n_that_matches_a_live_seat_binds_it_and_nx_seat_wins_over_it,
    row_nx_seat_naming_no_seat_is_ephemeral_and_says_so,
    row_nx_seat_naming_a_renamed_seat_is_ephemeral_and_the_notice_names_the_new_name,
    row_startup_naming_an_ended_seat_from_a_process_with_no_identity_binds_the_next_generation,
    row_startup_naming_a_seat_live_elsewhere_is_ephemeral_and_the_remedy_is_printed_only_interactively,
    row_startup_naming_a_lapsed_seat_whose_holder_still_runs_is_ephemeral,
    row_startup_naming_a_seat_whose_holder_is_gone_binds_the_next_generation_and_the_live_holder_needs_the_verdict,
    row_a_seat_held_from_another_host_stays_reserved_for_ten_minutes_after_the_lapse,
    row_each_restart_mode_finds_the_old_holder_gone_and_binds_the_next_generation_with_no_release_step,
    row_resume_of_an_unindexed_session_id_is_treated_as_startup_with_the_seat_the_launch_names,
    row_resume_of_an_ended_ephemeral_identity_is_treated_as_startup,
    row_resume_of_an_ended_delegated_identity_is_treated_as_startup_with_the_seat_the_launch_names,
    row_resume_of_an_ended_or_an_unheld_seat_in_a_process_with_no_identity_binds_it_at_the_next_generation,
    row_resume_of_a_seat_live_in_this_same_process_is_unchanged,
    row_resume_of_a_seat_live_in_another_process_is_ephemeral_with_the_holder,
    row_a_cross_seat_resume_switches_the_process_and_refuses_a_bash_child_from_before_the_switch,
    row_a_cross_seat_resume_of_a_seat_live_elsewhere_changes_nothing_and_prints_the_holder,
    row_clear_and_fork_keep_the_generation_and_a_lapsed_seat_is_revived,
    row_compact_changes_nothing_and_appends_no_row,
    row_an_empty_in_place_restart_rebinds_the_same_identity_ended_included_and_skips_delegated_rows,
    row_a_rebind_considers_only_the_most_recent_identity_of_the_process,
    row_a_data_credential_hook_forks_for_a_clear_keeps_its_seat_on_a_forbidden_switch_and_sweeps_quietly,
    row_a_resume_of_an_unindexed_id_in_a_process_that_holds_an_identity_reuses_it_or_closes_it_for_the_named_seat,
    row_a_cross_seat_switch_honours_the_other_host_reservation_like_a_start,
    row_a_vanished_hostname_this_machine_bound_under_is_found_gone_and_a_stranger_is_not,
    row_a_process_bound_before_the_host_key_keeps_its_identity_across_a_clear,
)
