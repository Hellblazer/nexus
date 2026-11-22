# SPDX-License-Identifier: AGPL-3.0-or-later
"""The owner paths RDR-252 never saw live, on a LOCAL THROWAWAY ENGINE (nexus-igea9, post-close audit lane A5).

What this is and is not. The engine is the real service jar on a throwaway tenant of a local Postgres: its stamper, decision
and deadline timer are the production code. Everything around it is staged: the ``--push`` rows are posted by hand through the
tuple client (no push script wrote them), the ``--done`` rows are posted by hand (no CI adapter and no GitHub run), time is
moved by backdating a column (no real clock passed), and the stamper fault is a database trigger. None of this is
production, the cloud engine or a live red. The live check is the first real occurrence of each path on the board; until one
happens, a green row here says the decision and the stamper write what the Java decision says, nothing about the adapter, the
script or the clock.

Every first owner row of the 14 live reds was ``earliest_suspect``; ``no_record``, ``chain_gap``, ``owner_not_live``,
``owner_unleased``, ``stamper_error`` and ``escalated`` rested on Java unit tests that hand the decision its rows (and
``owner_not_member``, the seventh path, too). Each ``row_*``
here forms a real group serving a topic, posts push records on ``board/ci/<topic>--push`` as the pushing sessions do
(each under its own identity token, so the engine stamps the session) and verdicts on ``--done`` as the CI adapter does
(no identity), lets the engine's stamper and deadline timer write, and reads the rows on ``--owner``.

This module holds no test and mints no tenant (the tenant ceiling, tests/storage_touch_ceiling.toml, is Sam's); one
``_journey`` entry in tests/identity/test_identity_engine_journeys.py runs ``ROWS`` (tag ``own``) on its one tenant. Run only
these: ``NX_ROWS=owner_path_journeys uv run pytest tests/identity/test_identity_engine_journeys.py``.

Two things a real engine cannot be told, and what stands in for each (same device as ``World.lapse``):

* **Time.** The engine reads ``Clock.systemUTC()`` with no seam, and the shortest acknowledgement deadline a group allows is
  5 minutes (``GroupInput.MIN_DEADLINE``). The ``escalated`` row backdates the open owner row's ``episode_at`` in PG, then
  posts a rerun red for the same tip (a new ``--done`` row, as a CI rerun is); the engine's own evaluation finds the
  deadline past, arms the timer at once and its timer thread posts the hand-off and then the escalation.
* **A fault in the stamper.** The decision is injectable only from Java (a package-private constructor). The
  ``stamper_error`` row makes the real evaluation throw by installing a BEFORE INSERT trigger that refuses the one row the
  evaluation would post (``earliest_suspect``); the engine's own catch then posts the leader with ``stamper_error``. The
  install drops any trigger a crashed earlier run left (``CREATE OR REPLACE``), and runs inside the ``try`` whose
  ``finally`` drops it again.

The last row, ``check_hold_and_record_push``, is the one that runs the client commands (``nx ci check-hold``,
``nx ci record-push``) against the engine, with an identity token per caller and an open red on the board.

The stamper runs after the ``--done`` write's response has gone out, and the timer on its own thread, so every read of
``--owner`` polls (bounded) for the rows it expects instead of reading once.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from nexus.db.t2.http_tuple_store import HttpTupleStore
from tests import _tenant_pool
from tests.identity.conftest import World
from tests.identity.test_group_journeys import Actor, _actor, _as_actor, _cli

#: How long a read of ``--owner`` waits for the engine's own write (it runs after the ``--done`` response, or on the timer).
OWNER_WAIT_SECONDS = 20.0

_SAFE = re.compile(r"^[A-Za-z0-9._-]+$")


def _sha(n: int) -> str:
    return f"{n:x}".rjust(40, "0")


@dataclass
class Scene:
    """One row's group, topic and store. ``push`` and ``verdict`` post what the push script and the CI adapter post."""

    world: World
    store: HttpTupleStore
    group: str
    topic: str
    actors: dict[str, Actor]
    _nonce: int = 0

    @property
    def owner_subspace(self) -> str:
        return f"board/ci/{self.topic}--owner"

    def push(self, who: str, sha: str, base: str) -> None:
        """A push record on ``--push`` signed by *who*'s own token, as ``nx tuple out`` from the push script is."""
        actor = self.actors[who]
        assert actor.token_file is not None
        with _as_actor(actor.token_file):
            self.store.out(f"board/ci/{self.topic}--push", {"topic": f"{self.topic}--push"},
                           {"from": actor.identity, "kind": "push", "sha": sha}, json.dumps({"base": base, "n": 1}),
                           nonce=f"push-{sha}")

    def verdict(self, sha: str, outcome: str) -> None:
        """The CI adapter's ``--done`` row for *sha*: no identity token, ``from=github``, the result as dims and runs in the body.
        Every call is a new row (a rerun of the same tip is one)."""
        self._nonce += 1
        conclusion = "success" if outcome == "success" else "failure"
        with _as_actor(self.world.tmp / "no-such-token"):
            self.store.out(f"board/ci/{self.topic}--done", {"topic": f"{self.topic}--done"},
                           {"from": "github", "kind": "sha", "sha": sha, "outcome": outcome},
                           json.dumps({"state": "completed", "n": 1, "runs": [[1, 1, "CI", conclusion]]}),
                           nonce=f"done-{sha}-{self._nonce}")

    def state_rows(self) -> list[tuple[str, dict[str, Any], dict[str, str]]]:
        """The engine's own rows on ``--owner``, oldest first: (kind, body, dims). A row a client wrote is not state."""
        rows = self.store.rd(self.owner_subspace, {"topic": f"{self.topic}--owner"}, n=50)
        return [(r.dims["kind"], json.loads(r.body or "{}"), r.dims) for r in rows
                if r.dims.get("from") == "engine" and r.sender_session is None]

    def wait_for_rows(self, count: int) -> list[tuple[str, dict[str, Any], dict[str, str]]]:
        """Poll, bounded, until the engine has written *count* state rows; fail naming what is there instead."""
        stop = time.monotonic() + OWNER_WAIT_SECONDS
        while True:
            rows = self.state_rows()
            if len(rows) >= count:
                return rows
            if time.monotonic() > stop:
                raise AssertionError(f"expected {count} engine rows on {self.owner_subspace} within {OWNER_WAIT_SECONDS}s, "
                                     f"found {[(k, b.get('reason'), b.get('owner')) for k, b, _ in rows]}")
            time.sleep(0.2)

    def expect_row(self, row: tuple[str, dict[str, Any], dict[str, str]], *, sha: str, reason: str, owner: str,
                   next_up: str | None, suspects: list[str], handoffs: int = 0) -> None:
        """One owner row: its kind, tip, reason, who is woken (owner) and who is shown next, the suspects and the hand-offs."""
        kind, body, dims = row
        assert (kind, dims["sha"], dims["from"]) == ("owner", sha, "engine"), (kind, dims)
        assert body["reason"] == reason, body
        assert body["owner"] == owner and body["next"] == next_up, f"wake targets: {body}"
        assert body["suspects"] == suspects and body["suspects_total"] == len(suspects), body
        assert (body["handoffs"], body["refresh"]) == (handoffs, False), body
        by_id = {a.identity: n for n, a in self.actors.items()}
        assert body.get("owner_alias") == self.world.q(by_id[owner]), f"the owner's seat name is stamped beside its id: {body}"
        if next_up is not None:
            assert body.get("next_alias") == self.world.q(by_id[next_up]), body


