# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx identity`` (RDR-253 Step 4, nexus-hczdo.1.5): seats, take-over, release, rename, promote, purge, liveness.

A thin Click layer over :mod:`nexus.identity.ops`, which holds the guards. The calls use the tenant credential ``nx``
already resolves; an engine that cannot be reached, or a refusal, is one line and exit status 1.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import click

from nexus.identity.ops import IdentityCommandError, IdentityEnv
from nexus.identity.work import EXIT_OWNED, OwnedByLiveIdentity


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
    except OwnedByLiveIdentity as exc:
        raise _OwnedExit(str(exc)) from exc
    except IdentityCommandError as exc:
        raise click.ClickException(str(exc)) from exc


class _OwnedExit(click.ClickException):
    """``may-stop``'s own exit status for a resource a live identity owns, so a script keys on it, never on the text."""

    exit_code = EXIT_OWNED


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
    a seat is released. A seat nobody holds is started rather than taken over, which a data-scoped credential (a
    Claude Desktop session's) may do; taking over a held seat needs the tenant credential.
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
@click.option("--token-file", "token_file", type=click.Path(path_type=Path, dir_okay=False), default=None,
              help="Cloud mode: the identity token file of the session that holds IDENTITY, for a caller that is not "
                   "that session (the restart script). Ignored under a tenant credential.")
@click.argument("identity")
@click.argument("name")
@click.pass_context
def promote_cmd(ctx: click.Context, token_file: Path | None, identity: str, name: str) -> None:
    """Make the ephemeral identity IDENTITY a seat named NAME (an id-shaped name, i<digits>, is refused).

    Promote also moves the identity's threads board entry to the title NAME. From engine 0.1.198 the engine does that
    itself, in the promote's own transaction, so it works from any shell and the restart script, and an entry already
    titled NAME is never overwritten (the promote still succeeds and the entry keeps its title). Against an older
    engine the retitle is best effort and only works inside the session that owns the entry (the engine lets only that
    session write it); run anywhere else the promote still succeeds and the entry keeps its old title, and readers
    still find it by its identity: line.
    """
    from nexus.identity import ops  # noqa: PLC0415

    _run(ctx, lambda env: ops.promote(env, identity, name, token_file))


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



_KINDS = ["worktree", "tmux", "watcher", "wait", "labrun", "agent"]
_KIND = click.option("--kind", required=True, type=click.Choice(_KINDS), help="The kind of work or resource.")
_REF = click.option("--ref", required=True,
                    help="Its label: an absolute worktree path, a tmux -L socket name, or the work's own name.")


@identity_group.command("may-stop")
@_KIND
@_REF
@click.pass_context
def may_stop_cmd(ctx: click.Context, kind: str, ref: str) -> None:
    """Exit 0 when no live identity other than this process owns the resource; else refuse, naming the owner.

    Run it before removing a worktree or killing a tmux server. Ownership is read from the work rows
    (`nx identity work register`) and liveness from the holder check, never from a name pattern or a process listing.
    A resource no row names is unowned and the answer is yes.

    Exit status: 0 no live owner; 3 a live identity owns it; 1 anything else (the engine does not answer, a bad
    argument). A worktree's REF is resolved to its real path. Stop on any nonzero status.
    """
    from nexus.identity import work  # noqa: PLC0415

    _run(ctx, lambda env: work.may_stop_lines(env, kind, ref))


@identity_group.group("work")
def work_group() -> None:
    """Work this session owns: worktrees, tmux servers, waits, watchers, lab runs and dispatched agents (RDR-253)."""


@work_group.command("register")
@_KIND
@_REF
@click.pass_context
def work_register_cmd(ctx: click.Context, kind: str, ref: str) -> None:
    """Register work this session owns, in state running. A process with no identity registers nothing (exit 0)."""
    from nexus.identity import work  # noqa: PLC0415

    _run(ctx, lambda env: work.register_lines(env, kind, ref, work.RUNNING))


_OWNER = click.option(
    "--identity", "owner", default=None,
    help="The identity that registered the row (the id `register` printed), for a process that has lost its session: "
         "a lab-remote whose shell ended before the run did. Default: the caller's own.")


def _owner_of(owner: str | None) -> Callable[[], str | None] | None:
    """The ``who`` for ``--identity``: the named identity, with one stderr line when it is not the caller's own (a wrong id
    ends another session's work, and nothing in the engine refuses it)."""
    if not owner:
        return None
    from nexus.identity import work  # noqa: PLC0415

    mine = work.caller_identity()
    if mine is not None and mine != owner:
        click.echo(f"note: --identity {owner} is not this process's identity ({mine})", err=True)
    return lambda: owner


@work_group.command("done")
@_KIND
@_REF
@_OWNER
@click.pass_context
def work_done_cmd(ctx: click.Context, kind: str, ref: str, owner: str | None) -> None:
    """Mark work this session owns finished (state done)."""
    from nexus.identity import work  # noqa: PLC0415

    _run(ctx, lambda env: work.register_lines(env, kind, ref, work.DONE, who=_owner_of(owner)))


@work_group.command("forget")
@_KIND
@_REF
@_OWNER
@click.pass_context
def work_forget_cmd(ctx: click.Context, kind: str, ref: str, owner: str | None) -> None:
    """Drop a work row of this session."""
    from nexus.identity import work  # noqa: PLC0415

    _run(ctx, lambda env: work.forget_lines(env, kind, ref, who=_owner_of(owner)))


@work_group.command("list")
@click.argument("seat", required=False)
@click.option("--session", "session_id", default=None, help="List the work of the identity a Claude session id belongs to.")
@click.option("--running", "only_running", is_flag=True,
              help="Only work that is running and whose identity is alive (a crashed session's running rows are stale).")
@click.option("--json", "as_json", is_flag=True, help="Print the identity, whether it is alive, and its rows as JSON.")
@click.option("--reap", "reap_first", is_flag=True,
              help="First close the running rows whose work has ended (process gone, worktree gone, a ghost of an earlier "
                   "session); they are listed under 'reaped'.")
@click.pass_context
def work_list_cmd(ctx: click.Context, seat: str | None, session_id: str | None, only_running: bool, as_json: bool,
                  reap_first: bool) -> None:
    """List the work of an identity: SEAT (a name or an id), --session, or the caller's own.

    A running row of an identity that is not alive is marked stale.
    """
    from nexus.identity import work  # noqa: PLC0415

    if seat and session_id:
        raise click.UsageError("give a seat or --session, not both")
    try:
        report = work.list_rows(_env(ctx), seat, session_id, only_running=only_running, reap_first=reap_first)
    except IdentityCommandError as exc:
        raise click.ClickException(str(exc)) from exc
    if as_json:
        click.echo(json.dumps(report, indent=2, sort_keys=True))
    else:
        for line in work.list_lines(report):
            click.echo(line)


@work_group.command("owners")
@_KIND
@_REF
@click.pass_context
def work_owners_cmd(ctx: click.Context, kind: str, ref: str) -> None:
    """Who registered a resource, and whether each owner's holder is alive."""
    from nexus.identity import work  # noqa: PLC0415

    _run(ctx, lambda env: work.owners_lines(env, kind, ref))


@work_group.command("run", context_settings={"ignore_unknown_options": True})
@_KIND
@_REF
@click.argument("argv", nargs=-1, type=click.UNPROCESSED, required=True)
@click.pass_context
def work_run_cmd(ctx: click.Context, kind: str, ref: str, argv: tuple[str, ...]) -> None:
    """Register work, run the command after --, mark it done when it exits, and exit with its status.

    nx identity work run --kind labrun --ref pytest-full -- lab-remote -c lab-m -- uv run pytest tests/x
    """
    from nexus.identity import work  # noqa: PLC0415

    try:
        code = work.run_command(lambda: _env(ctx), kind, ref, argv)
    except IdentityCommandError as exc:
        raise click.ClickException(str(exc)) from exc
    ctx.exit(code)
