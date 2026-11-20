# SPDX-License-Identifier: AGPL-3.0-or-later
"""The engine-backed rows of ``nx group`` and ``nexus.groups`` (RDR-252 Step 4 part B1, nexus-igea9.7).

Each ``row_*`` takes a scoped :class:`~tests.identity.conftest.World` on the one tenant of
tests/identity/test_identity_engine_journeys.py and drives the REAL engine through the verbs. This module holds no test
and mints no tenant (the tenant ceiling, tests/storage_touch_ceiling.toml, is Sam's); one ``_journey`` entry there runs
``ROWS`` (tag ``grp``). Run only these: ``NX_ROWS=group_journeys uv run pytest tests/identity/test_identity_engine_journeys.py``.
The tenant-free tests of the same behaviour, against canned engine answers, are tests/test_group_cmd.py and, for ack-times, tests/test_group_ack_times.py.
"""
from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from click.testing import CliRunner

from nexus import groups
from nexus.commands.group_cmd import GroupEnv, group_group
from nexus.db.t2.http_tuple_store import HttpTupleStore
from nexus.groups import GroupsClient, GroupsError
from nexus.identity import token_contract, token_header
from nexus.identity.binder import NX_IDENTITY_TOKEN_ENV, SessionBinder
from nexus.identity.token_file import read_token_file, token_path
from tests.identity.conftest import T0, World

# Group names and topic names carry the row's tag (``world.q``): groups, and the one-live-group-per-topic rule, are per
# tenant and the rows share one.


@dataclass
class Actor:
    """One bound Claude process of a row: its identity, generation, and a client that sends ITS token (so a write by
    another actor is refused as the engine refuses it)."""

    identity: str
    generation: int
    client: GroupsClient
    env: GroupEnv
    #: The process's identity token file, which a write posted AS this actor sends (see :func:`_as_actor`).
    token_file: Path | None = None


def _operator(world: World, stack: contextlib.ExitStack) -> Actor:
    """The tenant credential with no identity: what ``nx group override`` runs as."""
    client = GroupsClient(world.base_url, os.environ["NX_SERVICE_TOKEN"], identity_headers=lambda: {})
    stack.callback(client.close)
    return Actor("", 0, client, GroupEnv(client=client, caller=lambda: None, tuples=lambda: None))


def _bind_as_session(world: World, pid: int, name: str, session_id: str) -> Any:
    """``world.process`` then ``world.hook``, but under *session_id* as written: the world qualifies a session id with its
    row's tag, and ``nx group ack-times`` asks the session route only about a UUID (a Claude session id)."""
    world.procs.start(pid, T0, name=world.q(name), entrypoint="cli", session_id=session_id, registry=True)
    binder = SessionBinder(client=world.client, procs=world.procs, identity_dir=world.identity_dir, allow_privileged=True,
                           clock=lambda: world.now, sleep=lambda s: None)
    world.procs.set_session(pid, session_id)
    return binder.bind(world.facts(pid, "startup", session_id), env=None)


def _actor(world: World, stack: contextlib.ExitStack, name: str | None, pid: int, *, entrypoint: str = "cli",
           tuples: Any = None, uuid_session: bool = False) -> Actor:
    """A process holding a seat *name* (or, with None, an ephemeral identity), bound by the hook, with its token file.
    *uuid_session* binds it under a UUID session id (a real Claude session id) that the engine's session index maps to it."""
    world.now = time.time()                   # the hook stamps the file's expiry from this clock; the engine's tokens live a real hour
    if name:
        world.seat(name)
    if uuid_session:
        out = _bind_as_session(world, pid, name or "", str(uuid.uuid5(uuid.NAMESPACE_URL, f"{world.tag}/{pid}")))
    else:
        world.process(pid, name=name or "", entrypoint=entrypoint, session_id=f"s{pid}")
        out = world.hook(pid, "startup", f"s{pid}")
    path = token_path(world.identity_dir, pid, pid * 1000)
    assert read_token_file(path).token, "the engine issued no identity token"
    client = GroupsClient(world.base_url, os.environ["NX_SERVICE_TOKEN"],
                          identity_headers=lambda: token_contract.headers(read_token_file(path).token),
                          retry_headers=lambda sent, p: None)
    stack.callback(client.close)
    return Actor(out.identity, out.generation, client,
                 GroupEnv(client=client, caller=lambda: out.identity, tuples=lambda: tuples), path)


