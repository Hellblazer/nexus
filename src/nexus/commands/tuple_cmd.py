# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""CLI command group for the RDR-205 Linda tuple space (bead nexus-em75s.10).

Subcommands:

  out        -- write a tuple.
  rd         -- non-destructive read (probe by default; --timeout-s blocks).
  in         -- destructive (claiming) read (probe by default; --timeout-s blocks).
  ack        -- consume a claimed tuple, optionally writing a reply in the
                same transaction (--reply-* flags, RDR-206).
  renew      -- extend a live claim's lease before it lapses (RDR-206).
  nack       -- release a claim back to available, counting an attempt.
  release    -- end a live claim without counting an attempt (RDR-211):
                a hand-back that is not a failure.
  templates  -- the boot-loaded template registry (digest, sources, templates).
  list       -- concrete subspaces that exist.
  stats      -- the census for one subspace.
  directory  -- who holds a name in the RDR-208 session directory (bead
                nexus-galkv.11; resolver in ``nexus.tuple_directory``).
  wait       -- park until new mail or a board post arrives for this session,
                then exit: the channel-free push path, run as a background
                task by a session launched without --channels (RDR-233).

Mailbox push delivery is the RDR-211 MCP subscription tools
(``tuple_subscribe``/``tuple_unsubscribe``/``tuple_subscriptions``), the
Claude Code channel, and the ``UserPromptSubmit`` drain hook. ``wait`` covers
only a session the channel cannot reach (RDR-233): it refuses where the channel
launch flag is present. The prior ping-then-pull watcher loop was deleted at
RDR-211 nexus-rplay.14.