@contextlib.contextmanager
def _scene(world: World, *, members: tuple[str, ...], desktop: tuple[str, ...] = (), outsiders: tuple[str, ...] = (),
           deadline: int = 20, pid: int = 100, uuid_sessions: bool = False) -> Iterator[Scene]:
    """A real group ``alpha`` leads, serving one topic, with *members* (seats) and *desktop* seats (unleased) joined, and
    *outsiders*: live leased seats that never join the group. *uuid_sessions* binds every seat under a UUID session id, as a
    real Claude session has: the client commands that map a push record's stamp to an identity (``nx ci check-hold``) ask the
    session index only about a UUID."""
    with contextlib.ExitStack() as stack:
        store = HttpTupleStore(base_url=world.base_url, tenant="default", _token=os.environ["NX_SERVICE_TOKEN"])
        actors = {"alpha": _actor(world, stack, "alpha", pid, tuples=store, uuid_session=uuid_sessions)}
        for i, name in enumerate(members, start=1):
            actors[name] = _actor(world, stack, name, pid + i, tuples=store, uuid_session=uuid_sessions)
        for j, name in enumerate(desktop, start=1):
            actors[name] = _actor(world, stack, name, pid + 50 + j, entrypoint="claude-desktop", tuples=store, uuid_session=uuid_sessions)
        for k, name in enumerate(outsiders, start=1):
            actors[name] = _actor(world, stack, name, pid + 80 + k, tuples=store, uuid_session=uuid_sessions)
        group, topic = world.q("train"), world.q("topic")
        formed = _cli(actors["alpha"], "form", group, "--serves", topic, "--ack-deadline", str(deadline))
        assert formed.exit_code == 0, formed.output
        for name in (*members, *desktop):
            joined = _cli(actors[name], "join", group)
            assert joined.exit_code == 0, joined.output
        yield Scene(world, store, group, topic, actors)