def _cli(actor: Actor, *args: str) -> Any:
    return CliRunner().invoke(group_group, list(args), obj={"group_env": actor.env})


def row_form_set_and_disband_round_trip_and_a_disbanded_name_is_formed_again(world: World) -> None:
    with contextlib.ExitStack() as stack:
        alpha, beta = _actor(world, stack, "alpha", 100), _actor(world, stack, "beta", 101)
        name, topic = world.q("train"), world.q("topic")
        assert groups.read_group(alpha.client, name) is None, "a name with no record reads as no record"

        formed = _cli(alpha, "form", name, "--serves", topic, "--ack-deadline", "20", "--ack-deadline-for", f"{topic}=30", "--purpose", "order")
        assert formed.exit_code == 0, formed.output
        record = groups.read_group(alpha.client, name)
        assert record is not None and record.live and record.ended_at is None
        assert (record.leader, record.serves, record.ack_deadline, record.ack_deadline_by_topic, record.purpose) == (
            alpha.identity, (topic,), 20, {topic: 30}, "order")
        assert record.requested_by == alpha.identity
        assert [m["identity"] for m in alpha.client.roster(name)] == [alpha.identity], "the leader's membership is written with the record"

        second = _cli(alpha, "form", world.q("train2"), "--serves", topic)
        assert second.exit_code == 1 and "topic_served" in second.output, second.output
        assert _cli(alpha, "form", name, "--serves", world.q("other")).exit_code == 1, "a live name is not formed over"

        not_leader = _cli(beta, "set", name, "--ack-deadline", "40")
        assert not_leader.exit_code == 1 and "not_owner" in not_leader.output, not_leader.output

        assert _cli(alpha, "set", name, "--ack-deadline", "25", "--ack-deadline-for", f"{topic}=45").exit_code == 0
        record = groups.read_group(alpha.client, name)
        assert (record.ack_deadline, record.ack_deadline_by_topic) == (25, {topic: 45})
        assert _cli(alpha, "set", name, "--ends", "2099-01-01T00:00:00Z").exit_code == 0
        assert groups.read_group(alpha.client, name).ends.startswith("2099-01-01")
        assert _cli(alpha, "set", name, "--ends", "open").exit_code == 0
        assert groups.read_group(alpha.client, name).ends is None
        too_fast = _cli(alpha, "set", name, "--ack-deadline", "4")
        assert too_fast.exit_code == 1 and "bad_deadline" in too_fast.output
        with pytest.raises(GroupsError) as bad:
            alpha.client.form("bad name", alpha.identity, serves=[])
        assert (bad.value.status, bad.value.code) == (400, "bad_group")

        gone = _cli(alpha, "disband", name)
        assert gone.exit_code == 0 and "disbanded" in gone.output, gone.output
        record = groups.read_group(alpha.client, name)
        assert record is not None and not record.live and record.ended_at, "the record stays as history"
        assert alpha.client.roster(name) == [], "a disband removes every membership, the leader's included"
        listed = _cli(alpha, "list", name)
        assert listed.exit_code == 0 and "disbanded" in listed.output, listed.output

        again = _cli(alpha, "form", name, "--serves", topic)
        assert again.exit_code == 0, again.output
        record = groups.read_group(alpha.client, name)
        assert record.live and record.ended_at is None and record.leader == alpha.identity
        assert [m["identity"] for m in alpha.client.roster(name)] == [alpha.identity]


