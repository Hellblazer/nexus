# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""CLI command group for the RDR-205 Linda tuple space (bead nexus-em75s.10).

Subcommands:

  out        -- write a tuple.
  rd         -- non-destructive read (probe by default; --timeout-s blocks).
  in         -- destructive (claiming) read (probe by default; --timeout-s blocks).
  ack        -- consume a claimed tuple.
  nack       -- release a claim back to available.
  templates  -- the boot-loaded template registry (digest, sources, templates).
  list       -- concrete subspaces that exist.
  stats      -- the census for one subspace.

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
    """RDR-205 Linda tuple space: out / rd / in / ack / nack / templates / list / stats."""


@tuple_group.command(name="out")
@click.argument("subspace")
@click.option("--key", "keys", multiple=True, metavar="KEY=VALUE",
              help="A pinned key field (repeatable).")
@click.option("--dim", "dims", multiple=True, metavar="KEY=VALUE",
              help="A dimension field (repeatable).")
@click.option("--body", default=None, help="Tuple payload.")
@click.option("--nonce", default=None, help="Caller-minted nonce, for templates whose id_from includes it.")
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
              help="Claim lease length, capped at the template's max_lease_seconds.")
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
def tuple_ack_cmd(claim_id: str, claimant: str) -> None:
    """Consume a claimed tuple. The row is invisible to rd/in after this."""
    try:
        _store().ack(claim_id, claimant)
    except Exception as e:  # noqa: BLE001 — CLI boundary: report and exit non-zero, never traceback
        _print_tuple_error(e)
        raise SystemExit(1) from e
    click.echo(f"Acked claim {claim_id}")


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
