# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx group`` (RDR-252 Step 4): form, set, disband, override, join, leave, list, ack-times.

A thin Click layer over :mod:`nexus.groups`. A group is an engine record, not a tuple: ``form`` makes the caller's
identity its leader, and every write is fenced by an identity token (the leader's for the record, the member's for a
membership). ``override`` is the one verb with no token: the tenant credential's, for a leader that cannot write.

Membership is read once, when an MCP server starts (RDR-252 "Load by membership"), so ``join`` and ``leave`` say that
the server must reconnect. A refusal prints the engine's code and reason and exits 1.
"""
from __future__ import annotations

import functools
import json
import statistics
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import click

from nexus import ci_reds, groups
from nexus.ci_reds import AckResolver
from nexus.groups import GROUP_NAME, GroupRecord, GroupsClient, GroupsError, GroupsUnavailableError, NoIdentityError

_RECONNECT = ("The MCP server reads group membership once, when it starts: reconnect it (/mcp) or restart the session "
              "for the auras that load by group to take effect.")
_PAGE = 300
_MAX_ROWS = 10_000


@dataclass
class GroupEnv:
    """What the verbs reach: the engine client, the caller's identity id (raises :class:`NoIdentityError` or returns a
    falsy value when this process holds none), and the tuple store ``ack-times`` reads. A test puts one in
    ``ctx.obj['group_env']``; the real one is built once per invocation."""

    client: GroupsClient
    caller: Callable[[], str | None]
    tuples: Callable[[], Any]
    #: The identity service's session route (``IdentityClient.session``: a session id in, ``{"identity": ...}`` out),
    #: which ``ack-times`` asks to map an ack's sender-session stamp to the identity the engine counts it for. None: no
    #: stamped ack can be resolved, and each is noted as unresolved.
    session_lookup: Callable[[str], Mapping[str, Any]] | None = None
    #: The identity record route (``IdentityClient.show``), read for the parent of a delegated identity: the engine counts
    #: a delegated child's ack for its parent, and the session route names only the child.
    identity_lookup: Callable[[str], Mapping[str, Any]] | None = None

    @classmethod
    def real(cls) -> GroupEnv:
        def tuples() -> Any:
            from nexus.db.t2.http_tuple_store import HttpTupleStore  # noqa: PLC0415 — deferred: CLI startup cost

            return HttpTupleStore()

        from nexus.tuple_directory import (  # noqa: PLC0415 — deferred: CLI startup cost
            identity_record_lookup, identity_session_lookup)

        return cls(client=GroupsClient.from_config(), caller=groups.own_identity_id, tuples=tuples,
                   session_lookup=identity_session_lookup(), identity_lookup=identity_record_lookup())


def _env(ctx: click.Context) -> GroupEnv:
    obj = ctx.ensure_object(dict)
    if "group_env" not in obj:
        try:
            obj["group_env"] = GroupEnv.real()
        except GroupsUnavailableError as exc:
            raise click.ClickException(f"no engine to talk to ({exc})") from exc
    return obj["group_env"]


def _caller(env: GroupEnv) -> str:
    who = env.caller()
    if not who:
        raise NoIdentityError("this process holds no identity: a group write needs the identity of its leader or member")
    return who


def _guarded(fn: Callable[..., None]) -> Callable[..., None]:
    """Turn every refusal of the engine, and of the identity layer, into one line and exit status 1."""
    @functools.wraps(fn)
    def run(*args: Any, **kwargs: Any) -> None:
        # Imported when a verb runs, not when the module loads: the resolver is only needed to name its refusals.
        from nexus.identity.resolver import IdentityConflictError  # noqa: PLC0415
        from nexus.identity.token_header import GenerationSupersededError, IdentityNotHeldError  # noqa: PLC0415

        try:
            fn(*args, **kwargs)
        except GroupsUnavailableError as exc:
            raise click.ClickException(f"the engine is not answering ({exc})") from exc
        except (GroupsError, NoIdentityError, GenerationSupersededError, IdentityNotHeldError, IdentityConflictError) as exc:
            raise click.ClickException(str(exc)) from exc

    return run


def _check_name(ctx: click.Context, param: click.Parameter, value: str) -> str:
    if not GROUP_NAME.match(value):
        raise click.BadParameter(
            "a name starts with a letter or digit and holds letters, digits, '.', '_' and '-' (at most 64)")
    return value


def _check_topics(ctx: click.Context, param: click.Parameter, values: tuple[str, ...]) -> tuple[str, ...]:
    for topic in values:
        if not GROUP_NAME.match(topic) or "--" in topic:
            raise click.BadParameter(f"{topic!r} is not a base topic name (the part of a board topic before '--')")
    return values


def _deadlines(ctx: click.Context, param: click.Parameter, values: tuple[str, ...]) -> dict[str, int]:
    out: dict[str, int] = {}
    for pair in values:
        topic, sep, minutes = pair.partition("=")
        minutes = minutes.strip()
        if not sep or not topic or not (minutes.isascii() and minutes.isdigit()):
            raise click.BadParameter(f"expected TOPIC=MINUTES with whole minutes, got {pair!r}")
        out[topic] = int(minutes)
    return out


def _name() -> Callable[[Any], Any]:
    return click.argument("name", callback=_check_name)


def _deadline_for() -> Callable[[Any], Any]:
    return click.option("--ack-deadline-for", "deadline_for", multiple=True, callback=_deadlines, metavar="TOPIC=MINUTES",
                        help="Acknowledgement deadline in minutes for one board topic (its base name), 5 to 240. Repeatable.")


@click.group("group")
def group_group() -> None:
    """Groups: who answers a board topic (RDR-252). form, set, disband, override, join, leave, list, ack-times."""


@group_group.command("form")
@_name()
@click.option("--serves", "serves", multiple=True, required=True, callback=_check_topics, metavar="TOPIC",
              help="A board topic base name this group answers, for example nexus-develop. Repeatable. One live group serves a topic.")
@click.option("--purpose", default=None, help="What the group is for.")
@click.option("--ends", default=None, metavar="UTC|open", help="When the group ends, a UTC instant such as 2026-12-01T00:00:00Z, or open.")
@click.option("--ack-deadline", type=int, default=None, metavar="MINUTES", help="Default acknowledgement deadline, 5 to 240 (the engine's default is 20).")
@_deadline_for()
@click.option("--requested-by", default=None, help="Who asked for the group (default: the caller's identity).")
@click.pass_context
@_guarded
def form_cmd(ctx: click.Context, name: str, serves: tuple[str, ...], purpose: str | None, ends: str | None,
             ack_deadline: int | None, deadline_for: dict[str, int], requested_by: str | None) -> None:
    """Form the group NAME with the caller as its leader, and re-form a disbanded name.

    The caller must hold a live, leased seat: only that identity may lead. The leader's membership is written in the
    same call.
    """
    env = _env(ctx)
    leader = _caller(env)
    record = GroupRecord.from_wire(env.client.form(
        name, leader, serves=list(serves), purpose=purpose, ends=ends, ack_deadline=ack_deadline,
        ack_deadline_by_topic=deadline_for or None, requested_by=requested_by or leader))
    click.echo(f"formed {record.name}: leader {record.leader}, serves {', '.join(record.serves) or '(nothing)'}, "
               f"ack deadline {record.ack_deadline} min")


@group_group.command("set")
@_name()
@click.option("--leader", default=None, metavar="SEAT", help="Hand the group to this member: a seat name or an identity id.")
@click.option("--ack-deadline", type=int, default=None, metavar="MINUTES", help="Default acknowledgement deadline, 5 to 240.")
@_deadline_for()
@click.option("--ends", default=None, metavar="UTC|open", help="A UTC instant in the future, or open.")
@click.option("--serves", "serves", multiple=True, callback=_check_topics, metavar="TOPIC",
              help="Replace the topics this group answers. Repeatable.")
@click.pass_context
@_guarded
def set_cmd(ctx: click.Context, name: str, leader: str | None, ack_deadline: int | None, deadline_for: dict[str, int],
            ends: str | None, serves: tuple[str, ...]) -> None:
    """Change the group NAME in place; only the leader may. Sends only the fields given.

    A per-topic deadline is merged into the stored ones (the engine replaces the whole map, so this reads the record
    first). Naming a new leader needs that member to be a live, leased seat.
    """
    if leader is None and ack_deadline is None and not deadline_for and ends is None and not serves:
        raise click.UsageError("nothing to set: give --leader, --ack-deadline, --ack-deadline-for, --ends or --serves")
    env = _env(ctx)
    fields: dict[str, Any] = {}
    if leader is not None:
        fields["leader"] = env.client.resolve(leader)
    if ack_deadline is not None:
        fields["ack_deadline"] = ack_deadline
    if deadline_for:
        stored = GroupRecord.from_wire(env.client.read(name)).ack_deadline_by_topic
        fields["ack_deadline_by_topic"] = {**stored, **deadline_for}
    if ends is not None:
        fields["ends"] = ends
    if serves:
        fields["serves"] = list(serves)
    record = GroupRecord.from_wire(env.client.set(name, **fields))
    click.echo(f"set {name}: {', '.join(sorted(fields))}; leader {record.leader}, serves "
               f"{', '.join(record.serves) or '(nothing)'}, ack deadline {record.ack_deadline} min")


@group_group.command("disband")
@_name()
@click.pass_context
@_guarded
def disband_cmd(ctx: click.Context, name: str) -> None:
    """End the group NAME now: every membership goes, the record stays as history. Only the leader may."""
    record = GroupRecord.from_wire(_env(ctx).client.disband(name))
    click.echo(f"disbanded {record.name} (ended_at {record.ended_at})")


@group_group.command("override")
@_name()
@click.option("--leader", default=None, metavar="SEAT", help="Name a new leader (a member that is a live, leased seat).")
@click.option("--disband", "disband", is_flag=True, help="End the group.")
@click.pass_context
@_guarded
def override_cmd(ctx: click.Context, name: str, leader: str | None, disband: bool) -> None:
    """Operator override: replace the leader of NAME, or end it, when its leader cannot write.

    Uses the tenant credential and no identity token. The engine allows it only while the leader is ended, unheld,
    lapsed with its holder gone, or live with an unleased binding, and refuses it while the leader is live. It is
    audit-logged.
    """
    if (leader is None) == (not disband):
        raise click.UsageError("give exactly one of --leader and --disband")
    env = _env(ctx)
    record = GroupRecord.from_wire(
        env.client.override(name, disband=True) if disband else env.client.override(name, leader=env.client.resolve(leader or "")))
    click.echo(f"overrode {record.name}: " + (f"ended_at {record.ended_at}" if disband else f"leader is now {record.leader}"))


@group_group.command("join")
@_name()
@click.pass_context
@_guarded
def join_cmd(ctx: click.Context, name: str) -> None:
    """Join the group NAME as a member. Only a seat joins; an ephemeral or delegated identity is refused.

    Membership is read when an MCP server starts, so reconnect the server afterwards.
    """
    env = _env(ctx)
    me = _caller(env)
    kind = env.client.identity(me).get("kind")
    if kind != "seat":
        raise click.ClickException(
            f"{me} is {kind}: only a seat joins a group. Launch the session with a seat (NX_SEAT, or -n naming one) "
            "or promote this identity with `nx identity promote`.")
    held = {m["group"]: m for m in env.client.memberships(me)}
    if name in held:
        click.echo(f"{me} is already a member of {name} (role {held[name].get('role')}); nothing changed")
        return
    env.client.join(me, name)
    click.echo(f"{me} joined {name} as member")
    click.echo(_RECONNECT)


@group_group.command("leave")
@_name()
@click.pass_context
@_guarded
def leave_cmd(ctx: click.Context, name: str) -> None:
    """Leave the group NAME. The leader hands the group on, or disbands it, first (the engine refuses otherwise)."""
    env = _env(ctx)
    me = _caller(env)
    if env.client.leave(me, name).get("removed"):
        click.echo(f"{me} left {name}")
        click.echo(_RECONNECT)
    else:
        click.echo(f"{me} is not a member of {name}; nothing to leave")


@group_group.command("list")
@_name()
@click.option("--json", "as_json", is_flag=True, help="Print the record and the members as JSON.")
@click.pass_context
@_guarded
def list_cmd(ctx: click.Context, name: str, as_json: bool) -> None:
    """Show the group NAME: its record, its leader and its members in join order, by alias, each with its state.

    State is the member's RDR-253 state: unheld, live, lapsed, ended, or unleased (a live binding whose MCP server
    cannot renew, so the engine cannot say whether its holder is alive).
    """
    client = _env(ctx).client
    raw = client.read(name)
    record = GroupRecord.from_wire(raw)
    members = []
    for row in client.roster(name):
        try:
            view = client.identity(row["identity"])
        except GroupsError:
            view = {}
        members.append({"identity": row["identity"], "name": view.get("name") or view.get("display_name"), "role": row.get("role"),
                        "state": groups.state_word(view) if view else "unknown", "joined_at": row.get("joined_at")})
    if as_json:
        click.echo(json.dumps({"group": raw, "members": members}, indent=2, sort_keys=True))
        return
    per_topic = ", ".join(f"{t} {m} min" for t, m in sorted(record.ack_deadline_by_topic.items()))
    live = record.live if record.live is not None else not record.ended_at   # no verdict from the engine: ended_at alone
    if live:
        status = "live"
    elif record.ended_at:
        status = f"disbanded (ended_at {record.ended_at})"
    else:
        status = f"disbanded (its ends, {record.ends}, has passed)"
    click.echo(f"{record.name}: {status}; serves {', '.join(record.serves) or '(nothing)'}; "
               f"ack deadline {record.ack_deadline} min" + (f" ({per_topic})" if per_topic else ""))
    click.echo(f"formed {record.formed_at}, ends {record.ends or 'open'}, requested by {record.requested_by or 'unknown'}")
    leader = next((m for m in members if m["identity"] == record.leader), None)
    click.echo(f"leader: {(leader or {}).get('name') or record.leader} ({record.leader})")
    click.echo("members:" if members else "members: (none)")
    for m in members:
        click.echo(f"  {m['name'] or m['identity']:<16} {m['identity']:<6} {m['role']:<7} {m['state']}")


# ── ack-times ───────────────────────────────────────────────────────────────


def _rows(store: Any, subspace: str, topic: str) -> tuple[list[Any], bool]:
    """The rows of *subspace*, oldest first, paged with the ``since`` cursor until an empty page (the engine may cap a
    read below ``_PAGE``, so a short page is not the last), at most ``_MAX_ROWS`` so a board read stays a bounded call.
    The flag is true when the ceiling stopped the read with rows still unread."""
    out: list[Any] = []
    since: tuple[str, str] | None = None
    while len(out) < _MAX_ROWS:
        page = store.rd(subspace, {"topic": topic}, n=min(_PAGE, _MAX_ROWS - len(out)), since=since)
        if not page:
            return out, False
        out.extend(page)
        since = (page[-1].created_at, page[-1].id)
    return out, bool(store.rd(subspace, {"topic": topic}, n=1, since=since))


def reds_from(rows: list[Any], resolver: AckResolver | None = None) -> list[dict[str, Any]]:
    """One entry per red of an ``--owner`` subspace, from its owner, clear and ack rows (:func:`nexus.ci_reds.split`
    says how rows make reds; a hand-off's ``reason`` ties it to the open red, not its new ``episode_at``).

    An ack counts as the engine counts it (RDR-252 "Acknowledgement"): inside the red, for the identity the latest owner
    row named as owner at the ack's time (an ack from one the red was handed away from does not, and none counts once
    the red is escalated with no owner), and for the sha of any red tip of the red. *Who* posted it is what the engine
    reads, the sender-session stamp mapped through the session index; ``dims.from`` is only a label and is never read, so
    an ack with no stamp counts for nobody (:meth:`nexus.ci_reds.AckResolver.poster`). The cheap
    checks, an owner and a tip, come before any lookup. ``minutes`` runs from the first row's ``episode_at`` to the
    first counting ack; a red with none has ``acked_at`` null. ``handoffs`` counts the hand-ons, the escalation
    included, as the engine's own counter does. ``ignored_acks`` counts the acks that pass those checks but carry no
    stamp, or one the index maps to nobody, up to the counting ack: the engine ignores them and so does ``minutes``.
    """
    resolver = resolver or AckResolver()
    reds, acks = ci_reds.split(rows)
    out: list[dict[str, Any]] = []
    for index, red in enumerate(reds):
        episode, cleared = red.rows, red.cleared
        start = episode[0][0]
        end = cleared if cleared is not None else (reds[index + 1].rows[0][0] if index + 1 < len(reds) else None)
        # Red tips only: the sha of each owner row. A suspect an owner row names may be untested or green, and the engine
        # counts an ack of a red tip alone.
        tips = {(r.dims or {}).get("sha") for _, r, _ in episode}
        acked: tuple[datetime, Any] | None = None
        acked_alias: str | None = None
        acked_by: str | None = None
        ignored_before = resolver.ignored
        for at, ack in acks:
            if at < start or (end is not None and at >= end):
                continue
            current = [b for t, _, b in episode if t <= at][-1:]
            owner = current[0].get("owner") if current else None
            if not owner or (ack.dims or {}).get("sha") not in tips:
                continue
            poster = resolver.poster(ack)
            if poster == owner:
                acked = (at, ack)
                acked_alias = current[0].get("owner_alias")
                acked_by = poster
                break
        began = ci_reds.instant(red.key) or start
        first = episode[0]
        out.append({
            "sha": (first[1].dims or {}).get("sha"), "episode_at": red.key, "reason": first[2].get("reason"),
            "owner": episode[-1][2].get("owner"),
            # RDR-252 (engine 0.1.198): the alias the engine stamped beside the id; null from an older engine's row.
            "owner_alias": episode[-1][2].get("owner_alias"),
            "handoffs": sum(1 for _, _, b in episode[1:] if b.get("reason") in ci_reds.HAND_ON_REASONS and not b.get("refresh")),
            "acked_at": acked[1].created_at if acked else None, "acked_by": acked_by if acked else None,
            "acked_by_alias": acked_alias if acked else None,
            "minutes": round((acked[0] - began).total_seconds() / 60, 1) if acked else None,
            "cleared_at": cleared.isoformat().replace("+00:00", "Z") if cleared else None,
            # Acks on a red tip, inside the red, that the engine ignores (null or unmapped stamp), up to the counting ack.
            "ignored_acks": resolver.ignored - ignored_before,
        })
    return out


@group_group.command("ack-times")
@_name()
@click.option("--json", "as_json", is_flag=True, help="Print the reds as JSON.")
@click.pass_context
@_guarded
def ack_times_cmd(ctx: click.Context, name: str, as_json: bool) -> None:
    """Per red on the topics NAME serves: the sha, when its episode began, when the owner acknowledged, and the minutes.

    The Day 2 calibration read for the group's ``ack_deadline`` (RDR-252: review the default against the distribution
    after the first 20 reds or 30 days). It reads the ``--owner`` post of each served topic (``board/ci/<topic>--owner``)
    and counts an ack the way the engine does: the ack whose sender-session stamp maps, through the identity service's
    session index, to the identity that owned the red at that time, for a red tip (``from`` is never read, so an ack with
    no stamp, or one whose stamp maps to no identity, counts for nobody). A lookup that fails for a reason other than "not known" is noted on standard error.
    """
    env = _env(ctx)
    record = GroupRecord.from_wire(env.client.read(name))
    topics = list(record.serves)
    if not topics:
        raise click.ClickException(f"{name} serves no topic, so it has no owner post to read")
    store = env.tuples()
    resolver = AckResolver(env.session_lookup, env.identity_lookup)
    reds: list[dict[str, Any]] = []
    for topic in topics:
        subspace = f"board/ci/{topic}--owner"
        try:
            rows, truncated = _rows(store, subspace, f"{topic}--owner")
        except Exception as exc:  # noqa: BLE001 — CLI boundary: one line and exit 1
            raise click.ClickException(f"could not read {subspace}: {type(exc).__name__}: {exc}") from exc
        if truncated:
            click.echo(f"note: stopped at {len(rows)} rows of {subspace}; later reds are not shown", err=True)
        reds.extend({"topic": topic, **red} for red in reds_from(rows, resolver))
    if resolver.unresolved:
        n = resolver.unresolved
        click.echo(f"note: {n} ack{'s' if n != 1 else ''} could not be resolved; counts may be low", err=True)
    if resolver.ignored:
        n = resolver.ignored
        click.echo(f"note: {n} ack{'s' if n != 1 else ''} on red tips carried no session stamp the engine maps; "
                   f"{'they' if n != 1 else 'it'} did not count", err=True)
    if as_json:
        click.echo(json.dumps({"group": name, "reds": reds}, indent=2, sort_keys=True))
        return
    if not reds:
        click.echo(f"{name}: no reds on {', '.join(topics)}")
        return
    acked = [r["minutes"] for r in reds if r["minutes"] is not None]
    summary = f"{len(reds)} reds, {len(acked)} acknowledged" + (f", median {statistics.median(acked):.1f} min, longest {max(acked):.1f} min" if acked else "")
    click.echo(f"{name}: {summary} (deadline {record.ack_deadline} min)")
    for r in reds:
        ack = f"acked by {r['acked_by_alias'] or r['acked_by']} at {r['acked_at']}, {r['minutes']} min" if r["acked_at"] else "no ack"
        click.echo(f"  {str(r['sha'])[:12]}  episode {r['episode_at']}  {ack}" + (f"  ({r['handoffs']} hand-off)" if r["handoffs"] else ""))