def row_override_is_refused_while_the_leader_is_live_and_hands_the_group_on_or_ends_it_when_the_leader_is_gone(world: World) -> None:
    with contextlib.ExitStack() as stack:
        alpha, beta, operator = _actor(world, stack, "alpha", 100), _actor(world, stack, "beta", 101), _operator(world, stack)
        name, topic = world.q("train"), world.q("topic")
        assert _cli(alpha, "form", name, "--serves", topic).exit_code == 0
        assert _cli(beta, "join", name).exit_code == 0

        live = _cli(operator, "override", name, "--leader", world.q("beta"))
        assert live.exit_code == 1 and "leader_not_overridable" in live.output, live.output
        assert groups.read_group(alpha.client, name).leader == alpha.identity

        world.client.bind(alpha.identity, "end", alpha.generation)               # the leader's seat is ended: it cannot write
        blocked = _cli(alpha, "set", name, "--ack-deadline", "30")
        assert blocked.exit_code == 1, "an ended leader's own token fences nothing"
        done = _cli(operator, "override", name, "--leader", world.q("beta"))
        assert done.exit_code == 0 and beta.identity in done.output, done.output
        assert groups.read_group(operator.client, name).leader == beta.identity

        world.client.bind(beta.identity, "end", beta.generation)
        ended = _cli(operator, "override", name, "--disband")
        assert ended.exit_code == 0, ended.output
        record = groups.read_group(operator.client, name)
        assert not record.live and record.ended_at
        assert operator.client.roster(name) == []


def row_join_leave_and_list_round_trip_a_seat_joins_an_ephemeral_is_refused_and_the_leader_cannot_leave(world: World) -> None:
    with contextlib.ExitStack() as stack:
        alpha, beta = _actor(world, stack, "alpha", 100), _actor(world, stack, "beta", 101)
        desk = _actor(world, stack, "desk", 102, entrypoint="claude-desktop")
        eph = _actor(world, stack, None, 103)
        name, topic = world.q("train"), world.q("topic")
        assert _cli(alpha, "form", name, "--serves", topic).exit_code == 0

        joined = _cli(beta, "join", name)
        assert joined.exit_code == 0 and "reconnect" in joined.output.lower(), joined.output
        again = _cli(beta, "join", name)
        assert again.exit_code == 0 and "already" in again.output and "member" in again.output, again.output
        assert _cli(desk, "join", name).exit_code == 0

        refused = _cli(eph, "join", name)
        assert refused.exit_code == 1 and "ephemeral" in refused.output, refused.output
        with pytest.raises(GroupsError) as engine:
            eph.client.join(eph.identity, name)
        assert engine.value.code == "seat_required", "the engine refuses it too, once the group has a record"
        with pytest.raises(GroupsError) as other:
            beta.client.join(desk.identity, name)
        assert other.value.status == 403, "a member's token is not another identity's"

        world.lapse(beta.identity)
        listing = _cli(alpha, "list", name)
        assert listing.exit_code == 0, listing.output
        lines = {line.split()[0]: line for line in listing.output.splitlines() if line.startswith("  ")}
        a, b, d = (lines[world.q(n)] for n in ("alpha", "beta", "desk"))
        assert "leader" in a and "live" in a
        assert "member" in b and "lapsed" in b
        assert "unleased" in d, d
        assert list(lines) == [world.q("alpha"), world.q("beta"), world.q("desk")], "join order"
        data = json.loads(_cli(alpha, "list", name, "--json").stdout)
        assert [m["state"] for m in data["members"]] == ["live", "lapsed", "unleased"] and data["group"]["leader"] == alpha.identity

        leader_leaves = _cli(alpha, "leave", name)
        assert leader_leaves.exit_code == 1 and "leader_cannot_leave" in leader_leaves.output
        left = _cli(beta, "leave", name)
        assert left.exit_code == 0 and "reconnect" in left.output.lower(), left.output
        assert [m["group"] for m in beta.client.memberships(beta.identity)] == []
        assert [m["identity"] for m in alpha.client.roster(name)] == [alpha.identity, desk.identity]
        nothing = _cli(beta, "leave", name)
        assert nothing.exit_code == 0 and "not a member" in nothing.output


