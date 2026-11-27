# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx collection delete``'s body, by label or by ``--id`` (RDR-229 P3.12, nexus-p2m1x.32), and the 409 ``collection_busy`` wording
``delete``, ``prune`` and ``reindex`` share. Moved out of ``commands/collection.py`` (over its size ceiling); the Click command stays
there. The wire and the typed refusals are :mod:`nexus.catalog.engine_writes.collection_delete`."""
from __future__ import annotations

import shlex
from collections.abc import Callable
from typing import Any

import click


def collection_busy_message(busy: dict[str, str], retry: str) -> str:
    """What to tell a person whose delete the engine refused with 409 ``collection_busy`` (engine 0.1.202).

    One wording for ``delete``, ``prune`` and ``reindex``. The fence holds the collection while a job is planned,
    running, paused, cutover or failed, and the way out depends on which: a running or cutover job ends on its own,
    a paused one needs ``--resume`` or ``--abort``, a failed one ``--abort`` (``--resume`` retries it). The engine
    names the state in its text, not in a field; when it is not one the fence holds, the engine's own message goes
    beside the generic advice. *retry* is the command to run again once the job has ended."""
    name, job, state, said = busy["collection"], busy.get("job_id", ""), busy.get("state", ""), busy.get("message", "")
    job_arg = shlex.quote(job) if job else "JOB"
    status, abort, resume = (f"`nx collection re-embed --{v} {job_arg}`" for v in ("status", "abort", "resume"))
    head = (f"re-embed job {job or '(unnamed)'}{f' ({state})' if state else ''} holds collection '{name}' as its source "
            f"or its target, so nothing was deleted.")
    advice = {
        "running": f"Wait for it to finish ({status}), or cancel it ({abort}; documents already moved stay moved)",
        "cutover": f"Wait for it to finish ({status}), or cancel it ({abort}; documents already moved stay moved)",
        "planned": f"Wait for it to start and finish ({status}), or cancel it ({abort})",
        "paused": f"Resume it and let it finish ({resume}), or cancel it ({abort}; documents already moved stay moved)",
        "failed": f"Cancel it ({abort}; documents already moved stay moved); {resume} retries it",
    }.get(state)
    if advice is None:
        advice = (f"The engine said: {said or 'no detail'}. Check the job ({status}); wait for it to finish, or cancel it "
                  f"({abort}; documents already moved stay moved)")
    return f"{head} {advice}, then run `{retry}` again."


def _unreadable(why: str) -> None:
    """Say, on stderr, that the label pre-check could not run. Fail-open stays (the engine decides, and a T3 orphan with no registry row
    is not in the listing), but never silently: every way the listing cannot be had comes here."""
    click.echo(f"note: the registry listing could not be read ({why}); deleting without the superseded-label and label-clash checks",
               err=True)


def _registry_rows(cat: Any) -> list[dict[str, Any]] | None:
    """Every registry row (all lifecycle states) for the label pre-check, or None, said on stderr, when the listing cannot be read."""
    try:
        return list(cat.list_collections())
    except Exception as exc:  # noqa: BLE001 — boundary: the pre-check is advisory, the delete route decides
        _unreadable(f"{type(exc).__name__}: {exc}")
        return None


def run_delete(t3: Callable[[], Any], name: str | None, collection_id: int | None, yes: bool) -> None:
    """The delete of one collection by its label *name* or by *collection_id*: exactly one. *t3* is the command module's handle factory."""
    from nexus.catalog.engine_writes.collection_delete import (  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
        CollectionDeleteRefusedError, check_label_deletable, require_delete_by_id)
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    if (name is None) == (collection_id is None):
        raise click.UsageError("give either a label or --id, not both and not neither: `nx collection delete LABEL` or "
                               "`nx collection delete --id N`")
    by_id = collection_id is not None
    retry = f"nx collection delete --id {collection_id}" if by_id else f"nx collection delete {shlex.quote(name or '')}"
    # What the refusal that is certain says comes before the question: the engine's flag for an id, the registry for a label.
    reader_failed = False
    try:
        cat = make_catalog_reader()
    except Exception as exc:  # noqa: BLE001 — a label delete goes on to the cascade, which reports the unreachable service itself
        if by_id:
            raise click.ClickException(f"the catalog service is unreachable, so `--id` cannot be sent ({type(exc).__name__}: {exc})") from exc
        _unreadable(f"{type(exc).__name__}: {exc}")
        cat, reader_failed = None, True
    try:
        if by_id:
            if cat is None:
                raise click.ClickException("the catalog service client is unavailable, so `--id` cannot be sent")
            require_delete_by_id(cat)
        elif cat is None:
            if not reader_failed:
                _unreadable("no catalog service client")
        elif (rows := _registry_rows(cat)) is not None:
            check_label_deletable(rows, name or "")
    except CollectionDeleteRefusedError as refused:
        raise click.ClickException(str(refused)) from refused
    if not yes:
        what = f"the collection with id {collection_id}" if by_id else f"collection '{name}'"
        click.confirm(f"Delete {what}? This cannot be undone.", abort=True)

    # nexus-jm3z / nexus-8a8e / nexus-vxz3: the full delete cascade (T3 +
    # taxonomy + chash + pipeline + catalog docs + projection) is the
    # canonical ``purge_collection_cascade`` (RDR-144 nexus-prgf4), shared
    # with the 384->768 migration so both leave the same clean state.
    # nexus-lub: a T3 delete may find the collection already absent (a prior
    # half-delete); the cascade still runs to clean the orphan rows.
    from nexus.db.collection_purge import purge_collection_cascade  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    cascade = purge_collection_cascade(t3(), name or "", collection_id=collection_id)
    label = cascade.name if by_id else (name or "")
    if cascade.t3_absent:
        click.echo(f"note: T3 collection '{label}' already absent — running cascade anyway", err=True)
    if cascade.collection_busy is not None:
        # RDR-229 P3.5a row 14 (engine 0.1.202): the engine refused with 409 collection_busy; nothing was removed.
        raise click.ClickException(collection_busy_message(cascade.collection_busy, retry))
    if cascade.refusal is not None:
        # RDR-229 P3.12: the engine's typed refusal (unknown id, superseded, label clash); nothing was removed.
        raise click.ClickException(str(cascade.refusal))
    # Surface any cascade-step failure to the user (orphan rows may remain).
    for failure in cascade.failures:
        click.echo(f"warn: {failure}", err=True)
    if cascade.service_delete_failed:
        # nexus-oq5gd: the service never confirmed the delete (a read timeout
        # makes ONE attempt, so it may or may not have committed). Do not
        # claim "Deleted:".
        what = f"collection id {collection_id}" if by_id else f"'{name}'"
        raise click.ClickException(f"delete of {what} is unconfirmed: the service cascade failed. Re-run `{retry}`.")

    from nexus.catalog.chunk_quarantine import is_quarantine_sibling_name  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    # The one sanctioned name parser for the quarantine flag (no new raw parse sites, RDR-204).
    is_quarantine_name = is_quarantine_sibling_name(label)
    taxonomy_counts = cascade.taxonomy
    parts: list[str] = []
    # For a quarantine-* name the engine reports the rows it held under both keys; say them once, below.
    if cascade.chunks_deleted and not is_quarantine_name:
        parts.append(f"{cascade.chunks_deleted} chunks")
    if taxonomy_counts and any(taxonomy_counts.values()):
        parts.append(
            f"{taxonomy_counts['topics']} topics, "
            f"{taxonomy_counts['assignments']} assignments, "
            f"{taxonomy_counts['links']} links, "
            f"{taxonomy_counts['meta']} meta"
        )
    if cascade.chash_deleted:
        parts.append(f"{cascade.chash_deleted} chash rows")
    if cascade.pipeline_rows_deleted:
        parts.append(f"{cascade.pipeline_rows_deleted} pipeline rows")
    if cascade.catalog_docs_deleted:
        parts.append(f"{cascade.catalog_docs_deleted} catalog docs")
    if cascade.catalog_projection_deleted:
        parts.append(f"{cascade.catalog_projection_deleted} catalog projection row")
    if cascade.quarantine_chunks_deleted:
        # nexus-wbfpw.68/.71: the origin's quarantine rows go with it, audited per sibling.
        audit_op = "quarantine_collection_delete" if is_quarantine_name else "collection_delete_quarantine"
        parts.append(f"{cascade.quarantine_chunks_deleted} quarantined chunks (gc_audit: {audit_op})")
    if by_id:
        parts.insert(0, f"id {collection_id}")
    click.echo(f"Deleted: {label} ({'; '.join(parts)})" if parts else f"Deleted: {label}")