def _alpha(scene: Scene) -> str:
    return scene.actors["alpha"].identity


def row_no_record_a_red_tip_with_no_push_record_hands_the_red_to_the_leader_not_the_earliest_suspect(world: World) -> None:
    """Path ``no_record``: the earliest suspect P0 has a record (a live member, beta), the red tip P1 has none (a hand
    push, a record that failed to write: the two unrecorded tips of 2026-11-21). The rule does not trust the chain, so the
    owner is the leader, where ``earliest_suspect`` would have named beta."""
    with _scene(world, members=("beta",)) as s:
        p0, p1 = _sha(0x101), _sha(0x102)
        s.push("beta", p0, base=_sha(0x1))
        s.verdict(p1, "failure")
        rows = s.wait_for_rows(1)
        assert len(rows) == 1, rows
        s.expect_row(rows[0], sha=p1, reason="no_record", owner=_alpha(s), next_up=s.actors["beta"].identity, suspects=[p0, p1])


def row_chain_gap_a_break_in_the_push_chain_gives_the_red_to_the_leader(world: World) -> None:
    """Path ``chain_gap``: both tips are recorded by live members (beta, then gamma) but P1's ``base`` is no tip the chain
    holds (a push that landed while the lock was skipped, a back-merge), so the chain has a gap inside the suspects. The
    owner is the leader; next up is the later suspect's pusher."""
    with _scene(world, members=("beta", "gamma")) as s:
        p0, p1 = _sha(0x201), _sha(0x202)
        s.push("beta", p0, base=_sha(0x1))
        s.push("gamma", p1, base=_sha(0x2))                  # not p0, not any tip: a second segment, gapBefore
        s.verdict(p1, "failure")
        rows = s.wait_for_rows(1)
        assert len(rows) == 1, rows
        s.expect_row(rows[0], sha=p1, reason="chain_gap", owner=_alpha(s), next_up=s.actors["gamma"].identity, suspects=[p0, p1])


def row_owner_not_live_a_lapsed_earliest_suspect_hands_the_red_to_the_leader(world: World) -> None:
    """Path ``owner_not_live``: the earliest suspect's pusher (dozer) pushed, then its lease lapsed (300 s past its last
    renewal, staged by backdating it as ``World.lapse`` does), so the red goes to the live leader. The chain is contiguous."""
    with _scene(world, members=("dozer", "beta")) as s:
        p0, p1 = _sha(0x301), _sha(0x302)
        s.push("dozer", p0, base=_sha(0x1))
        s.push("beta", p1, base=p0)
        world.lapse(s.actors["dozer"].identity)
        s.verdict(p1, "failure")
        rows = s.wait_for_rows(1)
        assert len(rows) == 1, rows
        s.expect_row(rows[0], sha=p1, reason="owner_not_live", owner=_alpha(s), next_up=s.actors["beta"].identity, suspects=[p0, p1])