def row_the_real_token_resolution_writes_with_the_process_own_token_and_a_shell_with_none_is_refused(world: World) -> None:
    with pytest.MonkeyPatch.context() as monkeypatch:        # a row's patches end with the row; the journeys test resets token_header's caches after
        world.now = time.time()
        alpha = world.seat("alpha")
        world.process(100, name="alpha")
        world.hook(100, "startup", "s1")
        name, topic = world.q("train"), world.q("topic")
        monkeypatch.setattr(token_header, "_discover", lambda: token_path(world.identity_dir, 100, 100_000))
        token_header.reset_for_tests()
        client = GroupsClient(world.base_url, os.environ["NX_SERVICE_TOKEN"])          # the default token seams
        try:
            assert groups.own_identity_id() == alpha
            assert client.form(name, groups.own_identity_id(), serves=[topic])["leader"] == alpha
            client.disband(name)
            assert not groups.read_group(client, name).live

            monkeypatch.setattr(token_header, "_discover", lambda: None)                # a shell with no Claude ancestor
            token_header.reset_for_tests()
            with pytest.raises(groups.NoIdentityError):
                groups.own_identity_id()
            with pytest.raises(GroupsError) as bare:
                client.form(name, alpha, serves=[topic])
            assert (bare.value.status, bare.value.code) == (403, "identity_token_required")
        finally:
            client.close()


#: The stand-in sender an engine row is posted under before :func:`_stamp_engine_rows` makes it ``from=engine``.
_ENGINE_SEED = "engine-seed"


