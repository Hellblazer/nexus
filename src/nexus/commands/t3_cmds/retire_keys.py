# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx t3 retire-keys`` — the sweep vehicle of RDR-236 Phase 4 Step 3 (nexus-6pfgb.43).

The engine holds three admin routes under ``/v1/admin/chunk-keys``:

* ``sweep`` — strips the chunk-row metadata keys the allow-list does not name, from THIS credential's tenant, in
  batches, without touching ``last_written_at``. A dry run (the default) counts the rows per collection, the
  preconditions and the time of the batch SELECT on a few collections; it writes nothing, locks nothing and runs no
  UPDATE. Apply refuses while a precondition is non-zero and not accepted (``--accept NAME=N``: up to N rows).
* ``add-constraints`` and ``validate-constraints`` — the table-wide CHECKs, NOT VALID and then VALID, each refused
  while any tenant of the install (every tenant with chunk rows, token or not) is not ready, naming the tenant.
  Operator (root token) only.

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


def parse_accept(values: tuple[str, ...]) -> dict[str, int]:
    """``NAME=N`` pairs to ``{NAME: N}``. A bare name is refused: the number is the ceiling the operator judged."""
    out: dict[str, int] = {}
    for raw in values:
        name, sep, number = raw.partition("=")
        if not sep or not name.strip() or not number.strip().isdigit():
            _fail(
                f"--accept {raw!r}: write NAME=N, the residue and the number of rows you accept (the dry run prints "
                "both). A live count above N still blocks the apply.", EXIT_USAGE,
            )
        out[name.strip()] = int(number)
    return out


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
        report = body.get("report") or {}
        pre = report.get("preconditions") or {}
        accepted = report.get("accepted") or {}
        parts = []
        for n in body.get("blocking", []):
            got = accepted.get(n)
            parts.append(f"{n}={pre.get(n, '?')}" + ("" if got is None else f" (accepted {got})"))
        lines.append("Blocking preconditions: " + ", ".join(parts))
        lines.append("Fix them, or accept each residue with --accept NAME=N (N at least the live count).")
        _fail("\n".join(lines), EXIT_PRECONDITION)
    for t in body.get("tenants") or []:
        detail = ", ".join(f"{k}={v}" for k, v in t.items() if k != "tenant")
        lines.append(f"  tenant {t.get('tenant')}: {detail}")
    checked = body.get("tenants_checked")
    if isinstance(checked, list) and checked:
        lines.append(f"  tenants checked ({len(checked)}): {', '.join(checked)}")
    _fail("\n".join(lines), EXIT_FAILED)


def _render_details(report: dict) -> None:
    details = report.get("precondition_details") or {}
    pre = report.get("preconditions") or {}
    gen = details.get("generated_nulls") or {}
    if pre.get("generated_nulls") and gen:
        by_type = ", ".join(f"{k}={v}" for k, v in (gen.get("by_content_type") or {}).items())
        by_key = ", ".join(f"{k}={v}" for k, v in (gen.get("missing_key") or {}).items())
        click.echo(f"    generated_nulls by content type: {by_type or '-'}; missing key: {by_key or '-'}")
    span = (details.get("null_span_rows") or {}).get("by_field") or {}
    if pre.get("null_span_rows") and span:
        click.echo("    null_span_rows by column: " + ", ".join(f"{k}={v}" for k, v in span.items()))


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
    accepted = report.get("accepted") or {}
    click.echo("  Preconditions:")
    for name, n in pre.items():
        kind = " (acceptable residue)" if name in acceptable else ""
        got = f" [accepted up to {accepted[name]}]" if name in accepted else ""
        click.echo(f"    {name}: {n}{kind}{got}")
    _render_details(report)
    blocking = report.get("blocking") or []
    if blocking:
        click.echo(
            f"  Apply would be refused until fixed or accepted with --accept NAME=N: {', '.join(blocking)}."
        )
    cost = report.get("batch_cost")
    if cost:
        samples = cost.get("samples") or []
        click.echo(
            f"  Batch SELECT timed on {len(samples)} collection(s), nothing written or locked: "
            f"about {cost['select_ms']} ms a batch; at least {cost['projected_seconds']:.1f} s for the whole sweep. "
            "A LOWER BOUND: the update half of a batch is not measured. The first applied batch prints its real time."
        )