def row_owner_unleased_a_desktop_earliest_suspect_hands_the_red_to_the_leader(world: World) -> None:
    """Path ``owner_unleased``: the earliest suspect's pusher is a Claude Desktop seat, whose binding is unleased (its MCP
    server cannot renew it, so the engine cannot say it is alive), so the red goes to the leader."""
    with _scene(world, members=("beta",), desktop=("desk",)) as s:
        p0, p1 = _sha(0x401), _sha(0x402)
        s.push("desk", p0, base=_sha(0x1))
        s.push("beta", p1, base=p0)
        s.verdict(p1, "failure")
        rows = s.wait_for_rows(1)
        assert len(rows) == 1, rows
        s.expect_row(rows[0], sha=p1, reason="owner_unleased", owner=_alpha(s), next_up=s.actors["beta"].identity, suspects=[p0, p1])


def _fail_owner_row_trigger(world: World, subspace: str, *, install: bool) -> None:
    """Install (or drop) the fault: a BEFORE INSERT trigger on nexus.tuples that raises for an ``earliest_suspect`` row on
    *subspace*, the row the real evaluation would post. Run as the cluster admin; the trigger fires for the engine's role."""
    assert _SAFE.match(subspace.replace("/", "_")), subspace
    name = "nx_test_fail_owner_row"
    if install:
        # Idempotent: a trigger or function a crashed earlier run left behind is dropped first and the function replaced, so
        # a rerun never fails on "already exists" and never runs with a stale body.
        sql = (f"DROP TRIGGER IF EXISTS {name} ON nexus.tuples; "
               f"CREATE OR REPLACE FUNCTION public.{name}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
               f"IF NEW.subspace = '{subspace}' AND NEW.body LIKE '%earliest_suspect%' THEN "
               "RAISE EXCEPTION 'nx test fault: the owner row is refused'; END IF; RETURN NEW; END $$; "
               f"CREATE TRIGGER {name} BEFORE INSERT ON nexus.tuples FOR EACH ROW EXECUTE FUNCTION public.{name}();")
    else:
        sql = f"DROP TRIGGER IF EXISTS {name} ON nexus.tuples; DROP FUNCTION IF EXISTS public.{name}();"
    proc = _tenant_pool.psql(world.state, sql, ON_ERROR_STOP="1")
    assert proc.returncode == 0, f"{'installing' if install else 'dropping'} the fault failed: {proc.stderr} {proc.stdout}"


def row_stamper_error_an_evaluation_that_throws_posts_the_leader_with_no_next_up(world: World) -> None:
    """Path ``stamper_error``: a red whose evaluation throws (here: its one row is refused by the database) posts the leader
    with ``reason=stamper_error``, no suspects and no next up, so a stamper fault still names someone. The row the evaluation
    would have posted (``earliest_suspect``, owner beta) is absent."""
    with _scene(world, members=("beta",)) as s:
        p0 = _sha(0x501)
        s.push("beta", p0, base=_sha(0x1))
        try:
            _fail_owner_row_trigger(world, s.owner_subspace, install=True)   # inside the try: a half-installed fault is dropped too
            s.verdict(p0, "failure")
            rows = s.wait_for_rows(1)
        finally:
            _fail_owner_row_trigger(world, s.owner_subspace, install=False)
        assert [b["reason"] for _, b, _ in rows] == ["stamper_error"], "the refused earliest_suspect row must not exist: " + str(rows)
        s.expect_row(rows[0], sha=p0, reason="stamper_error", owner=_alpha(s), next_up=None, suspects=[])


def _backdate_open_owner_row(world: World, subspace: str, minutes: int) -> None:
    """Move the newest engine owner row's ``episode_at`` *minutes* into the past: the engine reads the column, not our clock."""
    proc = _tenant_pool.psql(
        world.state,
        "UPDATE nexus.tuples SET body = jsonb_set(body::jsonb, '{episode_at}', to_jsonb(to_char("
        "(now() - (:'mins' || ' minutes')::interval) AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')))::text "
        "WHERE id = (SELECT id FROM nexus.tuples WHERE tenant_id = :'tenant' AND subspace = :'sub' "
        "AND dims->>'from' = 'engine' AND dims->>'kind' = 'owner' ORDER BY created_at DESC, id DESC LIMIT 1) RETURNING id;",
        mins=str(minutes), tenant=world.tenant, sub=subspace, ON_ERROR_STOP="1")
    assert proc.returncode == 0 and proc.stdout.strip(), f"could not backdate the owner row: {proc.stderr} {proc.stdout}"


