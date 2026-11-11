# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx t3 validate-chash-checks`` and ``nx t3 repair-chash`` (nexus-f3yqd).

The engine validates the chash width CHECKs on ``nexus.chunks`` and ``nexus.catalog_document_chunks`` only when a user
starts its job (engine 0.1.190; Sam 2026-11-11: never at boot). ``validate-chash-checks`` starts it and polls it to the
end, or polls a job already started (``--job``). The job is safe to stop: a CHECK it validated stays valid, and the next
run skips it. It needs the engine's operator (root) credentials, so on a managed install the operator runs it.

``repair-chash`` is the next step a refused manifest CHECK names: it deletes a document's 16-byte manifest rows and
marks the document for re-index, through the engine. A dry run (the default) counts and changes nothing; ``--apply``
writes. Name one document, or ``--all`` for every document with a bad row (up to ``--limit``).

Exit codes: 0 every CHECK validated (or a dry run with nothing refused); 1 a CHECK refused, deferred or the job failed;
2 usage; 3 the engine cannot run it (too old, or these credentials cannot).
"""

from __future__ import annotations

import json
from typing import Any, NoReturn

import click

from nexus.db import chash_constraints_job as job

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
@click.option("--job", "job_id", default=None, metavar="ID", help="Poll a job already started instead of starting one.")
@click.option("--no-wait", "no_wait", is_flag=True, default=False, help="Start the job, print its id and return.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the job's final report as one JSON document.")
def validate_chash_checks_cmd(dry_run: bool, job_id: str | None, no_wait: bool, as_json: bool) -> None:
    """Validate the chash width CHECKs: start the engine's job and wait for its verdict."""
    if job_id and (dry_run or no_wait):
        _fail("--job polls a job already started: --dry-run and --no-wait do not apply to it", EXIT_USAGE)
    client = _client()
    try:
        try:
            if job_id is None:
                started = job.start(client, dry_run=dry_run)
                job_id = started["job_id"]
                if not as_json:
                    click.echo(f"Validate job {job_id} {'started' if started.get('created') else 'already running'}"
                               f"{' (dry run)' if dry_run else ''}. It is safe to stop; a validated CHECK stays valid.")
                if no_wait:
                    click.echo(f"Poll it with: nx t3 validate-chash-checks --job {job_id}")
                    return
            body = job.wait(client, job_id, on_progress=None if as_json else lambda b: click.echo(job.progress_line(b)))
        except job.ValidateJobError as exc:
            _fail(str(exc), EXIT_CANNOT)
    finally:
        client.close()
    verdict = job.verdict(body)
    if as_json:
        click.echo(json.dumps(body, indent=2, default=str))
    else:
        for line in verdict.lines:
            click.echo(f"  {line}")
        click.echo(verdict.summary)
    raise SystemExit(EXIT_OK if verdict.ok else EXIT_NOT_VALID)


@click.command("repair-chash")
@click.argument("doc_id", required=False)
@click.option("--all", "all_docs", is_flag=True, default=False, help="Every document with a bad manifest row.")
@click.option("--apply", "apply", is_flag=True, default=False,
              help="Delete the bad rows and mark the documents for re-index. Without it nothing is changed.")
@click.option("--limit", type=click.IntRange(1, 100_000), default=None, help="At most this many documents.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the engine's answer as one JSON document.")
def repair_chash_cmd(doc_id: str | None, all_docs: bool, apply: bool, limit: int | None, as_json: bool) -> None:
    """Remove a document's 16-byte manifest rows and mark it for re-index (the step a refused CHECK names)."""
    if bool(doc_id) == all_docs:
        _fail("name one document (its tumbler), or --all for every document with a bad row", EXIT_USAGE)
    client = _client()
    try:
        try:
            out = job.repair(client, doc_id=doc_id, dry_run=not apply, limit=limit)
        except job.ValidateJobError as exc:
            _fail(str(exc), EXIT_CANNOT)
    finally:
        client.close()
    if as_json:
        click.echo(json.dumps(out, indent=2, default=str))
        return
    for d in out["documents"]:
        marked = " and marked for re-index" if d.get("marked_for_reindex") else ""
        click.echo(f"  {d.get('doc_id')}: {d.get('bad_rows')} bad manifest row(s)"
                   f"{' removed' + marked if apply else ''}")
    capped = " (capped: more remain, raise --limit)" if out.get("capped") else ""
    if apply:
        click.echo(f"Repaired {out.get('count', len(out['documents']))} document(s){capped}. "
                   "Run `nx index repo` (or re-add the files) to re-index them, then `nx t3 validate-chash-checks`.")
    else:
        click.echo(f"{out.get('count', len(out['documents']))} document(s) would be repaired{capped}; "
                   "nothing was changed. Rerun with --apply to repair.")


def doctor_chash_fix(*, dry_run: bool) -> bool:
    """``nx doctor --fix``'s step: start and wait for the validate job when a chash CHECK is not validated.
    Returns False only when a run it started did not validate everything."""
    client = _client()
    try:
        return job.doctor_fix_lines(client, dry_run=dry_run, echo=click.echo)
    finally:
        client.close()
