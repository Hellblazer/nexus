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
  nack       -- release a claim back to available.
  templates  -- the boot-loaded template registry (digest, sources, templates).
  list       -- concrete subspaces that exist.
  stats      -- the census for one subspace.
  watch      -- ping-then-pull mailbox watcher for a Claude Code Monitor
                (bead nexus-6konb.2; loop in ``nexus.tuple_watch``).

Every subcommand calls through ``nexus.db.t2.http_tuple_store.HttpTupleStore``
(RDR-205 Phase 2 Step 1, nexus-em75s.9) — none of them talks HTTP itself.
"""
from __future__ import annotations

import json
from pathlib import Path
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
    """RDR-205 Linda tuple space: out / rd / in / ack / nack / templates / list / stats / watch."""


@tuple_group.command(name="out")
@click.argument("subspace")
@click.option("--key", "keys", multiple=True, metavar="KEY=VALUE",
              help="A pinned key field (repeatable).")
@click.option("--dim", "dims", multiple=True, metavar="KEY=VALUE",
              help="A dimension field (repeatable).")
@click.option("--body", default=None, help="Tuple payload.")
@click.option(
    "--nonce", default=None,
    help=(
        "Caller-minted nonce. REQUIRED for a keys+nonce template (the "
        "mailbox) -- refused as SchemaViolation without one; omit for a "
        "keys-only template (the ledger). An id ingredient only, never "
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


@tuple_group.command(name="rd")
@click.argument("subspace")
@click.option("--pattern", "patterns", multiple=True, metavar="KEY=VALUE",
              help="A key-equality filter (repeatable; subset match).")
@click.option("-n", "n", type=int, default=1, show_default=True, help="Max rows to return.")
@click.option("--timeout-s", "timeout_s", type=int, default=0, show_default=True,
              help="Seconds to park when nothing matches immediately; 0 never blocks.")
@click.option("--json", "json_out", is_flag=True, default=False, help="Output as JSON array.")
def tuple_rd_cmd(
    subspace: str, patterns: tuple[str, ...], n: int, timeout_s: int, json_out: bool,
) -> None:
    """Non-destructive read from SUBSPACE. A probe by default (--timeout-s 0);
    returns dead-lettered rows too (dead-lettering is a claim state, not an
    exclusion)."""
    pattern_map = _parse_kv_pairs(patterns, option_name="--pattern") or None
    try:
        rows = _store().rd(subspace, pattern_map, n=n, timeout_s=timeout_s)
    except Exception as e:  # noqa: BLE001 — CLI boundary: report and exit non-zero, never traceback
        _print_tuple_error(e)
        raise SystemExit(1) from e
    _render_rows(rows, json_out)


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
        click.echo(json.dumps({"tuple": _row_dict(row), "claim_id": claim_id}))
    else:
        click.echo(f"claim_id={claim_id}")
        _print_row(row)


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
@click.option("--reply-body", "reply_body", default=None,
              help="Reply payload. Requires --reply-subspace.")
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
@click.option("--json", "json_out", is_flag=True, default=False, help="Output as JSON array.")
def tuple_list_cmd(prefix: str | None, json_out: bool) -> None:
    """Concrete tuple subspaces that exist."""
    try:
        rows = _store().subspace_list(prefix)
    except Exception as e:  # noqa: BLE001 — CLI boundary: report and exit non-zero, never traceback
        _print_tuple_error(e)
        raise SystemExit(1) from e
    if json_out:
        click.echo(json.dumps([_census_dict(c) for c in rows]))
        return
    if not rows:
        click.echo("No subspaces.")
        return
    for c in rows:
        click.echo(
            f"{c.subspace}  total={c.total} available={c.available} "
            f"claimed={c.claimed} dead={c.dead} consumed={c.consumed} "
            f"expired_unpurged={c.expired_unpurged}"
        )


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
    click.echo(f"dead: {c.dead}")
    click.echo(f"consumed: {c.consumed}")
    click.echo(f"expired_unpurged: {c.expired_unpurged}")
    click.echo(f"oldest_created_at: {c.oldest_created_at}")
    click.echo(f"newest_created_at: {c.newest_created_at}")


# ── rendering helpers ────────────────────────────────────────────────────────


@tuple_group.command(name="watch")
@click.argument("addresses", nargs=-1)
@click.option("--instance", "instance", default="", metavar="NAME",
              help="This session's instance-name mailbox (the ListAgents row, e.g. nexus-19). "
                   "It is in no environment variable, so it must be passed here.")
@click.option("--interval", "interval_s", type=float, default=3.0, show_default=True,
              help="Seconds between probes.")
@click.option("--reemit-after", "reemit_after_s", type=float, default=600.0, show_default=True,
              help="Seconds before a still-present tuple is pinged again.")
@click.option("--max-emits", "max_emits", type=int, default=3, show_default=True,
              help="Pings per tuple before it goes silent.")
@click.option("--iterations", type=int, default=0, show_default=True,
              help="Probe cycles to run; 0 runs until interrupted.")
@click.option("--state-dir", "state_dir", type=click.Path(path_type=Path), default=None,
              help="Where the seen-set lives (default: the nexus config dir).")
def tuple_watch_cmd(
    addresses: tuple[str, ...], instance: str, interval_s: float, reemit_after_s: float,
    max_emits: int, iterations: int, state_dir: Path | None,
) -> None:
    """Watch mailbox/ADDRESS... and print one ping line per newly arrived tuple.

    An explicit ADDRESS wins outright: it suppresses every default and watches
    exactly what you named. With no ADDRESS, watches the session id, read from
    this process's own environment, and adds the instance mailbox only when
    --instance supplies that name, since it is in no environment variable. So the
    no-flag default is ONE mailbox and it says so; omitting --instance warns
    rather than silently halving the watch. When both are watched, this one
    process probes them, never two Monitors.

    Built to be a Claude Code Monitor source: prints nothing on an empty probe,
    never claims, never prints a body. Re-pings a still-present tuple after
    --reemit-after, at most --max-emits times. Every line saying mail is not being
    delivered goes to stdout, the stream a Monitor watches: pings, a dead-lettered
    row never seen alive, a probe failure and its recovery, the SKIP when the
    engine cannot be read, the refusal when another watcher holds the address.
    Only the death of a row already pinged while alive goes to stderr."""
    from nexus import config as _config  # noqa: PLC0415 — deferred: CLI startup cost
    from nexus.session import resolve_active_session_id  # noqa: PLC0415 — deferred
    from nexus.tuple_watch import (  # noqa: PLC0415 — deferred: CLI startup cost
        PING_PREFIX,
        WatchConfig,
        acquire_watch_locks,
        preflight,
        resolve_watch_addresses,
        run_watch,
    )

    cfg = WatchConfig(interval_s=interval_s, reemit_after_s=reemit_after_s, max_emits=max_emits)
    sd = state_dir or _config.nexus_config_dir()
    report = lambda s: click.echo(s, err=True)  # noqa: E731 — one-liner, matches emit's shape

    # The ADDRESSES are resolved exactly once, here, and never re-resolved inside the
    # loop: CLAUDE_CODE_SESSION_ID is spawn-time env a long-lived process cannot see
    # change, and ~/.config/nexus/current_session is machine-wide and clobbered by every
    # peer session's SessionStart, so a re-resolve is either a no-op or a spurious exit.
    # A moved address is handled by this process dying with its session and the next
    # SessionStart re-arming (MM-3.1/MM-3.2), backed by the lock below.
    resolved = resolve_watch_addresses(
        addresses, instance=instance, session_id=resolve_active_session_id(),
    )
    if resolved.error:
        click.echo(resolved.error)
        return
    for notice in resolved.notices:
        click.echo(notice)
    watched = resolved.addresses

    # EVERY path out of this command says so on stdout before it goes. The shared
    # one-shot-command error helper writes to stderr, which is right for `nx tuple rd`
    # and wrong here: this command's whole contract is that a session watching stdout
    # learns when mail is not being delivered, and the watcher dying is the most
    # complete form of that. So the setup calls are inside the guard too -- a bug in
    # preflight or the lock acquisition itself would otherwise reach Click's default
    # handler as a bare traceback on stderr, with no stdout line at all.
    locks = None
    try:
        store = _store()
        if not preflight(store, watched, config=cfg, emit=click.echo).ok:
            return
        locks = acquire_watch_locks(watched, state_dir=sd, emit=click.echo)
        if not locks.ok:
            return
        run_watch(
            store, watched, config=cfg, state_dir=sd,
            iterations=iterations, emit=click.echo, report=report,
        )
    except KeyboardInterrupt:
        return
    except Exception as e:  # noqa: BLE001 — CLI boundary: report and exit non-zero, never traceback
        click.echo(
            f"{PING_PREFIX} the watcher is exiting and no mailbox is being watched:"
            f" {type(e).__name__}: {e}",
        )
        _print_tuple_error(e)
        raise SystemExit(1) from e
    finally:
        if locks is not None:
            locks.release()


def _row_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row.id, "subspace": row.subspace, "template": row.template,
        "keys": row.keys, "dims": row.dims, "body": row.body,
        "claim_state": row.claim_state, "claimant": row.claimant,
        "lease_until": row.lease_until, "attempts": row.attempts,
        "consumed_at": row.consumed_at, "consumed_by": row.consumed_by,
        "expires_at": row.expires_at, "created_at": row.created_at,
    }


def _census_dict(c: Any) -> dict[str, Any]:
    return {
        "subspace": c.subspace, "total": c.total, "available": c.available,
        "claimed": c.claimed, "dead": c.dead, "consumed": c.consumed,
        "expired_unpurged": c.expired_unpurged,
        "oldest_created_at": c.oldest_created_at, "newest_created_at": c.newest_created_at,
    }


def _print_row(row: Any) -> None:
    click.echo(f"  id: {row.id}")
    click.echo(f"  subspace: {row.subspace}")
    click.echo(f"  template: {row.template}")
    click.echo(f"  keys: {row.keys}")
    click.echo(f"  dims: {row.dims}")
    click.echo(f"  body: {row.body}")
    click.echo(f"  claim_state: {row.claim_state}")
    click.echo(f"  created_at: {row.created_at}")


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