def row_escalated_an_unacknowledged_red_is_handed_on_once_and_then_escalated_by_the_deadline_timer(world: World) -> None:
    """Path ``escalated`` (with the ``handoff`` before it): beta owns the red, alpha (the leader) is next up, nobody
    acknowledges. At the first missed deadline the engine's timer hands the red to alpha (``handoff``, handoffs 1, no next
    up); at the second it posts ``escalated`` (handoffs 2) naming the live leader, and arms nothing more. The deadline is the
    group's 5-minute minimum, passed by backdating the open row (see the module docstring), never by waiting."""
    with _scene(world, members=("beta",), deadline=5) as s:
        beta, alpha = s.actors["beta"].identity, _alpha(s)
        p0 = _sha(0x601)
        s.push("beta", p0, base=_sha(0x1))
        s.verdict(p0, "failure")
        first = s.wait_for_rows(1)
        s.expect_row(first[0], sha=p0, reason="earliest_suspect", owner=beta, next_up=alpha, suspects=[p0])

        _backdate_open_owner_row(world, s.owner_subspace, 6)
        s.verdict(p0, "failure")                              # a CI rerun of the same tip: the evaluation finds the deadline past
        second = s.wait_for_rows(2)
        s.expect_row(second[1], sha=p0, reason="handoff", owner=alpha, next_up=None, suspects=[p0], handoffs=1)

        _backdate_open_owner_row(world, s.owner_subspace, 6)
        s.verdict(p0, "failure")
        third = s.wait_for_rows(3)
        s.expect_row(third[2], sha=p0, reason="escalated", owner=alpha, next_up=None, suspects=[p0], handoffs=2)
        assert [b["reason"] for _, b, _ in third] == ["earliest_suspect", "handoff", "escalated"], third
        # An escalated row runs no deadline: another rerun posts nothing more. An absence cannot be polled for, and a fixed
        # sleep proves nothing about a write that has not happened yet, so the check is ordered by something observable
        # instead: after the rerun the newest tip goes green, which the stamper answers with a ``clear`` row. The stamper
        # evaluates one topic at a time under its advisory lock (``BoardTopicLock``), so the clear row appearing is strong
        # evidence the rerun was evaluated before it; the owner rows are still the three and the clear is the fourth state
        # row, found within the bounded wait. (Evidence, not proof: the lock does not promise arrival order, so a rerun
        # evaluated after the green could still write late and go unseen here.)
        p1 = _sha(0x602)
        s.push("beta", p1, base=p0)
        s.verdict(p0, "failure")                              # the rerun of the escalated tip
        s.verdict(p1, "success")                              # the newest tip goes green
        last = s.wait_for_rows(4)
        assert [k for k, _, _ in last] == ["owner", "owner", "owner", "clear"], [(k, b.get("reason")) for k, b, _ in last]


def row_owner_not_member_an_earliest_suspect_who_is_not_in_the_group_hands_the_red_to_the_leader(world: World) -> None:
    """Path ``owner_not_member``: the earliest suspect's pusher (out) is a live, leased seat but never joined the group, so
    the group has no standing to make it the owner and the red goes to the leader. The chain is contiguous and recorded."""
    with _scene(world, members=("beta",), outsiders=("out",)) as s:
        p0, p1 = _sha(0x801), _sha(0x802)
        s.push("out", p0, base=_sha(0x1))
        s.push("beta", p1, base=p0)
        s.verdict(p1, "failure")
        rows = s.wait_for_rows(1)
        assert len(rows) == 1, rows
        s.expect_row(rows[0], sha=p1, reason="owner_not_member", owner=_alpha(s), next_up=s.actors["beta"].identity, suspects=[p0, p1])


def _command(who: Actor | None, world: World, *args: str) -> Any:
    """Run a real ``nx ci`` command as *who* (its identity token, so the engine stamps its session and the command reads its
    identity), or with no identity at all when *who* is None. The tuple store and the identity service are the ambient ones
    the world's engine serves (NX_SERVICE_URL / NX_SERVICE_TOKEN)."""
    from click.testing import CliRunner  # noqa: PLC0415

    from nexus.commands import ci_cmd  # noqa: PLC0415

    token = who.token_file if who is not None else world.tmp / "no-such-token"
    assert token is not None
    with _as_actor(token):
        return CliRunner().invoke(ci_cmd.ci_group, list(args))


