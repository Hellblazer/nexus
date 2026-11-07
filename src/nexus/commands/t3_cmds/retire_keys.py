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

``--tenant`` names the tenant to sweep and is sent to the engine. A credential bound to that tenant sweeps it; the
ROOT credential may name any registered tenant (the engine runs the call in that tenant's scope); a tenant credential
naming another tenant is refused (``tenant_mismatch``), and a data or mint credential is refused by the scope guard.
The verb then checks that the engine's answer names the tenant asked for, before any apply: an engine that predates
the target field ignores it and answers for the credential's own tenant, and a sweep of the wrong tenant is not undone.
That case is one line and exit 4.

Every sweep the ROOT credential makes is audited by the engine, in the target tenant's ``gc_audit`` (operation
``retire_keys_sweep``): an intent row before the sweep reads or changes any chunk row (only the check that the named
tenant is registered runs before it) and an outcome row after the work. The answers carry the
row ids and the actor (a hash prefix of the credential), and the verb prints them. ``--operator LABEL`` is recorded in
both rows, because one shared root token names no person. If the engine cannot write the audit, it refuses before any
chunk row is read or changed; if it cannot write the outcome row after the work, the rows ARE changed and the verb exits 6.

Pacing: ``--max-rps`` holds every request the verb sends to that rate (the request gate, nexus-ldf2s). Run against
the cloud, start with ``--max-rps 2``. An apply is a write and goes through the production-write guard
(``NX_ALLOW_PROD_WRITE``, nexus-a2qhz).
"""
from __future__ import annotations

import json
import urllib.error
from typing import Any, NoReturn

import click

from nexus.db.http_vector_client import VectorServiceError

#: Exit codes. 1 is a failed call, 2 a usage error or a tenant the engine refuses, 3 a precondition the engine counts
#: that is neither zero nor accepted, 4 an engine that predates the routes or the target tenant, 5 a stalled apply,
#: 6 the sweep ran but its audit row could not be written (rows were changed).
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_PRECONDITION = 3
EXIT_NO_ROUTE = 4
EXIT_STALLED = 5
#: The engine did the work and could not write its audit row (or said the sweep failed part-way): rows may have changed.
EXIT_AUDIT = 6

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


def _old_engine_tenant(asked: str, answered: object) -> NoReturn:
    """The engine answered for another tenant than the one asked for: it predates the ``tenant`` field.

    The remedy is the engine, never a tenant token: on a managed cloud client credentials are mint-locked, so a token
    for ``asked`` cannot be made. Both halves are needed: an engine newer than the pinned 0.1.178 that carries the
    root-names-the-target change, and a client with ``--tenant`` (this one has it).
    """
    _fail(
        f"This engine cannot sweep another tenant (it answered for tenant {answered!r}, not {asked!r}): the engine must "
        f"be upgraded to a build newer than engine-service-v0.1.178 that carries the root-names-the-target change "
        f"(nexus-6pfgb.43) AND the client must carry --tenant (this one does); run it with the root service token, "
        f"which is how a mint-locked cloud sweeps a tenant it cannot mint a token for. Nothing was written.",
        EXIT_NO_ROUTE,
    )


def _audit_line(label: str, audit: object) -> str | None:
    """One line naming the gc_audit rows a root sweep call wrote: actor, tenant, intent id, outcome id."""
    if not isinstance(audit, dict):
        return None
    return (f"  audit ({label}): gc_audit rows {audit.get('intent_id')} (intent) and {audit.get('outcome_id')} "
            f"(outcome) in tenant {audit.get('tenant')}, actor {audit.get('actor')}")


def _audit_lines(audits: list[tuple[str, dict]]) -> list[str]:
    return [ln for label, a in audits if (ln := _audit_line(label, a))]


_READ_AUDIT_HINT = (
    "  Read the rows: nexus.gc_audit has row-level security keyed on nexus.tenant, so `nx catalog gc-audit list "
    "--operation retire_keys_sweep --json --limit N` needs a credential bound to the TARGET tenant, and SQL needs a "
    "BYPASSRLS or superuser role, or SET nexus.tenant = '<tenant>' first. A tenant with no live tenant-bound credential "
    "is read through SQL; the ids and counts printed above are always available."
)


def _swept_phrase(body: dict, swept_before: int) -> str:
    n = body.get("rows_swept")
    earlier = f" ({swept_before} more in the earlier calls of this run)" if swept_before else ""
    return f"{n if n is not None else '?'} rows swept by this call{earlier}"


def _engine_error(exc: VectorServiceError, *, swept_before: int = 0,
                  audits: list[tuple[str, dict]] | None = None) -> NoReturn:
    if exc.reason == "sweep_job_not_found":
        _fail(
            "The engine no longer holds the dry-run job (it restarted, or the job expired) and a second start was "
            "lost the same way. Nothing was written; run the same command again.", EXIT_FAILED,
        )
    if exc.reason == "sweep_job_still_running":
        _fail(
            f"{exc} Nothing was written. The dry run is a read; the engine bounds how many it runs at once.",
            EXIT_FAILED,
        )
    if exc.code == 404:
        _fail(
            "This engine does not carry the retire-keys routes (HTTP 404). Upgrade the engine to the release that "
            "ships nexus-6pfgb.43.", EXIT_NO_ROUTE,
        )
    body = exc.engine_body or {}
    reason = exc.reason or body.get("reason")
    lines = [f"The engine refused: {body.get('error') or exc}"]
    if reason in ("tenant_mismatch", "unknown_tenant"):
        _fail("\n".join(lines), EXIT_USAGE)
    if reason in ("audit_unavailable", "audit_intent_failed"):
        # Refused before any chunk row was read or changed: say so, and that the sweep is not the thing that is wrong.
        lines.append("The sweep records every root-credential call in gc_audit first and refuses when it cannot. "
                     "No chunk row was read or changed; retry once the engine's audit sink is available.")
        _fail("\n".join(lines + _audit_lines(audits or [])), EXIT_FAILED)
    if reason == "audit_write_failed":
        changed = body.get("rows_changed")
        lines = ["The sweep RAN, but the engine could not write the outcome row of its audit "
                 "(reason audit_write_failed)."]
        if body.get("dry_run"):
            lines.append("  This call was a dry run: no row was changed.")
        else:
            lines.append("  " + _swept_phrase(body, swept_before) + "; "
                         + ("those rows WERE changed and cannot be taken back." if changed
                            else "no batch was confirmed committed."))
        audit = body.get("audit") or {}
        lines.append(f"  The intent row is gc_audit id {audit.get('intent_id')} in tenant {audit.get('tenant')} "
                     f"(actor {audit.get('actor')}); there is no outcome row for this call.")
        lines.extend(_audit_lines(audits or []))
        if body.get("resume"):
            lines.append("  The engine handed back where it stopped; re-running the same command takes up from the "
                         "rows that are left (a repeated apply is safe: it sweeps what is still there).")
        else:
            lines.append("  Re-running the same command is safe: the sweep takes only the rows that are still there.")
        lines.append(_READ_AUDIT_HINT)
        _fail("\n".join(lines), EXIT_AUDIT)
    if reason == "sweep_failed":
        lines = [f"The engine's sweep failed part-way: {body.get('error')}"]
        lines.append("  " + _swept_phrase(body, swept_before) + "; "
                     + ("those rows WERE changed (the count is of confirmed batches; the batch that was in flight at "
                        "the failure may also have committed)." if body.get("rows_changed")
                        else "no batch was confirmed committed (the batch that was in flight at the failure may still "
                             "have committed)."))
        a = body.get("audit")
        if a and a.get("outcome_id") is not None:
            lines.append(f"  The outcome row (failed, with the partial counts) is gc_audit id {a.get('outcome_id')}, "
                         f"intent {a.get('intent_id')}, in tenant {a.get('tenant')}.")
        elif a:
            lines.append(f"  The outcome row could not be written either; the intent row is gc_audit id "
                         f"{a.get('intent_id')} in tenant {a.get('tenant')}.")
        lines.append("  Re-running the same command is safe: the sweep takes only the rows that are still there.")
        lines.extend(_audit_lines(audits or []))
        lines.append(_READ_AUDIT_HINT)
        _fail("\n".join(lines), EXIT_AUDIT)
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
        a = _audit_line("refused", body.get("audit"))
        if a:
            lines.append(a)
        _fail("\n".join(lines + _audit_lines(audits or [])), EXIT_PRECONDITION)
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
    if pre.get("null_span_rows"):
        click.echo(
            "    null_span_rows: run `nx upgrade` (the manifest-span-fill rung) to copy each chunk row's span onto the "
            "manifest rows it is exact for; its summary names the rows that stay, and says which of them lose a "
            "recorded span. A chunk whose span keys hold 0 stays in this count with nothing to copy."
        )


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


def _dry_run_progress() -> Any:
    """A progress sink for the dry run job: one stderr line when the engine's stage changes, and one a minute while it
    stays on a stage, so a long count shows it is alive without a line per poll."""
    state: dict[str, Any] = {"stage": None, "printed_ms": 0}

    def note(event: dict) -> None:
        kind = event.get("event")
        if kind == "synchronous":
            click.echo("  This engine predates the background dry run: counted in the one request.", err=True)
        elif kind in ("started", "restarted"):
            click.echo(f"  dry run {'restarted' if kind == 'restarted' else 'started'} as engine job "
                       f"{event.get('job_id')}", err=True)
        elif kind == "poll":
            stage, elapsed = event.get("stage"), int(event.get("elapsed_ms") or 0)
            if stage != state["stage"] or elapsed - state["printed_ms"] >= 60_000:
                click.echo(f"  dry run {event.get('state')}: {stage} ({elapsed / 1000:.0f} s)", err=True)
                state["stage"], state["printed_ms"] = stage, elapsed

    return note


def _run_sweep(client: Any, *, tenant: str, apply: bool, accept: dict[str, int], collection: str | None,
               batch_size: int | None, as_json: bool, operator: str | None = None,
               wait_seconds: int | None = None) -> None:
    audits: list[tuple[str, dict]] = []
    try:
        report = client.chunk_keys_dry_run(
            accept=accept, collection=collection, batch_size=batch_size,
            measure_batch=None if not apply else False, tenant=tenant, operator=operator,
            progress=_dry_run_progress(), **({} if wait_seconds is None else {"max_wait_s": float(wait_seconds)}),
        )
    except VectorServiceError as exc:
        _engine_error(exc, audits=audits)
    if report.get("tenant") != tenant:
        _old_engine_tenant(tenant, report.get("tenant"))
    if isinstance(report.get("audit"), dict):
        audits.append(("dry run", report["audit"]))
    out: dict[str, Any] = {"tenant": tenant, "dry_run": not apply, "report": report}
    if not as_json:
        _render_dry_run(report, tenant=tenant)
        if not apply:
            for ln in _audit_lines(audits):
                click.echo(ln)
    if not apply:
        if as_json:
            click.echo(json.dumps(out, indent=2, default=str))
        else:
            click.echo("Dry run: nothing written. Re-run with --apply.")
        return
    if report.get("blocking"):
        if as_json:
            out["audit"] = [{"call": label, **a} for label, a in audits]
            click.echo(json.dumps(out, indent=2, default=str))
        else:
            for ln in _audit_lines(audits):
                click.echo(ln)
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
                max_batches=_MAX_BATCHES, budget_seconds=_BUDGET_SECONDS, resume=resume, tenant=tenant,
                operator=operator,
            )
        except VectorServiceError as exc:
            _engine_error(exc, swept_before=swept, audits=audits)
        if last.get("tenant") != tenant:
            _old_engine_tenant(tenant, last.get("tenant"))
        calls += 1
        if isinstance(last.get("audit"), dict):
            audits.append((f"call {calls}", last["audit"]))
        n = int(last.get("rows_swept", 0))
        swept += n
        if calls == 1:
            click.echo(f"  first batch: {last.get('first_batch_ms')} ms", err=True)
        locked = int(last.get("locked_skipped_rows") or 0)
        click.echo(
            f"  call {calls}: swept {n}, {last.get('remaining_rows')} left"
            f"{f' ({locked} skipped: locked by a writer)' if locked else ''}"
            f"{' (done)' if last.get('done') else ''}"
            f"{' (resume not accepted: the engine repeated the checks)' if last.get('resume_ignored') else ''}",
            err=True,
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
    out["audit"] = [{"call": label, **a} for label, a in audits]
    if as_json:
        click.echo(json.dumps(out, indent=2, default=str))
        return
    click.echo(f"Swept {swept} rows in {calls} calls; {last.get('remaining_rows')} left.")
    for ln in _audit_lines(audits):
        click.echo(ln)
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


def _is_timeout(exc: BaseException) -> bool:
    """A socket timeout, bare or wrapped by urllib (``URLError.reason``)."""
    return isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError)


def _run_constraints(client: Any, *, add: bool, replace: bool, as_json: bool) -> None:
    try:
        result = client.chunk_keys_add_constraints(replace=replace) if add else client.chunk_keys_validate_constraints()
    except VectorServiceError as exc:
        _engine_error(exc)
    except (TimeoutError, urllib.error.URLError) as exc:
        if not _is_timeout(exc):
            raise
        # The socket gave up; the engine does not. Say what that means rather than leave the operator to guess.
        if add:
            _fail(
                "The add request timed out. The engine may still be running it, and the add may have landed (the "
                "constraints go in one statement at a time and none is rolled back). Wait a minute, then rerun the "
                "same command: an add is idempotent, a constraint that exists is left alone and the report says what "
                "is there.", EXIT_FAILED,
            )
        _fail(
            "The validate request timed out. The engine may still be running the VALIDATE (it keeps going after the "
            "socket closes). Wait, then rerun the same command: a constraint that is already valid is skipped.",
            EXIT_FAILED,
        )
    if as_json:
        click.echo(json.dumps(result, indent=2, default=str))
        return
    _render_constraints(result, add=add)


@click.command("retire-keys")
@click.option("--tenant", metavar="ID", default=None,
              help="The tenant to sweep. Required for the sweep. The root credential may name any registered tenant; "
                   "another credential may name only its own. The verb checks the engine's answer names this tenant, "
                   "and refuses (exit 4) an engine too old to sweep another tenant.")
@click.option("--operator", "operator", metavar="LABEL", default=None,
              help="Who is running this, recorded with the engine's audit rows of a root-credential sweep (one shared "
                   "root token names no person). At most 80 printable characters.")
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
@click.option("--wait-seconds", "wait_seconds", type=click.IntRange(1, 86400), default=None, metavar="N",
              help="How long to wait for the dry run before giving up on waiting (default 1800). The count runs as a "
                   "background job in the engine, because a large tenant's takes longer than the public edge allows "
                   "one request; giving up leaves the job running, and running the same command again joins it.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit one JSON document.")
def retire_keys_cmd(tenant: str | None, operator: str | None, dry_run: bool, apply: bool, accept: tuple[str, ...], collection: str | None,
                    batch_size: int | None, max_rps: float | None, add_check: bool, replace: bool, validate: bool,
                    wait_seconds: int | None, as_json: bool) -> None:
    """Retire the chunk-row metadata keys: sweep a tenant, then add and validate the CHECKs (nexus-6pfgb.43).

    \b
    Order, per install:
      1. nx t3 retire-keys --tenant ID            dry run: counts, preconditions, the batch SELECT's time
      2. nx t3 retire-keys --tenant ID --apply    sweep the tenant (repeat for every tenant)
      3. nx t3 retire-keys --add-check --apply    CHECKs NOT VALID (refused while a tenant is not ready)
      4. nx t3 retire-keys --validate --apply     VALIDATE (refused while a tenant breaks one)

    Every sweep the ROOT credential makes is recorded by the engine in the target tenant's gc_audit (an intent row
    before the sweep reads or changes any chunk row, an outcome row after); the verb prints the row ids and the actor.
    gc_audit has row-level security keyed on the tenant: read the rows with a credential bound to that tenant
    (nx catalog gc-audit list --operation retire_keys_sweep --json --limit N), or in SQL as a BYPASSRLS or superuser
    role, or after SET nexus.tenant = '<tenant>'. A tenant with no live tenant-bound credential is read through SQL.

    The dry run is the default and writes nothing, locks nothing and runs no UPDATE; its time is the batch SELECT
    only, so the projected duration is a lower bound. Apply refuses while a precondition the engine counts is
    non-zero and not accepted up to its count (--accept NAME=N). The sweep never touches a quarantine- or taxonomy
    collection, a row with a wrong-width chash, a row whose metadata is not an object, or a collection whose
    manifest-less census reads legacy-unmanifested above zero (or did not finish). DATA EFFECT of apply: each swept
    row loses its retired keys; nothing else on the row changes, and last_written_at is kept. DATA EFFECT of
    --add-check: the database then refuses a write that carries a key outside the allow-list, an unequal hash or
    model, or a quarantined_by without quarantined_at; and, because a NOT VALID CHECK is evaluated on every new row
    version, ANY update of a row that still holds a retired key (the rows the sweep leaves in a blocked collection,
    which the output names) until it is fixed. Exit codes: 0 ok; 1 a failed call; 2 usage, or a tenant the engine
    refuses; 3 a precondition; 4 an engine without the routes, or too old to sweep another tenant; 5 a stalled apply;
    6 the sweep ran but the engine could not write its audit row (rows were changed), or failed part-way.
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
        if operator:
            _fail("--operator labels the per-tenant sweep's audit rows; --add-check and --validate record none.",
                  EXIT_USAGE)
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
                       batch_size=batch_size, as_json=as_json, operator=operator, wait_seconds=wait_seconds)
