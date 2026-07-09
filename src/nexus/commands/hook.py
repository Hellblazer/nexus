# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx hook — SessionStart and SessionEnd hook subcommands."""
import json
import os
import sys
from typing import IO

import click
import structlog

from nexus import hooks

_log = structlog.get_logger()


def _read_stdin_session_id(stream: IO[str]) -> str | None:
    """Read and parse a Claude-Code SessionStart JSON payload from *stream*.

    Returns the ``session_id`` field or ``None`` when no usable payload
    is available. Designed to be safe against the three problematic
    inputs that produced nexus-rv2x:

    * **TTY stdin** (no piped input). Reading would block until EOF
      (Ctrl+D) or process death. Detected via ``isatty()`` and skipped
      without calling ``read()``.
    * **Empty / closed pipe**. ``read()`` returns ``""`` promptly;
      ``json.loads`` raises; helper returns ``None``.
    * **Malformed JSON**. Same swallow-and-return-None as empty.

    The Claude Code invocation path is unchanged: a pipe carrying a
    valid JSON payload reads and returns the ``session_id`` as before.
    """
    try:
        if stream.isatty():
            return None
        raw = stream.read()
    except Exception as exc:  # noqa: BLE001 — hook stdin read must not crash session-start; logged at debug
        _log.debug("session_start_stdin_read_failed", error=str(exc))
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001 — hook stdin parse must not crash session-start; logged at debug
        _log.debug("session_start_stdin_parse_failed", error=str(exc))
        return None
    if not isinstance(data, dict):
        return None
    sid = data.get("session_id")
    return sid if isinstance(sid, str) and sid else None


@click.group("hook")
def hook_group() -> None:
    """Claude Code lifecycle hook runners."""


@hook_group.command("session-start")
def session_start_cmd() -> None:
    """Run the SessionStart hook (called by Claude Code on session open)."""
    # nexus-rv2x: TTY-aware stdin parse. Skips read() on a TTY so
    # ``nx hook session-start`` invoked from a shell does not hang.
    claude_session_id = _read_stdin_session_id(sys.stdin)
    output = hooks.session_start(claude_session_id=claude_session_id)
    click.echo(output)


@hook_group.command("session-end")
def session_end_cmd() -> None:
    """Run the SessionEnd hook (called by Claude Code on session close)."""
    output = hooks.session_end()
    click.echo(output)


@hook_group.command("session-end-flush")
def session_end_flush_cmd() -> None:
    """Run only the storage-flush portion of SessionEnd (RDR-094 Phase B).

    Runs T1 scratch flush + T2 expire; does NOT touch chroma. The
    nx-session-end-launcher's grandchild dispatches to this entry
    point (Phase C / nexus-l828); the chroma teardown is owned by
    nx-mcp's lifespan + signal handler + atexit chain.
    """
    output = hooks.session_end_flush()
    click.echo(output)


@hook_group.command("session-end-detach")
def session_end_detach_cmd() -> None:
    """Fire-and-forget SessionEnd runner (nexus-99jb Layer 2).

    Double-forks into a detached grandchild that runs ``session_end``
    synchronously, then returns control to Claude Code in <50ms. This
    pattern survives the SIGTERM Claude Code sends to hook subprocesses
    at session close (anthropics/claude-code#41577) because by the time
    the signal is delivered the grandchild has already been reparented
    to init and no longer shares the hook's process group.

    On platforms without ``os.fork`` (Windows), falls through to the
    synchronous path so behaviour degrades gracefully rather than
    silently skipping the cleanup entirely.
    """
    if not hasattr(os, "fork"):
        output = hooks.session_end()
        click.echo(output)
        return

    # First fork: allow the parent (us, the hook subprocess Claude Code
    # launched) to return immediately. The child will daemonize.
    try:
        first_pid = os.fork()
    except OSError as exc:
        _log.warning("session_end_detach_fork_failed", error=str(exc))
        hooks.session_end()
        return
    if first_pid > 0:
        # Parent: exit immediately. Claude Code sees exit 0 and moves on.
        os._exit(0)

    # Child: detach from controlling terminal and start a new session
    # so the grandchild isn't in Claude Code's process group and
    # survives a pgrp-wide SIGTERM.
    try:
        os.setsid()
    except OSError:
        pass

    # Second fork: the grandchild is not a session leader, so it can
    # never reacquire a controlling terminal. That's the canonical
    # daemon recipe.
    try:
        second_pid = os.fork()
    except OSError:
        # Best effort: run the work right here if we can't fork again.
        hooks.session_end()
        os._exit(0)
    if second_pid > 0:
        os._exit(0)

    # Redirect stdio to /dev/null so the grandchild doesn't share the
    # hook's fds (Claude Code may close them on shutdown, and writes
    # to a closed fd would kill us).
    try:
        devnull = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            try:
                os.dup2(devnull, fd)
            except OSError:
                pass
        if devnull > 2:
            os.close(devnull)
    except OSError:
        pass

    # Now do the real work. Exceptions are swallowed — we're detached
    # and nothing can observe us anyway.
    try:
        hooks.session_end()
    except Exception:  # noqa: BLE001 — hook teardown best-effort before os._exit; nothing to recover
        pass
    os._exit(0)


