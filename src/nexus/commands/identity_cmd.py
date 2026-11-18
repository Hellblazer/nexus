# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx identity`` (RDR-253 Step 4, nexus-hczdo.1.5): seats, take-over, release, rename, promote, purge, liveness.

A thin Click layer over :mod:`nexus.identity.ops`, which holds the guards. The calls use the tenant credential ``nx``
already resolves; an engine that cannot be reached, or a refusal, is one line and exit status 1.
"""
from __future__ import annotations

import json
from collections.abc import Callable

import click

from nexus.identity.ops import IdentityCommandError, IdentityEnv


def _env(ctx: click.Context) -> IdentityEnv:
    """The env a test put in ``ctx.obj['identity_env']``, else the real one (built once per invocation)."""
    obj = ctx.ensure_object(dict)
    if "identity_env" not in obj:
        try:
            obj["identity_env"] = IdentityEnv.real()
        except IdentityCommandError as exc:
            raise click.ClickException(str(exc)) from exc
    return obj["identity_env"]


def _run(ctx: click.Context, fn: Callable[[IdentityEnv], list[str]]) -> None:
    try:
        for line in fn(_env(ctx)):
            click.echo(line)
    except IdentityCommandError as exc:
        raise click.ClickException(str(exc)) from exc


@click.group("identity")
def identity_group() -> None:
    """Session identity: seats, take-over, release, rename, promote, purge, liveness (RDR-253)."""


@identity_group.command("create")
@click.argument("name")
@click.option("--created-by", type=click.Choice(["sam", "restart"]), default="sam", show_default=True,
              help="Who made the seat; the restart script passes 'restart'.")
@click.pass_context
def create_cmd(ctx: click.Context, name: str, created_by: str) -> None:
    """Create the seat NAME: an identity that outlives sessions, unheld until a launch names it."""
    from nexus.identity import ops  # noqa: PLC0415

    _run(ctx, lambda env: ops.create(env, name, created_by))


@identity_group.command("show")
@click.argument("seat", required=False)
@click.option("--all", "show_all", is_flag=True, help="Every binding on this host, and the live Claude processes here that have none.")
@click.option("--session", "session_id", default=None, help="Show the identity a Claude session id belongs to.")
@click.option("--json", "as_json", is_flag=True, help="Print the engine's identity record as JSON.")
@click.pass_context
def show_cmd(ctx: click.Context, seat: str | None, show_all: bool, session_id: str | None, as_json: bool) -> None:
    """Show an identity: its alias, state, current binding and generation, liveness and recent binding rows.

    SEAT is a seat name or an id (i7); with neither it is the caller's own identity.
    """
    from nexus.identity import ops  # noqa: PLC0415

    if show_all and (seat or session_id or as_json):
        raise click.UsageError("--all takes no seat, --session or --json")
    if seat and session_id:
        raise click.UsageError("give a seat or --session, not both")
    if show_all:
        _run(ctx, ops.show_all_lines)
        return
    try:
        env = _env(ctx)
        view = ops.show_view(env, seat, session_id)
        if as_json:
            click.echo(json.dumps(view, indent=2, sort_keys=True))
        else:
            for line in ops.show_lines(env, view):
                click.echo(line)
    except IdentityCommandError as exc:
        raise click.ClickException(str(exc)) from exc


@identity_group.command("alive")
@click.argument("target", required=False)
@click.pass_context
def alive_cmd(ctx: click.Context, target: str | None) -> None:
    """Run the holder check and report gone holders to the engine.

    TARGET is a seat name, an id or a pid on this host; with none, every binding on this host is checked as the
    SessionStart sweep does, skipping the caller's own and judging a delegated identity by its dispatcher process.
    """
    from nexus.identity import ops  # noqa: PLC0415

    _run(ctx, lambda env: ops.alive_lines(env, target))


@identity_group.command("claim")
@click.argument("seat")
@click.option("--take-over", "take_over", is_flag=True, required=True,
              help="Supersede the seat's current holder with this Claude session.")
@click.pass_context
def claim_cmd(ctx: click.Context, seat: str, take_over: bool) -> None:
    """Take SEAT over from its holder: bind this Claude session to it as a new generation.

    Runs only from an interactive Claude session (not a claude -p child), never for a seat held by an ancestor of
    this process, and never from a plain shell. The identity this session held before is closed: an ephemeral ends,
    a seat is released.
    """
    from nexus.identity import ops  # noqa: PLC0415

    _run(ctx, lambda env: ops.claim(env, seat))


@identity_group.command("release")
@click.argument("seat")
@click.pass_context
def release_cmd(ctx: click.Context, seat: str) -> None:
    """Free SEAT whose holder is gone. Runs from a plain shell and binds nothing.

    On the holder's host it is refused while the holder runs; for a holder on another host it is refused while the
    lease is live.
    """
    from nexus.identity import ops  # noqa: PLC0415

    _run(ctx, lambda env: ops.release(env, seat))


@identity_group.command("rename")
@click.argument("old")
@click.argument("new")
@click.pass_context
def rename_cmd(ctx: click.Context, old: str, new: str) -> None:
    """Rename seat OLD (a name or an id) to NEW. The old name keeps resolving for 7 days, then is refused.

    The seat's threads entry is retitled in the same call. Claude Code's /rename changes only the display name.
    """
    from nexus.identity import ops  # noqa: PLC0415

    _run(ctx, lambda env: ops.rename(env, old, new))


@identity_group.command("promote")
@click.argument("identity")
@click.argument("name")
@click.pass_context
def promote_cmd(ctx: click.Context, identity: str, name: str) -> None:
    """Make the ephemeral identity IDENTITY a seat named NAME (an id-shaped name, i<digits>, is refused)."""
    from nexus.identity import ops  # noqa: PLC0415

    _run(ctx, lambda env: ops.promote(env, identity, name))


@identity_group.command("purge")
@click.argument("identity")
@click.option("--operator", "operator", is_flag=True, required=True,
              help="Confirm the operator override: remove the identity's fenced state.")
@click.pass_context
def purge_cmd(ctx: click.Context, identity: str, operator: bool) -> None:
    """Remove the fenced state of IDENTITY (its board entry, mailbox, scratch, memberships).

    Refused for a live or unheld identity; allowed when it ended, or lapsed with its holder recorded gone. The engine
    audit-logs it.
    """
    from nexus.identity import ops  # noqa: PLC0415

    _run(ctx, lambda env: ops.purge(env, identity))