Every subcommand calls through ``nexus.db.t2.http_tuple_store.HttpTupleStore``
(RDR-205 Phase 2 Step 1, nexus-em75s.9) — none of them talks HTTP itself.
"""
from __future__ import annotations

import json
from typing import Any

import click
import structlog

_log = structlog.get_logger(__name__)


def _parse_kv_pairs(pairs: tuple[str, ...], *, option_name: str) -> dict[str, str]:
    """Parse repeatable ``KEY=VALUE`` options into a plain string-valued dict."""
    out: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise click.UsageError(
                f"{option_name} expects KEY=VALUE, got {pair!r}",
            )
        key, value = pair.split("=", 1)
        if not key:
            raise click.UsageError(f"{option_name} expects a non-empty KEY, got {pair!r}")
        out[key] = value
    return out


def _store():
    from nexus.db.t2.http_tuple_store import HttpTupleStore  # noqa: PLC0415 — deferred: CLI startup cost
    return HttpTupleStore()


def _print_tuple_error(e: Exception) -> None:
    click.echo(f"Error: {type(e).__name__}: {e}", err=True)


@click.group(name="tuple")
def tuple_group() -> None:
    """RDR-205 Linda tuple space: out / rd / in / ack (with an optional reply) / nack / renew / release / templates / list / stats / directory."""


@tuple_group.command(name="out")
@click.argument("subspace")
@click.option("--key", "keys", multiple=True, metavar="KEY=VALUE",
              help="A pinned key field (repeatable).")
@click.option("--dim", "dims", multiple=True, metavar="KEY=VALUE",
              help="A dimension field (repeatable).")
@click.option(
    "--body", default=None,
    help=(
        "Tuple payload -- a short message or signal, not a document. At "
        "most 4096 bytes UTF-8 (a template may set lower; the ledger's is "
        "0); refused as TooLarge over the limit. Put longer content in T2 "
        "(memory_put) or T3 (store_put) and pass a reference here "
        "(project/title or document id)."
    ),
)
@click.option(
    "--nonce", default=None,
    help=(
        "Caller-minted nonce. REQUIRED for a keys+nonce template (mailbox, "
        "board, board/ci, queue, directory, aspects) -- refused as "
        "SchemaViolation without one; omit for a keys-only template (ledger, "
        "lock). An id ingredient only, never "
        "echoed back on a read."
    ),
)
@click.option("--ttl-seconds", "ttl_seconds", type=int, default=None,
              help="Explicit TTL, capped at the template's retention ceiling.")
def tuple_out_cmd(
    subspace: str,
    keys: tuple[str, ...],
    dims: tuple[str, ...],
    body: str | None,
    nonce: str | None,
    ttl_seconds: int | None,
) -> None:
    """Write a tuple into SUBSPACE. Idempotent by construction: the tuple
    id is derived from the template's ``id_from`` fields only, so a retry
    lands on the same tuple."""
    key_map = _parse_kv_pairs(keys, option_name="--key")
    dim_map = _parse_kv_pairs(dims, option_name="--dim") or None
    try:
        tuple_id = _store().out(
            subspace, key_map, dim_map, body, nonce=nonce, ttl_seconds=ttl_seconds,
        )
    except Exception as e:  # noqa: BLE001 — CLI boundary: report and exit non-zero, never traceback
        _print_tuple_error(e)
        raise SystemExit(1) from e
    click.echo(tuple_id)


#: Rows per engine read: the engine's own default read cap (NX_TUPLE_READ_MAX),
#: which is also the client-wide paging ceiling.
_RD_PAGE: int = 300

#: The default hard bound on --all / --newest: a board or ledger read must
#: stay a bounded call even on a subspace far larger than expected.
_RD_MAX_ROWS_DEFAULT: int = 10_000

#: First words of the truncation note, the marker a machine reader that merges
#: stderr into stdout (the expectations census) keys on.
TRUNCATION_MARKER: str = "nx tuple rd: truncated"


def _parse_since(value: str | None) -> tuple[str, str] | None:
    """``CREATED_AT,ID`` -> the store's ``(created_at, id)`` cursor. Both
    halves are required: the engine's rd drops a cursor without an id and
    reads from the start (review of 195b1bb1b), so a bare timestamp would
    silently return the oldest rows."""
    if not value:
        return None
    created_at, _, row_id = value.partition(",")
    if not created_at.strip() or not row_id.strip():
        raise click.UsageError(
            f"--since expects CREATED_AT,ID (the cursor a truncation note prints), got {value!r}"
        )
    return created_at.strip(), row_id.strip()


def _cursor_of(row: Any) -> str:
    return f"{row.created_at or ''},{row.id}"


def _newest_by_descending_read(
    store: Any, subspace: str, pattern_map: dict[str, str] | None, *,
    n: int, since: tuple[str, str] | None, timeout_s: int,
) -> tuple[list[Any] | None, bool, str | None]:
    """The newest *n* rows via ONE descending engine read, oldest first.

    Returns ``(rows, waited, note)``. *rows* is ``None`` only when the engine
    predates descending reads (nexus-kp5q3) and the caller must page instead.
    *waited* says the engine has already had the park timeout, so a fallback
    must not spend it again: an engine without the read parks first and only
    then answers with no echo, so this is true on that path too.

    The engine echoes the row limit it ran with (n clamped to its read cap,
    NX_TUPLE_READ_MAX), which settles every short page without a second read:
    below the limit the subspace ran out and the page is all of it; exactly
    the limit while *n* asked for more, the cap trimmed it. That case is still
    the true newest rows, only fewer than asked, so it is returned with a
    *note* (the plain read's "asked for N" precedent) rather than re-read by
    paging up to ``--max-rows`` and exiting 3 on a large subspace.
    """
    from nexus.db.t2.http_tuple_store import DescendingReadUnsupportedError  # noqa: PLC0415 — deferred: CLI startup cost

    try:
        read = store.rd_newest(subspace, pattern_map, n=n, since=since, timeout_s=timeout_s)
    except DescendingReadUnsupportedError:
        return None, bool(timeout_s), None
    note = None
    if read.limit < n and len(read.rows) >= read.limit:
        note = (
            f"{TRUNCATION_MARKER}: showing the newest {len(read.rows)} matching rows "
            f"(asked for {n}; the engine's read cap is {read.limit}); older rows may exist."
        )
    return read.rows[::-1], bool(timeout_s), note


@tuple_group.command(name="rd")
@click.argument("subspace")
@click.option("--pattern", "patterns", multiple=True, metavar="KEY=VALUE",
              help="A key-equality filter (repeatable; subset match).")
@click.option("-n", "n", type=int, default=1, show_default=True,
              help=f"Max rows to return. One engine read returns at most {_RD_PAGE}; "
                   "with --newest, how many of the newest rows to keep.")
@click.option("--since", "since", default=None, metavar="CREATED_AT,ID",
              help="Start after this cursor (oldest first). The truncation note "
                   "prints the cursor for the next page.")
@click.option("--all", "read_all", is_flag=True, default=False,
              help="Page through every matching row, oldest first, up to --max-rows "
                   "(-n is ignored).")
@click.option("--newest", is_flag=True, default=False,
              help="Return the newest -n rows instead of the oldest (one "
                   "descending engine read; on an engine without it, pages the "
                   "subspace up to --max-rows).")
@click.option("--max-rows", "max_rows", type=int, default=_RD_MAX_ROWS_DEFAULT,
              show_default=True, help="Hard bound on rows read by --all, and by "
                   "--newest against an engine without descending reads.")
@click.option("--timeout-s", "timeout_s", type=int, default=0, show_default=True,
              help="Seconds to park when nothing matches immediately; 0 never blocks.")
@click.option("--json", "json_out", is_flag=True, default=False, help="Output as JSON array.")
def tuple_rd_cmd(
    subspace: str, patterns: tuple[str, ...], n: int, since: str | None,
    read_all: bool, newest: bool, max_rows: int, timeout_s: int, json_out: bool,
) -> None:
    """Non-destructive read from SUBSPACE, OLDEST rows first. A probe by
    default (--timeout-s 0); returns dead-lettered rows too (dead-lettering
    is a claim state, not an exclusion).

    A plain read returns one page; when more rows exist past it, a note on
    stderr says so and names the --since cursor for the next one. --all pages
    through everything and stops at --max-rows, saying so on stderr
    (nexus-sh1ea: the board read returned the 300 oldest rows with nothing
    saying more existed). --newest asks the engine for a descending read
    (nexus-kp5q3), so it returns the real newest -n whatever the subspace's
    size. Against an engine without one it pages oldest-first and keeps the
    tail, stops at --max-rows, and then exits 3 because the rows it printed
    are not the newest.
    """
    pattern_map = _parse_kv_pairs(patterns, option_name="--pattern") or None
    cursor = _parse_since(since)
    if max_rows <= 0:
        raise click.UsageError("--max-rows must be positive")
    if n <= 0:
        raise click.UsageError("-n must be positive")
    truncated = False
    store = None
    try:
        store = _store()
        if not (read_all or newest):
            rows = store.rd(subspace, pattern_map, n=n, since=cursor, timeout_s=timeout_s)
            # One probe past the last row decides whether more exist, so the
            # note is exact whatever the engine's per-read cap is
            # (NX_TUPLE_READ_MAX) and never fires on a page that ends the data.
            if rows and store.rd(
                subspace, pattern_map, n=1, since=(rows[-1].created_at or "", rows[-1].id),
            ):
                capped = f" (one read returns at most the engine's cap; asked for {n})" if len(rows) < n else ""
                click.echo(
                    f"{TRUNCATION_MARKER}: showing the oldest {len(rows)} matching rows{capped}; "
                    f"more exist. Next page: --since '{_cursor_of(rows[-1])}', "
                    "or --all / --newest.", err=True,
                )
        else:
            rows = []
            park_s = timeout_s
            descended = False
            if newest:
                # nexus-kp5q3: a descending engine read returns the real tail
                # however large the subspace, so --max-rows does not bound it.
                # Paging below stays only for an engine that predates the read
                # (see _newest_by_descending_read).
                found, waited, note = _newest_by_descending_read(
                    store, subspace, pattern_map, n=n, since=cursor, timeout_s=timeout_s,
                )
                if found is not None:
                    rows, descended = found, True
                    if note:
                        click.echo(note, err=True)
                elif waited:
                    park_s = 0
            if not descended:
                # Stop on an EMPTY page, not a short one: the engine's per-read
                # cap is configurable (NX_TUPLE_READ_MAX) and may sit below
                # _RD_PAGE.
                while len(rows) < max_rows:
                    page = store.rd(
                        subspace, pattern_map, n=min(_RD_PAGE, max_rows - len(rows)),
                        since=cursor, timeout_s=park_s if not rows else 0,
                    )
                    if not page:
                        break
                    rows.extend(page)
                    cursor = (page[-1].created_at or "", page[-1].id)
                else:
                    truncated = bool(store.rd(subspace, pattern_map, n=1, since=cursor))
                resume = _cursor_of(rows[-1]) if rows else ""
                if newest:
                    rows = rows[-n:] if n > 0 else []
                if truncated:
                    click.echo(
                        f"{TRUNCATION_MARKER}: stopped at --max-rows {max_rows}; more rows "
                        + ("exist, so these are not the newest. " if newest else "exist. ")
                        + f"Resume with --since '{resume}' or raise --max-rows.", err=True,
                    )
    except Exception as e:  # noqa: BLE001 — CLI boundary: report and exit non-zero, never traceback
        _print_tuple_error(e)
        raise SystemExit(1) from e
    finally:
        if store is not None:
            store.close()
    _render_rows(rows, json_out)
    if newest and truncated:
        # The rows printed are the newest of the oldest --max-rows, not the
        # subspace's newest: a caller reading only stdout must not take them
        # as current. Only the paging fallback reaches here (an engine without
        # descending reads, nexus-kp5q3); a descending read never truncates.
        raise SystemExit(3)


#: Exit status when the conexus channel launch flag is present: the channel
#: already pushes, so a second waiter would wake the session twice per row.
EXIT_CHANNEL_ACTIVE: int = 4
#: Exit status when the engine ignores the per-subscriber announce (not 2,
#: which is click's usage error).
EXIT_ENGINE_TOO_OLD: int = 5

#: Printed when the waiter gives up on the engine. Every exit wakes the
#: session, so it must say that re-running now would only fail again.
STOPPED_HINT: str = (
    "nx tuple wait: stopped. Do not run it again now; mail still arrives at your "
    "next prompt through the drain hook."
)

#: Printed after delivered rows: the wake is one-shot, so the session re-arms.
REARM_HINT: str = (
    "Act on these, then run `nx tuple wait` again with run_in_background to keep "
    "receiving mail and board posts."
)


def _resolve_wait_session(explicit: str | None, claude_pid: int) -> str | None:
    """The session whose mailbox to watch: *explicit*, else the SessionStart
    marker for this ``claude`` process (current after ``/clear``), else the
    env chain."""
    if explicit:
        return explicit
    from nexus import config as _nx_config  # noqa: PLC0415 — deferred: CLI startup cost
    from nexus.session import resolve_active_session_id  # noqa: PLC0415 — deferred: CLI startup cost
    from nexus.session_marker import _read_session_marker  # noqa: PLC0415 — deferred: CLI startup cost

    return _read_session_marker(_nx_config.nexus_config_dir(), claude_pid) or resolve_active_session_id()


def _board_entries_reader(session_id: str):
    """A callable returning this session's board subscriptions from T1, read
    fresh each park; ``[]`` (said once on stderr) when T1 is unreachable."""
    warned: list[bool] = []

    def read() -> list[dict[str, Any]]:
        try:
            from nexus.db.t1 import get_t1_database  # noqa: PLC0415 — deferred: CLI startup cost
            from nexus.mcp.subscriptions import load  # noqa: PLC0415 — deferred: CLI startup cost

            return load(get_t1_database(session_id), session_id).entries()
        except Exception as e:  # noqa: BLE001 — degrade to mailbox-only, never stop the wait
            if not warned:
                warned.append(True)
                click.echo(
                    f"nx tuple wait: board subscriptions unreadable ({type(e).__name__}: {e}); "
                    "waiting on the mailbox only.", err=True,
                )
            return []

    return read


@tuple_group.command(name="wait")
@click.option("--session", "session", default=None, metavar="SESSION_ID",
              help="Watch this session's mailbox (default: the current Claude session).")
@click.option("--timeout-s", "timeout_s", type=int, default=25, show_default=True,
              help="Seconds per engine park; liveness and subscriptions are re-checked between parks.")
@click.option("--max-parks", "max_parks", type=int, default=None,
              help="Give up after this many empty parks (default: wait until something arrives).")
@click.option("--force", is_flag=True, default=False,
              help="Run even when the claude process carries the channel launch flag.")
@click.option("--json", "json_out", is_flag=True, default=False,
              help="Print {reason, notices} as JSON.")
def tuple_wait_cmd(
    session: str | None, timeout_s: int, max_parks: int | None, force: bool, json_out: bool,
) -> None:
    """Park until new mail or a board post for this session arrives, then exit.

    The channel-free push path (RDR-233): run it with run_in_background in a
    Claude Code session launched without --channels (the Claude Desktop Code
    tab). Its exit wakes the idle session, which acts on the printed notices and
    runs it again. Watches mailbox/<session id> and the board topics subscribed
    with tuple_subscribe. Each row is announced once, only while claimable, and
    never when this session wrote it. Prints the channel's notice text, never a
    tuple body.

    Exits 0 on delivery, when replaced by a newer `nx tuple wait` for the same
    session, when the session changes (/clear), or when claude exits; 4 when the
    conexus channel flag is on the claude command line (the channel already
    pushes; --force overrides); 5 on an engine without per-subscriber announce.
    """
    from nexus.channel_launch import channel_launch_state  # noqa: PLC0415 — deferred: CLI startup cost
    from nexus.daemon.service_registry import pid_alive  # noqa: PLC0415 — deferred: CLI startup cost
    from nexus.session import find_immediate_claude_pid  # noqa: PLC0415 — deferred: CLI startup cost

    if timeout_s <= 0:
        raise click.UsageError("--timeout-s must be positive")
    if max_parks is not None and max_parks <= 0:
        raise click.UsageError("--max-parks must be positive")
    claude_pid = find_immediate_claude_pid()
    if not force and channel_launch_state() is True:
        click.echo(
            "nx tuple wait: this session was launched with the conexus channel flag, so "
            "the channel pushes mail and board posts; not starting a second waiter. Do "
            "not run it again in this session, unless channel notifications never "
            "arrive here (plugin not on the channel allowlist, channels disabled by "
            "your organisation): then run it with --force.", err=True,
        )
        raise SystemExit(EXIT_CHANNEL_ACTIVE)
    session_id = _resolve_wait_session(session, claude_pid)
    if not session_id:
        raise click.UsageError("no Claude session id resolved; pass --session")

    from nexus.mcp.channel import _mint_waiter_token  # noqa: PLC0415,SLF001 — deferred: loads the MCP package; the engine's token format has one minter
    from nexus.tuple_wait import (  # noqa: PLC0415 — deferred: CLI startup cost
        DELIVERED,
        SESSION_CHANGED,
        SUPERSEDED,
        EngineTooOldError,
        run_wait,
    )

    store = None
    try:
        store = _store()
        outcome = run_wait(
            store, session_id,
            board_entries=_board_entries_reader(session_id),
            alive=lambda: claude_pid <= 1 or pid_alive(claude_pid),
            current_session=lambda: None if session else _resolve_wait_session(None, claude_pid),
            waiter_token=_mint_waiter_token(),
            park_s=timeout_s, max_parks=max_parks,
        )
    except EngineTooOldError as e:
        _print_tuple_error(e)
        click.echo(STOPPED_HINT, err=True)
        raise SystemExit(EXIT_ENGINE_TOO_OLD) from e
    except Exception as e:  # noqa: BLE001 — CLI boundary: report and exit non-zero, never traceback
        _print_tuple_error(e)
        click.echo(STOPPED_HINT, err=True)
        raise SystemExit(1) from e
    finally:
        if store is not None:
            store.close()

    notes = {
        SUPERSEDED: (
            "nx tuple wait: a newer nx tuple wait for this session took over; nothing to "
            "do, do not run it again."
        ),
        SESSION_CHANGED: (
            "nx tuple wait: this session was replaced (/clear or /resume); do not run "
            "it again here, the new session's SessionStart says whether to."
        ),
    }
    if json_out:
        click.echo(json.dumps({"reason": outcome.reason, "notices": outcome.lines}))
        return
    if outcome.reason == DELIVERED:
        for line in outcome.lines:
            click.echo(line)
        click.echo(REARM_HINT)
    elif outcome.reason in notes:
        click.echo(notes[outcome.reason])
    else:
        click.echo(
            f"nx tuple wait: stopped ({outcome.reason}); nothing new arrived. Do not "
            "run it again unless you mean to keep waiting."
        )


@tuple_group.command(name="in")
@click.argument("subspace")
@click.option("--pattern", "patterns", multiple=True, metavar="KEY=VALUE",
              help="Every pinned key the template declares, exact match (required).")
@click.option("--claimant", required=True, help="This caller's identity.")
@click.option("--lease-s", "lease_s", type=int, required=True,
              help="Claim lease length. Refused above the template's max_lease_seconds; clipped to the row's remaining TTL.")
@click.option("--timeout-s", "timeout_s", type=int, default=0, show_default=True,
              help="Seconds to park when nothing matches immediately; 0 never blocks.")
@click.option("--json", "json_out", is_flag=True, default=False, help="Output as JSON.")
def tuple_in_cmd(
    subspace: str, patterns: tuple[str, ...], claimant: str, lease_s: int,
    timeout_s: int, json_out: bool,
) -> None:
    """Destructive (claiming) read from SUBSPACE. A probe by default
    (--timeout-s 0). Prints the claimed tuple and claim id, or nothing on
    a probe miss (exit code 1)."""
    pattern_map = _parse_kv_pairs(patterns, option_name="--pattern")
    try:
        result = _store().in_(
            subspace, pattern_map, claimant=claimant, lease_s=lease_s, timeout_s=timeout_s,
        )
    except Exception as e:  # noqa: BLE001 — CLI boundary: report and exit non-zero, never traceback
        _print_tuple_error(e)
        raise SystemExit(1) from e
    if result is None:
        click.echo("No matching tuple.", err=True)
        raise SystemExit(1)
    row, claim_id = result
    if json_out:
        click.echo(json.dumps({"tuple": _row_dict(row, fresh_claim=True), "claim_id": claim_id}))
    else:
        click.echo(f"claim_id={claim_id}")
        _print_row(row, fresh_claim=True)


@tuple_group.command(name="ack")
@click.argument("claim_id")
@click.option("--claimant", required=True, help="Must match the identity that made the claim.")
@click.option(
    "--reply-subspace", "reply_subspace", default=None,
    help=(
        "Write a reply into this subspace in the same transaction that "
        "consumes the claim (RDR-206). Must resolve to a keys+nonce "
        "template (SchemaViolation on a keys-only target, e.g. the "
        "ledger). Required by every other --reply-* flag."
    ),
)
@click.option("--reply-key", "reply_keys", multiple=True, metavar="KEY=VALUE",
              help="A pinned key field for the reply (repeatable). Requires --reply-subspace.")
@click.option("--reply-dim", "reply_dims", multiple=True, metavar="KEY=VALUE",
              help="A dimension field for the reply (repeatable). Requires --reply-subspace.")
@click.option(
    "--reply-body", "reply_body", default=None,
    help=(
        "Reply payload -- a short message or signal, not a document. Same "
        "limit as out's --body: at most 4096 bytes UTF-8 (a template may "
        "set lower), refused as TooLarge over it. Put longer content in "
        "T2 (memory_put) or T3 (store_put) and reply with a reference "
        "(project/title or document id). Requires --reply-subspace."
    ),
)
@click.option("--reply-ttl-seconds", "reply_ttl_seconds", type=int, default=None,
              help="Explicit TTL for the reply, capped at its template's retention "
                   "ceiling. Requires --reply-subspace.")
def tuple_ack_cmd(
    claim_id: str,
    claimant: str,
    reply_subspace: str | None,
    reply_keys: tuple[str, ...],
    reply_dims: tuple[str, ...],
    reply_body: str | None,
    reply_ttl_seconds: int | None,
) -> None:
    """Consume a claimed tuple. The row is invisible to rd/in after this.

    With --reply-subspace, the engine writes the reply as it consumes the
    claim, in one transaction, and prints the reply's tuple id. There is no
    --reply-nonce flag: the engine sets the reply's nonce itself, to the
    request's tuple id, and refuses a caller-supplied one.
    """
    any_reply_flag = bool(reply_keys) or bool(reply_dims) or reply_body is not None or (
        reply_ttl_seconds is not None
    )
    if reply_subspace is None:
        if any_reply_flag:
            raise click.UsageError(
                "--reply-key/--reply-dim/--reply-body/--reply-ttl-seconds require "
                "--reply-subspace"
            )
        reply = None
    else:
        from nexus.db.t2.records import ReplySpec  # noqa: PLC0415 — deferred: CLI startup cost
        reply_key_map = _parse_kv_pairs(reply_keys, option_name="--reply-key")
        reply_dim_map = _parse_kv_pairs(reply_dims, option_name="--reply-dim") or None
        reply = ReplySpec(
            subspace=reply_subspace, keys=reply_key_map, dims=reply_dim_map,
            body=reply_body, ttl_seconds=reply_ttl_seconds,
        )
    try:
        reply_id = _store().ack(claim_id, claimant, reply=reply)
    except Exception as e:  # noqa: BLE001 — CLI boundary: report and exit non-zero, never traceback
        _print_tuple_error(e)
        raise SystemExit(1) from e
    click.echo(f"Acked claim {claim_id}")
    if reply_id:
        click.echo(f"reply_id={reply_id}")


@tuple_group.command(name="renew")
@click.option("--claim-id", "claim_id", required=True,
              help="The claim id returned by nx tuple in.")
@click.option("--claimant", required=True, help="Must match the identity that made the claim.")
@click.option("--lease-s", "lease_s", type=int, required=True,
              help="New lease length from now, refused above the template's "
                   "max_lease_seconds and silently clipped to the tuple's own expiry.")
def tuple_renew_cmd(claim_id: str, claimant: str, lease_s: int) -> None:
    """Extend a live claim held by CLAIMANT before its lease lapses.

    Prints the engine's new lease_until -- never a locally computed one,
    because a duration inside the template's cap can still be clipped to the
    tuple's own expiry. Refused on a lapsed claim (ClaimNotFound) rather than
    resurrecting it; never touches attempts.
    """
    try:
        lease_until = _store().renew(claim_id, claimant, lease_s)
    except Exception as e:  # noqa: BLE001 — CLI boundary: report and exit non-zero, never traceback
        _print_tuple_error(e)
        raise SystemExit(1) from e
    click.echo(lease_until.isoformat())


@tuple_group.command(name="nack")
@click.argument("claim_id")
@click.option("--claimant", required=True, help="Must match the identity that made the claim.")
def tuple_nack_cmd(claim_id: str, claimant: str) -> None:
    """Release a claim back to available. Counts an attempt toward the
    template's max_attempts (dead-lettered at the cap)."""
    try:
        _store().nack(claim_id, claimant)
    except Exception as e:  # noqa: BLE001 — CLI boundary: report and exit non-zero, never traceback
        _print_tuple_error(e)
        raise SystemExit(1) from e
    click.echo(f"Nacked claim {claim_id}")


@tuple_group.command(name="release")
@click.argument("claim_id")
@click.option("--claimant", required=True, help="Must match the identity that made the claim.")
def tuple_release_cmd(claim_id: str, claimant: str) -> None:
    """End a live claim WITHOUT counting an attempt (RDR-211) -- a
    hand-back that is not a failure. Use nack instead when the work
    genuinely failed and should count toward the template's max_attempts.

    Refused on a lapsed claim (ClaimNotFound) rather than resurrecting it,
    and on a claim held by another claimant (ClaimOwnership).
    """
    try:
        _store().release(claim_id, claimant)
    except Exception as e:  # noqa: BLE001 — CLI boundary: report and exit non-zero, never traceback
        _print_tuple_error(e)
        raise SystemExit(1) from e
    click.echo(f"Released claim {claim_id}")


@tuple_group.command(name="templates")
@click.option("--json", "json_out", is_flag=True, default=False, help="Output as JSON.")
def tuple_templates_cmd(json_out: bool) -> None:
    """The boot-loaded template registry: digest, sources, templates."""
    try:
        reg = _store().registry()
    except Exception as e:  # noqa: BLE001 — CLI boundary: report and exit non-zero, never traceback
        _print_tuple_error(e)
        raise SystemExit(1) from e
    if json_out:
        click.echo(json.dumps(reg))
        return
    click.echo(f"digest: {reg.get('digest')}")
    click.echo(f"sources: {', '.join(reg.get('sources', []))}")
    for t in reg.get("templates", []):
        click.echo(f"  {t.get('name')}")


@tuple_group.command(name="list")
@click.option("--prefix", default=None, help="Filter to subspaces starting with this prefix.")
@click.option("--limit", type=int, default=None, help="Page size; unset returns every matching subspace.")
@click.option("--after", default=None, help="Subspace-name cursor from a prior truncated page's --json output.")
@click.option("--json", "json_out", is_flag=True, default=False, help="Output as JSON array.")
def tuple_list_cmd(prefix: str | None, limit: int | None, after: str | None, json_out: bool) -> None:
    """Concrete tuple subspaces that exist.

    Unpaged by default (every matching subspace). Pass --limit to page
    (a scalability research pass over this design, bead nexus-xapt8): a truncated page
    prints a "next: --after=<cursor>" hint line (or, under --json, the
    array's last element is {"next_cursor": "..."}).
    """
    try:
        if limit is None:
            rows: list[Any] = _store().subspace_list(prefix)
            next_cursor = None
        else:
            rows, next_cursor = _store().subspace_list(prefix, limit=limit, after=after)
    except Exception as e:  # noqa: BLE001 — CLI boundary: report and exit non-zero, never traceback
        _print_tuple_error(e)
        raise SystemExit(1) from e
    if json_out:
        out: list[Any] = [_census_dict(c) for c in rows]
        if next_cursor:
            out.append({"next_cursor": next_cursor})
        click.echo(json.dumps(out))
        return
    if not rows:
        click.echo("No subspaces.")
        return
    for c in rows:
        click.echo(
            f"{c.subspace}  total={c.total} available={c.available} "
            f"claimed={c.claimed} lapsed={c.lapsed} dead={c.dead} consumed={c.consumed} "
            f"expired_unpurged={c.expired_unpurged}"
        )
    if next_cursor:
        click.echo(f"next: --after={next_cursor}")


@tuple_group.command(name="stats")
@click.argument("subspace")
@click.option("--json", "json_out", is_flag=True, default=False, help="Output as JSON.")
def tuple_stats_cmd(subspace: str, json_out: bool) -> None:
    """The census for one subspace."""
    try:
        c = _store().subspace_stats(subspace)
    except Exception as e:  # noqa: BLE001 — CLI boundary: report and exit non-zero, never traceback
        _print_tuple_error(e)
        raise SystemExit(1) from e
    if json_out:
        click.echo(json.dumps(_census_dict(c)))
        return
    click.echo(f"subspace: {c.subspace}")
    click.echo(f"total: {c.total}")
    click.echo(f"available: {c.available}")
    click.echo(f"claimed: {c.claimed}")
    click.echo(f"lapsed: {c.lapsed}")
    click.echo(f"dead: {c.dead}")
    click.echo(f"consumed: {c.consumed}")
    click.echo(f"expired_unpurged: {c.expired_unpurged}")
    click.echo(f"oldest_created_at: {c.oldest_created_at}")
    click.echo(f"newest_created_at: {c.newest_created_at}")


@tuple_group.command(name="directory")
@click.argument("name")
@click.option("--json", "json_out", is_flag=True, default=False, help="Output as JSON.")
def tuple_directory_cmd(name: str, json_out: bool) -> None:
    """Show who holds NAME in the RDR-208 session directory.

    Prints each live directory/NAME entry's session_id, created_at and
    expires_at, and flags NAME as ambiguous when more than one distinct
    session holds it -- the case `mailbox_send` refuses. Reads
    `directory/NAME` exactly ONCE and classifies that one list through the
    SAME pure classifier `mailbox_send` uses (nexus.tuple_directory.
    classify_directory_holders), so the two can never disagree about
    whether NAME is safely addressable, and a lapse or a re-nonce between
    two reads can never make the printed entries and the verdict below
    describe different moments (gate audit round B item 1).
    """
    from nexus.tuple_directory import (  # noqa: PLC0415 — deferred: CLI startup cost
        DirectoryResolutionError, classify_directory_holders, list_directory_entries,
    )

    store = _store()
    try:
        rows = list_directory_entries(name, store)
    except Exception as e:  # noqa: BLE001 — CLI boundary: report and exit non-zero, never traceback
        _print_tuple_error(e)
        raise SystemExit(1) from e

    entries = [
        {
            "session_id": (row.dims or {}).get("session_id"),
            "created_at": row.created_at,
            "expires_at": row.expires_at,
        }
        for row in rows
    ]
    holders = sorted({e["session_id"] for e in entries if e["session_id"]})

    resolved_session: str | None = None
    ambiguous = False
    if entries:
        try:
            resolved_session, _kind = classify_directory_holders(name, rows)
        except DirectoryResolutionError:
            # entries is non-empty here, so the only way the classifier can
            # still refuse is the more-than-one-holder branch.
            ambiguous = True

    if json_out:
        click.echo(json.dumps({
            "name": name,
            "entries": entries,
            "holders": holders,
            "ambiguous": ambiguous,
            "resolved_session_id": resolved_session,
        }))
        return

    if not entries:
        click.echo(f"No live directory entry for {name!r}.")
        return
    for e in entries:
        click.echo(f"  session_id: {e['session_id']}  created_at: {e['created_at']}  expires_at: {e['expires_at']}")
    if ambiguous:
        click.echo(
            f"{name!r} is held by {len(holders)} sessions -- mailbox_send "
            f"would refuse this name; resend to one of these session ids directly."
        )
    else:
        click.echo(f"{name!r} resolves to session {resolved_session}.")


# ── rendering helpers ────────────────────────────────────────────────────────


def _row_dict(row: Any, *, fresh_claim: bool = False) -> dict[str, Any]:
    # fresh_claim: the row `in` just claimed. The engine set its lease a moment ago, so it
    # is "claimed"; relabelling it against this box's clock could call it "lapsed".
    out = {
        "id": row.id, "subspace": row.subspace, "template": row.template,
        "keys": row.keys, "dims": row.dims, "body": row.body,
        "claim_state": row.claim_state if fresh_claim else row.effective_claim_state(),
        "claimant": row.claimant,
        "lease_until": row.lease_until, "attempts": row.attempts,
        "consumed_at": row.consumed_at, "consumed_by": row.consumed_by,
        "expires_at": row.expires_at, "created_at": row.created_at,
    }
    # nexus-ztwp4 (F3 step 2): null = no stamp: no live minted session at write time, a reply row written by an engine before nexus-g2ckl (v0.1.163), or a row that predates the column;
    # omitted when the engine renders no such key.
    if row.sender_session_known:
        out["sender_session"] = row.sender_session
    return out


def _census_dict(c: Any) -> dict[str, Any]:
    return {
        "subspace": c.subspace, "total": c.total, "available": c.available,
        "claimed": c.claimed, "lapsed": c.lapsed, "dead": c.dead, "consumed": c.consumed,
        "expired_unpurged": c.expired_unpurged,
        "oldest_created_at": c.oldest_created_at, "newest_created_at": c.newest_created_at,
    }


def _print_row(row: Any, *, fresh_claim: bool = False) -> None:
    click.echo(f"  id: {row.id}")
    click.echo(f"  subspace: {row.subspace}")
    click.echo(f"  template: {row.template}")
    click.echo(f"  keys: {row.keys}")
    click.echo(f"  dims: {row.dims}")
    click.echo(f"  body: {row.body}")
    click.echo(f"  claim_state: {row.claim_state if fresh_claim else row.effective_claim_state()}")
    click.echo(f"  created_at: {row.created_at}")
    if row.sender_session_known:
        click.echo(f"  sender_session: {row.sender_session}")


def _render_rows(rows: list, json_out: bool) -> None:
    if json_out:
        click.echo(json.dumps([_row_dict(r) for r in rows]))
        return
    if not rows:
        click.echo("No matching tuples.")
        return
    for row in rows:
        _print_row(row)
        click.echo("")
