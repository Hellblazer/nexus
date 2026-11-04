# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx t3 retire-keys`` — the sweep vehicle of RDR-236 Phase 4 Step 3 (nexus-6pfgb.43).

The engine holds three admin routes under ``/v1/admin/chunk-keys``:

* ``sweep`` — strips the chunk-row metadata keys the allow-list does not name, from THIS credential's tenant, in
  batches, without touching ``last_written_at``. A dry run (the default) counts the rows per collection, the
  preconditions and one measured batch; apply refuses while a precondition is non-zero and not accepted.
* ``add-constraints`` and ``validate-constraints`` — the table-wide CHECKs, NOT VALID and then VALID, each refused
  while any tenant of the install is not ready, naming the tenant. Operator (root token) only.

Nothing calls them on its own. This verb is the operator's way in, per tenant and per install; the upgrade-ladder rung
that would run the sweep on every install ships later, after a cloud dry run has been read.

``--tenant`` names the tenant the operator MEANS. A credential is bound to one tenant, so the verb reads the tenant
back from the dry run and refuses to go on when it is another one: a sweep of the wrong tenant is not undone.

Pacing: ``--max-rps`` holds every request the verb sends to that rate (the request gate, nexus-ldf2s). Run against
the cloud, start with ``--max-rps 2``. An apply is a write and goes through the production-write guard
(``NX_ALLOW_PROD_WRITE``, nexus-a2qhz).
"""
from __future__ import annotations

import json
from typing import Any, NoReturn

import click

from nexus.db.http_vector_client import VectorServiceError

#: Exit codes. 1 is a failed call, 2 a usage error or the wrong tenant, 3 a precondition the engine counts that is
#: neither zero nor accepted, 4 an engine that predates the routes, 5 a stalled apply.
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_PRECONDITION = 3
EXIT_NO_ROUTE = 4
EXIT_STALLED = 5

#: Consecutive apply calls that swept nothing and did not finish before the verb gives up.
_STALL_LIMIT = 3

#: ``budget_seconds`` of one apply call. Short on purpose: the public edge of a managed engine cuts a request at about
#: 30 seconds, and the verb loops while the engine says ``done`` is false.
_BUDGET_SECONDS = 8
_MAX_BATCHES = 10


def _make_t3():
    """The T3 client (``HttpVectorClient`` in every mode). Patched in tests."""
    from nexus.db import make_t3  # noqa: PLC0415 — command-local import deferred to avoid CLI startup cost (nexus.db)

    return make_t3()


def _fail(message: str, code: int) -> NoReturn:
    click.echo(message, err=True)
    raise SystemExit(code)


def _engine_error(exc: VectorServiceError) -> NoReturn:
    if exc.code == 404:
        _fail(
            "This engine does not carry the retire-keys routes (HTTP 404). Upgrade the engine to the release that "
            "ships nexus-6pfgb.43.", EXIT_NO_ROUTE,
        )
    body = exc.engine_body or {}
    reason = exc.reason or body.get("reason")
    lines = [f"The engine refused: {body.get('error') or exc}"]
    if reason == "sweep_preconditions_not_met":
        pre = (body.get("report") or {}).get("preconditions") or {}
        lines.append("Blocking preconditions: " + ", ".join(f"{n}={pre.get(n, '?')}" for n in body.get("blocking", [])))
        lines.append("Fix them, or accept each residue with --accept NAME.")
        _fail("\n".join(lines), EXIT_PRECONDITION)
    for t in body.get("tenants") or []:
        detail = ", ".join(f"{k}={v}" for k, v in t.items() if k != "tenant")
        lines.append(f"  tenant {t.get('tenant')}: {detail}")
    _fail("\n".join(lines), EXIT_FAILED)


def _render_dry_run(report: dict, *, tenant: str) -> None:
    click.echo(f"Tenant {tenant}: retire-keys dry run")
    cols = report.get("collections") or []
    if not cols:
        click.echo("  No collection holds a retired key.")
    for c in cols:
        note = f"  BLOCKED ({c['blocked']})" if c.get("blocked") else ""
        census = c.get("legacy_unmanifested")
        click.echo(
            f"  {c['collection']}: {c['retired_key_rows']} rows with a retired key"
            f" ({c['sweepable_rows']} to sweep, {c['wrong_width_rows']} with a wrong-width chash"
            f"{'' if census is None else f', legacy-unmanifested {census}'}){note}"
        )
    totals = report.get("totals") or {}
    click.echo(
        f"  Total: {totals.get('retired_key_rows', 0)} rows with a retired key, "
        f"{totals.get('sweepable_rows', 0)} to sweep, {totals.get('blocked_rows', 0)} in blocked collections, "
        f"{totals.get('wrong_width_rows', 0)} with a wrong-width chash (left alone)."
    )
    pre = report.get("preconditions") or {}
    acceptable = set(report.get("acceptable_residues") or [])
    click.echo("  Preconditions:")
    for name, n in pre.items():
        kind = " (acceptable residue)" if name in acceptable else ""
        click.echo(f"    {name}: {n}{kind}")
    blocking = report.get("blocking") or []
    if blocking:
        click.echo(f"  Apply would be refused until accepted or fixed: {', '.join(blocking)}.")
    cost = report.get("batch_cost")
    if cost:
        click.echo(
            f"  One measured batch ({cost['rows']} rows, rolled back): select {cost['select_ms']} ms, "
            f"update {cost['update_ms']} ms; about {cost['projected_seconds']:.1f} s for the whole sweep."
        )


def _run_sweep(client: Any, *, tenant: str, apply: bool, accept: tuple[str, ...], collection: str | None,
               batch_size: int | None, as_json: bool) -> None:
    try:
        report = client.chunk_keys_sweep(
            dry_run=True, accept=accept, collection=collection, batch_size=batch_size,
            measure_batch=None if not apply else False,
        )
    except VectorServiceError as exc:
        _engine_error(exc)
    if report.get("tenant") != tenant:
        _fail(
            f"The credential in use is bound to tenant {report.get('tenant')!r}, not {tenant!r}. A token is bound to "
            f"one tenant: run this with {tenant!r}'s NX_SERVICE_TOKEN.", EXIT_USAGE,
        )
    out: dict[str, Any] = {"dry_run": not apply, "report": report}
    if not as_json:
        _render_dry_run(report, tenant=tenant)
    if not apply:
        if as_json:
            click.echo(json.dumps(out, indent=2, default=str))
        else:
            click.echo("Dry run: nothing written. Re-run with --apply.")
        return
    if report.get("blocking"):
        if as_json:
            click.echo(json.dumps(out, indent=2, default=str))
        _fail(
            "Not applied: " + ", ".join(report["blocking"]) + " stand in the way. Accept each residue you have "
            "judged with --accept NAME, or fix it.", EXIT_PRECONDITION,
        )
    swept = 0
    calls = 0
    stalled = 0
    last: dict[str, Any] = {}
    while True:
        try:
            last = client.chunk_keys_sweep(
                dry_run=False, accept=accept, collection=collection, batch_size=batch_size,
                max_batches=_MAX_BATCHES, budget_seconds=_BUDGET_SECONDS,
            )
        except VectorServiceError as exc:
            _engine_error(exc)
        calls += 1
        n = int(last.get("rows_swept", 0))
        swept += n
        if calls == 1:
            click.echo(f"  first batch: {last.get('first_batch_ms')} ms", err=True)
        click.echo(
            f"  call {calls}: swept {n}, {last.get('remaining_rows')} left"
            f"{' (done)' if last.get('done') else ''}", err=True,
        )
        if last.get("done"):
            break
        stalled = stalled + 1 if n == 0 else 0
        if stalled >= _STALL_LIMIT:
            _fail(f"Stopped: {stalled} calls in a row swept nothing and the engine says rows remain "
                  f"({last.get('remaining_rows')}).", EXIT_STALLED)
    out["applied"] = {"rows_swept": swept, "calls": calls, "remaining_rows": last.get("remaining_rows"),
                      "blocked_collections": last.get("blocked_collections", [])}
    if as_json:
        click.echo(json.dumps(out, indent=2, default=str))
        return
    click.echo(f"Swept {swept} rows in {calls} calls; {last.get('remaining_rows')} left.")
    for c in last.get("blocked_collections") or []:
        click.echo(f"  Left alone: {c['collection']} ({c['blocked']}).")


def _run_constraints(client: Any, *, add: bool, replace: bool, as_json: bool) -> None:
    try:
        result = client.chunk_keys_add_constraints(replace=replace) if add else client.chunk_keys_validate_constraints()
    except VectorServiceError as exc:
        _engine_error(exc)
    if as_json:
        click.echo(json.dumps(result, indent=2, default=str))
        return
    click.echo(f"{'Add' if add else 'Validate'} (checked {result.get('tenants_checked')} tenants):")
    for c in result.get("constraints") or []:
        state = "" if c.get("validated") is None else f" validated={c['validated']}"
        click.echo(f"  {c['name']}: {c['action']}{state}")


@click.command("retire-keys")
@click.option("--tenant", metavar="ID", default=None,
              help="The tenant to sweep. Required for the sweep; the verb refuses if the credential in use is bound "
                   "to another tenant.")
@click.option("--dry-run", "dry_run", is_flag=True, default=False,
              help="Count only (the default). Spelled out so a script says what it means.")
@click.option("--apply", "apply", is_flag=True, default=False,
              help="Write: sweep the tenant, or send --add-check / --validate. Without it nothing is written.")
@click.option("--accept", "accept", multiple=True, metavar="NAME",
              help="Accept a non-zero acceptable residue by name (the dry run lists them). Repeatable.")
@click.option("--collection", "collection", default=None, metavar="NAME",
              help="Restrict the sweep to one collection.")
@click.option("--batch-size", type=click.IntRange(1, 1000), default=None,
              help="Rows per batch (engine default and ceiling 1000).")
@click.option("--max-rps", type=click.FloatRange(min=0, min_open=True), default=None,
              help="Hard limit on HTTP requests a second, every request the verb sends (default: no limit). "
                   "THE guard for a managed service: start with 2.")
@click.option("--add-check", "add_check", is_flag=True, default=False,
              help="Table-wide, operator only: add the chunk CHECKs NOT VALID. Refused while any tenant is unswept. "
                   "Needs --apply.")
@click.option("--replace", is_flag=True, default=False,
              help="With --add-check: drop and re-add each constraint (a changed key table).")
@click.option("--validate", "validate", is_flag=True, default=False,
              help="Table-wide, operator only: VALIDATE the chunk CHECKs. Needs --apply.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit one JSON document.")
def retire_keys_cmd(tenant: str | None, dry_run: bool, apply: bool, accept: tuple[str, ...], collection: str | None,
                    batch_size: int | None, max_rps: float | None, add_check: bool, replace: bool, validate: bool,
                    as_json: bool) -> None:
    """Retire the chunk-row metadata keys: sweep a tenant, then add and validate the CHECKs (nexus-6pfgb.43).

    \b
    Order, per install:
      1. nx t3 retire-keys --tenant ID            dry run: counts, preconditions, one measured batch
      2. nx t3 retire-keys --tenant ID --apply    sweep the tenant (repeat for every tenant)
      3. nx t3 retire-keys --add-check --apply    CHECKs NOT VALID (refused while a tenant is unswept)
      4. nx t3 retire-keys --validate --apply     VALIDATE (refused while a tenant breaks one)

    The dry run is the default and writes nothing beyond one rolled-back batch. Apply refuses while a precondition
    the engine counts is non-zero and not accepted. The sweep never touches a quarantine- or taxonomy collection, a
    row with a wrong-width chash, or a collection whose manifest-less census reads legacy-unmanifested above zero.
    DATA EFFECT of apply: each swept row loses its retired keys; nothing else on the row changes, and
    last_written_at is kept. Exit codes: 0 ok; 1 a failed call; 2 usage or the wrong tenant; 3 a precondition;
    4 an engine without the routes; 5 a stalled apply.
    """
    from nexus.db import request_gate  # noqa: PLC0415 — command-local

    if apply and dry_run:
        _fail("--apply and --dry-run contradict each other.", EXIT_USAGE)
    if add_check and validate:
        _fail("--add-check and --validate are separate steps: run one, then the other.", EXIT_USAGE)
    if replace and not add_check:
        _fail("--replace goes with --add-check.", EXIT_USAGE)
    table_wide = add_check or validate
    if table_wide:
        if tenant:
            _fail("--add-check and --validate are table-wide: they take no --tenant.", EXIT_USAGE)
        if not apply:
            click.echo(
                f"{'--add-check' if add_check else '--validate'} writes (DDL on nexus.chunks) and has no dry run. "
                "Run the sweep's dry run for every tenant first, then repeat with --apply."
            )
            return
    elif not tenant:
        _fail("--tenant is required: name the tenant to sweep.", EXIT_USAGE)

    gate = request_gate.RequestGate(max_rps)
    client = _make_t3()
    with request_gate.paced(gate):
        if table_wide:
            _run_constraints(client, add=add_check, replace=replace, as_json=as_json)
        else:
            assert tenant is not None
            _run_sweep(client, tenant=tenant, apply=apply, accept=accept, collection=collection,
                       batch_size=batch_size, as_json=as_json)
