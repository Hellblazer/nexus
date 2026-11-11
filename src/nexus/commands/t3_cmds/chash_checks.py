# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx t3 validate-chash-checks`` and ``nx t3 repair-chash`` (nexus-f3yqd).

The engine validates the chash width CHECKs on ``nexus.chunks`` and ``nexus.catalog_document_chunks`` only when a user
starts its job (engine 0.1.190; Sam 2026-11-11: never at boot). ``validate-chash-checks`` starts it and polls it to the
end, or follows a job already started (``--job``). Stopping this command does not stop the job: it keeps running on the
engine. Stopping the engine stops it safely: a CHECK it validated stays valid, and the next run skips it. It needs the
engine's root credentials, so on a managed install the operator runs it.

``repair-chash`` is the step a refused manifest CHECK needs: the engine rekeys a document's 16-byte manifest rows
losslessly (it recomputes the 32-byte key from the stored chunk text, moves or merges the chunk, repoints the manifest
and drops the old row; Sam's choice), nothing to re-index. No released engine has that route yet; the verb says so. A dry run (the
default) counts and changes nothing; ``--apply`` writes.

Exit codes: 0 every CHECK validated (a dry run: nothing would be refused or deferred); 1 a CHECK refused or deferred,
the job failed or was lost, or the engine could not be reached (run it again); 2 usage; 3 this engine or these
credentials cannot run it.
"""

from __future__ import annotations

import json
from typing import Any, NoReturn

import click

from nexus.db.admin import chash_validate as job

EXIT_OK = 0
EXIT_NOT_VALID = 1
EXIT_USAGE = 2
EXIT_CANNOT = 3


def _client() -> Any:
    """A catalog client of its own (the shared handle is a proxy). Patched in tests."""
    from nexus.catalog.http_catalog_client import HttpCatalogClient  # noqa: PLC0415 — deferred: CLI startup cost

    return HttpCatalogClient()


def _fail(message: str, code: int) -> NoReturn:
    click.echo(message, err=True)
    raise SystemExit(code)


@click.command("validate-chash-checks")
@click.option("--dry-run", "dry_run", is_flag=True, default=False,
              help="Report what each CHECK would do (validate, refuse, defer) and change nothing.")
@click.option("--job", "job_id", default=None, metavar="ID", help="Follow a job already started instead of starting one.")
@click.option("--no-wait", "no_wait", is_flag=True, default=False, help="Start the job, print its id and return.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the job's report as one JSON document.")
def validate_chash_checks_cmd(dry_run: bool, job_id: str | None, no_wait: bool, as_json: bool) -> None:
    """Validate the chash width CHECKs: start the engine's job and wait for its verdict."""
    if job_id and (dry_run or no_wait):
        _fail("--job follows a job already started: --dry-run and --no-wait do not apply to it", EXIT_USAGE)
    client = _client()
    try:
        try:
            if job_id is None:
                started = job.start(client, dry_run=dry_run)
                job_id = started["job_id"]
                if no_wait:
                    click.echo(json.dumps(started) if as_json else
                               f"Validate job {job_id} {'started' if started.get('created') else 'already running'}. "
                               f"Follow it with: nx t3 validate-chash-checks --job {job_id}")
                    return
                if not as_json:
                    click.echo(f"Validate job {job_id} {'started' if started.get('created') else 'already running'}"
                               f"{' (dry run)' if dry_run else ''}. Stopping this command leaves it running; "
                               f"follow it with `nx t3 validate-chash-checks --job {job_id}`.")
            body = job.wait(client, job_id, on_progress=None if as_json else click.echo)
            repair_ready = job.repair_supported(client)
        except job.ValidateJobError as exc:
            _fail(str(exc), exc.exit_code)
        except KeyboardInterrupt:
            _fail(f"Stopped following. {job.poll_hint(job_id)}" if job_id else "Stopped.", EXIT_NOT_VALID)
    finally:
        client.close()
    verdict = job.verdict(body, repair_ready=repair_ready)
    if as_json:
        click.echo(json.dumps(body, indent=2, default=str))
    else:
        for line in verdict.lines:
            click.echo(f"  {line}")
        click.echo(verdict.summary)
    raise SystemExit(EXIT_OK if verdict.ok else EXIT_NOT_VALID)


@click.command("repair-chash")
@click.argument("doc_id", required=False)
@click.option("--all", "all_docs", is_flag=True, default=False,
              help="Every 16-byte manifest row of your tenant (narrow with --collection).")
@click.option("--collection", default=None, metavar="NAME", help="Only chunks in this collection.")
@click.option("--apply", "apply", is_flag=True, default=False,
              help="Rekey the rows. Without it the engine counts and nothing is changed.")
@click.option("--limit", type=click.IntRange(1, 1000), default=None,
              help="At most this many chunks per call (the engine's default is 100).")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the engine's answer as one JSON document.")
def repair_chash_cmd(doc_id: str | None, all_docs: bool, collection: str | None, apply: bool, limit: int | None,
                     as_json: bool) -> None:
    """Rekey 16-byte manifest rows to their 32-byte keys, losslessly (needs an engine with the route)."""
    if bool(doc_id) == all_docs:
        _fail("name one document (its tumbler), or --all for every 16-byte row of your tenant", EXIT_USAGE)
    client = _client()
    try:
        try:
            out = job.repair(client, doc_id=doc_id, collection=collection, dry_run=not apply, limit=limit)
        except job.ValidateJobError as exc:
            _fail(str(exc), exc.exit_code)
    finally:
        client.close()
    if as_json:
        click.echo(json.dumps(out, indent=2, default=str))
        return
    for line in job.rekey_lines(out):
        click.echo(line)


def doctor_chash_fix(*, dry_run: bool) -> bool:
    """``nx doctor --fix``'s step: start and wait for the validate job when a chash CHECK is not validated.
    Prints what it did and never raises; returns False when a run it started did not validate everything."""
    try:
        client = _client()
    except Exception as exc:  # noqa: BLE001 — doctor --fix's other steps go on; this one says why it could not run
        click.echo(f"chash CHECKs: could not reach the engine ({exc}); run `nx t3 validate-chash-checks` later")
        return False
    try:
        return job.doctor_fix_lines(client, dry_run=dry_run, echo=click.echo)
    finally:
        client.close()