def row_check_hold_and_record_push_commands_run_against_the_engine_with_an_open_red(world: World) -> None:
    """The client half on the real engine (review r1): with a red open (beta owns it; gamma pushed the later suspect; delta is a
    stranger), the real ``nx ci check-hold`` lets the owner and the suspect's pusher through, refuses the stranger and a caller
    with no identity with exit 13, and ``--red-fix`` overrides it; then the real ``nx ci record-push`` writes the stranger's
    push record with ``red_fix`` in its body, stamped with the stranger's own session, ``from`` its identity id. Nothing here is
    a fake store or a fake resolver: the commands read ``--owner`` and ``--push`` and the session index as they do in use."""
    with _scene(world, members=("beta", "gamma", "delta"), uuid_sessions=True) as s:
        beta, gamma, delta = s.actors["beta"], s.actors["gamma"], s.actors["delta"]
        p0, p1, p2 = _sha(0x901), _sha(0x902), _sha(0x903)
        s.push("beta", p0, base=_sha(0x1))
        s.push("gamma", p1, base=p0)
        s.verdict(p1, "failure")
        rows = s.wait_for_rows(1)
        s.expect_row(rows[0], sha=p1, reason="earliest_suspect", owner=beta.identity, next_up=gamma.identity, suspects=[p0, p1])
        hold = ("check-hold", "--base", s.topic)

        for who, label in ((beta, "the owner"), (gamma, "the pusher of a later suspect")):
            allowed = _command(who, world, *hold)
            assert allowed.exit_code == 0 and "UNCHECKED" not in allowed.output and "REFUSED" not in allowed.output, (label, allowed.output)

        refused = _command(delta, world, *hold)
        assert refused.exit_code == 13, refused.output
        first = refused.stdout.splitlines()[0]
        assert first.startswith("PUSH_REFUSED_DEVELOP_RED ") and f"red at {p1}" in first and beta.identity in first, first
        assert "unattributed" not in refused.stdout, "both suspects have attributable push records here"

        anonymous = _command(None, world, *hold)
        assert anonymous.exit_code == 13 and "holds no identity" in anonymous.stdout and "nx identity show" in anonymous.stdout, anonymous.output

        reason = "owner away; the fix for the red tip"
        overridden = _command(delta, world, *hold, "--red-fix", reason)
        assert overridden.exit_code == 0 and overridden.stdout.startswith("PUSH_RED_FIX_APPLIED "), overridden.output

        recorded = _command(delta, world, "record-push", "--base", s.topic, "--from-sha", p1, "--to-sha", p2, "--count", "1",
                            "--red-fix", reason)
        # the status line is the command's last: a write may log a guard warning to standard output before it (the throwaway engine's)
        status = recorded.stdout.strip().splitlines()[-1]
        assert recorded.exit_code == 0 and status.startswith("PUSH_RECORD_OK ") and status.endswith(" stamped"), recorded.output
        written = [r for r in s.store.rd(f"board/ci/{s.topic}--push", {"topic": f"{s.topic}--push"}, n=50) if r.dims.get("sha") == p2]
        assert len(written) == 1, written
        row = written[0]
        assert row.dims["kind"] == "push" and row.dims["from"] == delta.identity, row.dims
        assert json.loads(row.body) == {"base": p1, "n": 1, "red_fix": reason}, row.body
        assert row.sender_session is not None, "the engine stamped the stranger's own session on the record"


ROWS = (
    row_no_record_a_red_tip_with_no_push_record_hands_the_red_to_the_leader_not_the_earliest_suspect,
    row_chain_gap_a_break_in_the_push_chain_gives_the_red_to_the_leader,
    row_owner_not_live_a_lapsed_earliest_suspect_hands_the_red_to_the_leader,
    row_owner_unleased_a_desktop_earliest_suspect_hands_the_red_to_the_leader,
    row_stamper_error_an_evaluation_that_throws_posts_the_leader_with_no_next_up,
    row_escalated_an_unacknowledged_red_is_handed_on_once_and_then_escalated_by_the_deadline_timer,
    row_owner_not_member_an_earliest_suspect_who_is_not_in_the_group_hands_the_red_to_the_leader,
    row_check_hold_and_record_push_commands_run_against_the_engine_with_an_open_red,
)