def _run_sweep(client: Any, *, tenant: str, apply: bool, accept: dict[str, int], collection: str | None,
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
            "judged with --accept NAME=N (N at least its live count), or fix it.", EXIT_PRECONDITION,
        )
    swept = 0
    calls = 0
    stalled = 0
    last: dict[str, Any] = {}
    resume: dict[str, Any] | None = None
    while True:
        try:
            last = client.chunk_keys_sweep(
                dry_run=False, accept=accept, collection=collection, batch_size=batch_size,
                max_batches=_MAX_BATCHES, budget_seconds=_BUDGET_SECONDS, resume=resume,
            )
        except VectorServiceError as exc:
            _engine_error(exc)
        calls += 1
        n = int(last.get("rows_swept", 0))
        swept += n
        if calls == 1:
            click.echo(f"  first batch: {last.get('first_batch_ms')} ms", err=True)
        locked = int(last.get("locked_skipped_rows") or 0)
        click.echo(
            f"  call {calls}: swept {n}, {last.get('remaining_rows')} left"
            f"{f' ({locked} skipped: locked by a writer)' if locked else ''}"
            f"{' (done)' if last.get('done') else ''}", err=True,
        )
        if last.get("done"):
            break
        # The engine hands back where it stopped; the next call goes straight to the batches (the checks were the
        # first call's), so the 8 s budget is not spent re-counting a large tenant.
        resume = last.get("resume") or None
        stalled = stalled + 1 if n == 0 else 0
        if stalled >= _STALL_LIMIT:
            _fail(f"Stopped: {stalled} calls in a row swept nothing and the engine says rows remain "
                  f"({last.get('remaining_rows')}; {locked} of them locked by a writer).", EXIT_STALLED)
    out["applied"] = {"rows_swept": swept, "calls": calls, "remaining_rows": last.get("remaining_rows"),
                      "locked_skipped_rows": last.get("locked_skipped_rows", 0),
                      "blocked_collections": last.get("blocked_collections", [])}
    if as_json:
        click.echo(json.dumps(out, indent=2, default=str))
        return
    click.echo(f"Swept {swept} rows in {calls} calls; {last.get('remaining_rows')} left.")
    for c in last.get("blocked_collections") or []:
        click.echo(f"  Left alone: {c['collection']} ({c['blocked']}), {c.get('retired_key_rows', '?')} rows.")


def _render_constraints(result: dict, *, add: bool) -> None:
    checked = result.get("tenants_checked")
    names = ", ".join(checked) if isinstance(checked, list) else str(checked)
    count = len(checked) if isinstance(checked, list) else checked
    click.echo(f"{'Add' if add else 'Validate'} (checked {count} tenants: {names}; "
               f"{result.get('table_rows_examined', '?')} chunk rows examined):")
    for c in result.get("constraints") or []:
        state = "" if c.get("validated") is None else f" validated={c['validated']}"
        click.echo(f"  {c['name']}: {c['action']}{state}")
        if c.get("warning"):
            click.echo(f"    WARNING: {c['warning']}.")
        if c["action"] == "present_differs":
            click.echo(
                f"    The stored definition is not the key table's. Expected: {c.get('expected_definition')}. "
                "Re-run with --replace to drop and re-add it."
            )
    left = result.get("left_behind") or []
    if left:
        click.echo(
            "  Left behind: these rows still hold a retired key. The constraint is NOT VALID, which refuses ANY "
            "UPDATE of such a row (not only one that touches its metadata) until it is fixed; VALIDATE cannot pass "
            "while they stand."
        )
        for t in left:
            for c in t.get("blocked_collections") or []:
                click.echo(f"    tenant {t['tenant']}: {c['collection']} ({c['blocked']}), {c['retired_key_rows']} rows")


def _run_constraints(client: Any, *, add: bool, replace: bool, as_json: bool) -> None:
    try:
        result = client.chunk_keys_add_constraints(replace=replace) if add else client.chunk_keys_validate_constraints()
    except VectorServiceError as exc:
        _engine_error(exc)
    if as_json:
        click.echo(json.dumps(result, indent=2, default=str))
        return
    _render_constraints(result, add=add)


@click.command("retire-keys")
@click.option("--tenant", metavar="ID", default=None,
              help="The tenant to sweep. Required for the sweep; the verb refuses if the credential in use is bound "
                   "to another tenant.")
@click.option("--dry-run", "dry_run", is_flag=True, default=False,
              help="Count only (the default). Spelled out so a script says what it means.")
@click.option("--apply", "apply", is_flag=True, default=False,
              help="Write: sweep the tenant, or send --add-check / --validate. Without it nothing is written.")
@click.option("--accept", "accept", multiple=True, metavar="NAME=N",
              help="Accept a non-zero acceptable residue by name and number of rows, e.g. keyless_notes=3 (the dry "
                   "run lists them with their counts). A live count above N still blocks. Repeatable.")