def _stamp_engine_rows(world: World, subspace: str) -> None:
    """Make the rows posted as ``_ENGINE_SEED`` on *subspace* read ``from=engine``, the way the engine's stamper leaves
    them (it writes through the repository, never the route, which refuses the dim to every client). The tuple id is
    the keys and the nonce, so the dims change leaves every row's id alone. Run as the cluster admin, which RLS does
    not bind."""
    import subprocess  # noqa: PLC0415 -- deferred: only this row seeds through SQL

    state = world.state
    sql = ("WITH stamped AS (UPDATE nexus.tuples SET dims = jsonb_set(dims, '{from}', '\"engine\"'), sender_session = NULL "
           f"WHERE subspace = '{subspace}' AND dims->>'from' = '{_ENGINE_SEED}' RETURNING 1) "
           "SELECT count(*) FROM stamped")
    proc = subprocess.run([str(state["pg_bin"] / "psql"), "-h", "127.0.0.1", "-p", str(state["pg_port"]),
                           "-U", state["pg_user"], "-d", "nexus_t2_substrate", "-v", "ON_ERROR_STOP=1", "-At", "-c", sql],
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"stamping the engine rows failed: {proc.stderr}"
    assert proc.stdout.strip() == "5", f"expected the seed's 5 engine rows, got {proc.stdout!r}"


@contextlib.contextmanager
def _as_actor(token_file: Path) -> Any:
    """Writes made inside send *token_file*'s identity token, as a process holding it does (``NX_IDENTITY_TOKEN``), so the
    engine stamps each row's ``sender_session`` with that actor's session: the stamp ``ack-times`` counts."""
    with mock.patch.dict(os.environ, {NX_IDENTITY_TOKEN_ENV: str(token_file)}):
        token_header.reset_for_tests()
        try:
            yield
        finally:
            token_header.reset_for_tests()


def row_ack_times_counts_the_owners_ack_and_not_one_from_an_identity_the_red_was_handed_away_from(world: World) -> None:
    store = HttpTupleStore(base_url=world.base_url, tenant="default", _token=os.environ["NX_SERVICE_TOKEN"])
    with contextlib.ExitStack() as stack:
        alpha = _actor(world, stack, "alpha", 100, tuples=store, uuid_session=True)
        beta = _actor(world, stack, "beta", 101, tuples=store, uuid_session=True)
        name, topic = world.q("train"), world.q("topic")
        assert _cli(alpha, "form", name, "--serves", topic).exit_code == 0
        subspace, key = f"board/ci/{topic}--owner", f"{topic}--owner"

        def post(kind: str, sha: str, who: str, **body: Any) -> None:
            # a client write of from=engine on board/ci is refused (RDR-252, 403 reserved_dim): an engine row is
            # posted under a stand-in sender and stamped from=engine below, as the stamper's repository write leaves it
            sender = _ENGINE_SEED if who == "engine" else who
            store.out(subspace, {"topic": key}, {"from": sender, "kind": kind, "sha": sha}, json.dumps(body),
                      nonce=uuid.uuid4().hex)

        def ack(sha: str, actor: Actor) -> None:
            # the engine counts an ack by its sender-session stamp, so each ack is posted under its poster's own token:
            # the client signs `from` as that identity (nexus.ci_ack) and the engine stamps the poster's session
            assert actor.token_file is not None
            with _as_actor(actor.token_file):
                post("ack", sha, actor.identity)

        def minutes_ago(n: int) -> str:
            return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - n * 60))

        s1, s2, s3 = "1" * 40, "2" * 40, "3" * 40
        e1, e2, e3 = minutes_ago(12), minutes_ago(9), minutes_ago(3)
        post("owner", s1, "engine", owner=alpha.identity, next=beta.identity, reason="earliest_suspect", episode_at=e1, refresh=False, handoffs=0)
        ack(s1, alpha)
        post("clear", s1, "engine", episode_at=e1)
        post("owner", s2, "engine", owner=alpha.identity, next=beta.identity, reason="earliest_suspect", episode_at=e2, refresh=False, handoffs=0)
        # the engine's hand-on row carries a NEW episode_at (OwnerDecision.handOn passes in.now()); it is the same red
        post("owner", s2, "engine", owner=beta.identity, next=None, reason="handoff", episode_at=minutes_ago(5), refresh=False, handoffs=1)
        ack(s2, alpha)                                                   # the red was handed away from alpha: this does not count
        ack(s2, beta)
        post("owner", s3, "engine", owner=alpha.identity, next=beta.identity, reason="no_record", episode_at=e3, refresh=False, handoffs=0)
        # the engine's rule (nexus-igea9): an ack with no sender-session stamp counts for nobody, whatever its `from` says.
        # Posted by a credential holding no session (an absent token file), signed as the owner by hand.
        with mock.patch("nexus.ci_ack.counted_identity", return_value=alpha.identity), _as_actor(world.tmp / "no-such-token"):
            post("ack", s3, alpha.identity)

        _stamp_engine_rows(world, subspace)

        # the identity service's session and record routes, as `nx group` builds them from the host's config
        reader = dataclasses.replace(alpha, env=dataclasses.replace(alpha.env, session_lookup=world.client.session,
                                                                    identity_lookup=world.client.show))
        result = _cli(reader, "ack-times", name, "--json")
        assert result.exit_code == 0, result.output
        assert "could not be resolved" not in result.stderr, result.stderr
        reds = json.loads(result.stdout)["reds"]
        assert [(r["sha"], r["acked_by"], r["handoffs"]) for r in reds] == [(s1, alpha.identity, 0), (s2, beta.identity, 1), (s3, None, 0)]
        assert [r["ignored_acks"] for r in reds] == [0, 0, 1], "only the s3 ack, which carried no session stamp, is ignored"
        assert "1 ack on red tips carried no session stamp the engine maps; it did not count" in result.stderr, result.stderr
        assert 11.9 <= reds[0]["minutes"] <= 12.6 and 8.9 <= reds[1]["minutes"] <= 9.6 and reds[2]["minutes"] is None, reds
        assert reds[0]["cleared_at"] and reds[1]["cleared_at"] is None
        text = _cli(reader, "ack-times", name)
        assert text.exit_code == 0 and "3 reds, 2 acknowledged" in text.output and "no ack" in text.output, text.output


ROWS = (
    row_form_set_and_disband_round_trip_and_a_disbanded_name_is_formed_again,
    row_override_is_refused_while_the_leader_is_live_and_hands_the_group_on_or_ends_it_when_the_leader_is_gone,
    row_join_leave_and_list_round_trip_a_seat_joins_an_ephemeral_is_refused_and_the_leader_cannot_leave,
    row_the_real_token_resolution_writes_with_the_process_own_token_and_a_shell_with_none_is_refused,
    row_ack_times_counts_the_owners_ack_and_not_one_from_an_identity_the_red_was_handed_away_from,
)
