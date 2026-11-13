# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx t3 validate-chash-checks`` (nexus-f3yqd).

The engine validates the chash width CHECKs on ``nexus.chunks`` and ``nexus.catalog_document_chunks`` only when a user
starts its job (engine 0.1.190; Sam 2026-11-11: never at boot). ``validate-chash-checks`` starts it and polls it to the
end, or follows a job already started (``--job``). Stopping this command does not stop the job: it keeps running on the
engine. ``--job ID --cancel`` cancels it (Sam 2026-11-12): a CHECK it validated stays valid, and the next run skips it.
It needs the engine's root credentials, so on a managed install the operator runs it.

Exit codes: 0 every CHECK validated (a dry run: nothing would be refused or deferred), or ``--cancel`` saw the job end
cancelled; 1 a CHECK refused or deferred, the job failed, was cancelled (when not asked here) or was lost, or the
engine could not be reached (run it again); 2 usage; 3 this engine or these credentials cannot run it.
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
    """A catalog client of its own, from the factory (the shared handle is a proxy). Patched in tests."""
    from nexus.catalog.factory import make_catalog_admin_client  # noqa: PLC0415 — deferred: CLI startup cost

    return make_catalog_admin_client()


def _fail(message: str, code: int) -> NoReturn:
    click.echo(message, err=True)
    raise SystemExit(code)


@click.command("validate-chash-checks")
@click.option("--dry-run", "dry_run", is_flag=True, default=False,
              help="Report what each CHECK would do (validate, refuse, defer) and change nothing.")
@click.option("--job", "job_id", default=None, metavar="ID", help="Follow a job already started instead of starting one.")
@click.option("--no-wait", "no_wait", is_flag=True, default=False,
              help="Start (or with --cancel, cancel) the job, print its id and return without waiting.")
@click.option("--cancel", "cancel", is_flag=True, default=False,
              help="With --job: cancel that job and wait for it to end. A CHECK it validated stays valid.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the job's report as one JSON document.")
def validate_chash_checks_cmd(dry_run: bool, job_id: str | None, no_wait: bool, cancel: bool, as_json: bool) -> None:
    """Validate the chash width CHECKs: start the engine's job and wait for its verdict."""
    if cancel and (not job_id or dry_run):
        _fail("--cancel needs --job ID (the id `validate-chash-checks` printed) and takes no --dry-run", EXIT_USAGE)
    if job_id and not cancel and (dry_run or no_wait):
        _fail("--job follows a job already started: --dry-run and --no-wait do not apply to it", EXIT_USAGE)
    client = _client()
    body: dict[str, Any] | None = None
    try:
        try:
            if cancel and job_id:
                answered = job.cancel(client, job_id)
                if answered.get("state") != "running":
                    if not as_json:
                        click.echo(f"Validate job {job_id} had already ended; nothing to cancel.")
                    body = answered
                elif no_wait:
                    click.echo(json.dumps(answered) if as_json else
                               f"Cancelling validate job {job_id}. Follow it to its end with: "
                               f"nx t3 validate-chash-checks --job {job_id}")
                    return
                elif not as_json:
                    click.echo(f"Cancelling validate job {job_id}; waiting for it to end.")
            elif job_id is None:
                started = job.start(client, dry_run=dry_run)
                job_id = started["job_id"]
                what = "started" if started.get("created") else "already running"
                if no_wait:
                    click.echo(json.dumps(started) if as_json else
                               f"Validate job {job_id} {what}. {job.follow_or_cancel(job_id)}")
                    return
                if not as_json:
                    click.echo(f"Validate job {job_id} {what}{' (dry run)' if dry_run else ''}. Stopping this command "
                               f"leaves it running. {job.follow_or_cancel(job_id)}")
            if body is None and job_id:
                body = job.wait(client, job_id, on_progress=None if as_json else click.echo)
        except job.ValidateJobError as exc:
            _fail(str(exc), exc.exit_code)
        except KeyboardInterrupt:
            if cancel and job_id:
                _fail(f"Stopped following. The cancel was sent; follow job {job_id} to its end with "
                      f"`nx t3 validate-chash-checks --job {job_id}`.", EXIT_NOT_VALID)
            _fail(f"Stopped following. {job.poll_hint(job_id)}" if job_id else "Stopped.", EXIT_NOT_VALID)
    finally:
        client.close()
    assert body is not None  # every path above set it, returned or exited
    verdict = job.verdict(body, cancelled_here=cancel)
    if as_json:
        click.echo(json.dumps(body, indent=2, default=str))
    else:
        for line in verdict.lines:
            click.echo(f"  {line}")
        click.echo(verdict.summary)
    asked_and_done = cancel and job.was_cancelled(body)
    raise SystemExit(EXIT_OK if verdict.ok or asked_and_done else EXIT_NOT_VALID)


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