@click.option("--collection", "collection", default=None, metavar="NAME",
              help="Restrict the sweep to one collection.")
@click.option("--batch-size", type=click.IntRange(1, 1000), default=None,
              help="Rows per batch (engine default and ceiling 1000).")
@click.option("--max-rps", type=click.FloatRange(min=0, min_open=True), default=None,
              help="Hard limit on HTTP requests a second, every request the verb sends (default: no limit). "
                   "THE guard for a managed service: start with 2.")
@click.option("--add-check", "add_check", is_flag=True, default=False,
              help="Table-wide, operator only: add the chunk CHECKs NOT VALID. Refused while any tenant is not ready. "
                   "Needs --apply.")
@click.option("--replace", is_flag=True, default=False,
              help="With --add-check: drop and re-add each constraint (a changed key table). A constraint that was "
                   "VALID is NOT VALID until --validate runs again; the engine says so per constraint.")
@click.option("--validate", "validate", is_flag=True, default=False,
              help="Table-wide, operator only: VALIDATE the chunk CHECKs. Needs --apply.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit one JSON document.")
def retire_keys_cmd(tenant: str | None, dry_run: bool, apply: bool, accept: tuple[str, ...], collection: str | None,
                    batch_size: int | None, max_rps: float | None, add_check: bool, replace: bool, validate: bool,
                    as_json: bool) -> None:
    """Retire the chunk-row metadata keys: sweep a tenant, then add and validate the CHECKs (nexus-6pfgb.43).

    \b
    Order, per install:
      1. nx t3 retire-keys --tenant ID            dry run: counts, preconditions, the batch SELECT's time
      2. nx t3 retire-keys --tenant ID --apply    sweep the tenant (repeat for every tenant)
      3. nx t3 retire-keys --add-check --apply    CHECKs NOT VALID (refused while a tenant is not ready)
      4. nx t3 retire-keys --validate --apply     VALIDATE (refused while a tenant breaks one)

    The dry run is the default and writes nothing, locks nothing and runs no UPDATE; its time is the batch SELECT
    only, so the projected duration is a lower bound. Apply refuses while a precondition the engine counts is
    non-zero and not accepted up to its count (--accept NAME=N). The sweep never touches a quarantine- or taxonomy
    collection, a row with a wrong-width chash, a row whose metadata is not an object, or a collection whose
    manifest-less census reads legacy-unmanifested above zero (or did not finish). DATA EFFECT of apply: each swept
    row loses its retired keys; nothing else on the row changes, and last_written_at is kept. DATA EFFECT of
    --add-check: the database then refuses a write that carries a key outside the allow-list, an unequal hash or
    model, or a quarantined_by without quarantined_at; and, because a NOT VALID CHECK is evaluated on every new row
    version, ANY update of a row that still holds a retired key (the rows the sweep leaves in a blocked collection,
    which the output names) until it is fixed. Exit codes: 0 ok; 1 a failed call; 2 usage or the wrong tenant; 3 a
    precondition; 4 an engine without the routes; 5 a stalled apply.
    """
    from nexus.db import request_gate  # noqa: PLC0415 — command-local

    if apply and dry_run:
        _fail("--apply and --dry-run contradict each other.", EXIT_USAGE)
    if add_check and validate:
        _fail("--add-check and --validate are separate steps: run one, then the other.", EXIT_USAGE)
    if replace and not add_check:
        _fail("--replace goes with --add-check.", EXIT_USAGE)
    accepted = parse_accept(accept)
    table_wide = add_check or validate
    if table_wide:
        if tenant:
            _fail("--add-check and --validate are table-wide: they take no --tenant.", EXIT_USAGE)
        if accepted:
            _fail("--accept belongs to the per-tenant sweep, not to --add-check or --validate.", EXIT_USAGE)
        if not apply:
            _fail(
                f"{'--add-check' if add_check else '--validate'} writes (DDL on nexus.chunks) and has no dry run, so "
                "it needs --apply. Run the sweep's dry run for every tenant first, then repeat with --apply.",
                EXIT_USAGE,
            )
    elif not tenant:
        _fail("--tenant is required: name the tenant to sweep.", EXIT_USAGE)

    gate = request_gate.RequestGate(max_rps)
    client = _make_t3()
    with request_gate.paced(gate):
        if table_wide:
            _run_constraints(client, add=add_check, replace=replace, as_json=as_json)
        else:
            assert tenant is not None
            _run_sweep(client, tenant=tenant, apply=apply, accept=accepted, collection=collection,
                       batch_size=batch_size, as_json=as_json)