@hook_group.command("routing-stats")
@click.option(
    "--log-path",
    type=click.Path(path_type=str, dir_okay=False),
    default=None,
    help="Path to the routing log JSONL. Defaults to NX_ROUTING_LOG_PATH "
    "or ~/.config/nexus/routing_log.jsonl.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit aggregated stats as JSON instead of a human table.",
)
@click.option(
    "--escapes",
    "show_escapes",
    is_flag=True,
    default=False,
    help="List the escape events with their # routing-allow: reasons "
    "instead of aggregate stats (the audit surface for escape over-use).",
)
def routing_stats_cmd(log_path: str | None, as_json: bool, show_escapes: bool) -> None:
    """Aggregate the routing-hook log into per-rule fire / deny / escape stats.

    RDR-121 Phase 3. Reads JSONL records produced by the routing hook
    framework (`conexus/hooks/scripts/routing/_lib.log_routing_event`) and
    reports per-rule outcomes. Used at the 30-day soak review to spot
    false positives (high escape rate), inert matchers (zero fires), or
    overly broad blocks (high block rate).
    """
    import pathlib as _pathlib  # noqa: PLC0415 — stdlib pathlib deferred to subcommand scope

    from nexus.routing_stats import (  # noqa: PLC0415 — deferred import; routing_stats only needed in this subcommand
        aggregate_detailed,
        default_log_path,
        escape_events,
        registered_rules,
        stats_to_json,
    )

    path = _pathlib.Path(log_path) if log_path else default_log_path()

    if show_escapes:
        events = escape_events(path)
        if as_json:
            click.echo(json.dumps(events, indent=2))
            return
        if not events:
            click.echo(f"No escape events recorded at {path}.")
            return
        for ev in events:
            reason = ev["reason"] or "(reason not captured — pre-mzvwa.9 event)"
            click.echo(f"{ev['ts']}  {ev['rule']}: {reason}")
        click.echo()
        click.echo(f"{len(events)} escape event(s). Source: {path}")
        return

    stats, selftest_excluded = aggregate_detailed(path)

    if as_json:
        payload = {
            "rules": stats_to_json(stats),
            "selftest_excluded": selftest_excluded,
        }
        active = registered_rules()
        if active is not None:
            payload["unregistered_rules"] = sorted(set(stats) - active)
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
        return

    if not stats:
        click.echo(f"No routing-hook events recorded at {path}.")
        return

    # nexus-mzvwa.9: the log is append-only history — rules whose hooks.json
    # registration has since been removed keep their rows forever. Mark them
    # so a stats row is never read as "this hook is live" (the mzvwa.7 soak
    # review's own miss: registry.yaml is documentation, hooks.json is the
    # registration surface).
    active = registered_rules()

    header = f"{'rule':<48} {'total':>6} {'allow':>6} {'deny':>6} {'escape':>6} {'block%':>7} {'esc%':>6}"
    click.echo(header)
    click.echo("-" * len(header))
    for rule in sorted(stats):
        s = stats[rule]
        label = rule[:48]
        if active is not None and rule not in active:
            label = (rule + " (unregistered)")[:48]
        click.echo(
            f"{label:<48} {s.total:>6d} {s.allow:>6d} {s.deny:>6d} "
            f"{s.escape:>6d} {s.block_rate * 100:>6.1f}% {s.escape_rate * 100:>5.1f}%"
        )
    click.echo()
    if selftest_excluded:
        click.echo(
            f"Note: {selftest_excluded} fail-ladder self-test event(s) excluded "
            "(suite-written test_rule/unknown pairs; see nexus-mzvwa.9)."
        )
    if active is None:
        click.echo("Note: no hooks.json found — registration cross-check skipped.")
    click.echo(f"Source: {path}")
