# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx catalog reconcile-fences`` — stamp index-run fences on non-repo
documents that carry none (GH #1512, nexus-kt7f4).

``nx doctor``'s "stale index-run fences" row names documents whose
``index_state`` was reported NULL although they were indexed after the
install's fence baseline. For a repo file the remedy is ``nx index <path>
--force``. A document of a non-repo owner (the knowledge store's curator
owner, ``1.1.*``) has no file path: it was written by ``nx store put`` /
``store_put`` before that path fenced its writes (nexus-cotmr, 7.3.x), or
by a bulk import in a migration window (998 rows on one install, all
inside one second). Nothing could stamp or clear them, so the warning was
permanent.

This command walks the catalog once, selects documents whose owner is not a
repo, whose ``index_state`` is reported NULL, and whose manifest is
non-empty, and runs the same begin + verify-then-stamp pair the fenced
write paths run (``index-run/begin`` then ``index-run/complete``). The
engine's complete route refuses a document whose manifest chunks are not
all present in T3, so nothing is stamped that is not actually whole.

The recorded ``index_content_hash`` is the single chunk's chash for a
one-chunk document (exactly what ``store put`` records) and, for a
multi-chunk document, the sha256 over its manifest chashes in order: not
the source file's hash, which this command cannot see, so the next real
index of such a document sees "changed" once, re-indexes, and records the
real value. Harmless and self-correcting; stated here so nobody reads the
column as a file hash.
"""

from __future__ import annotations

import hashlib
from uuid import uuid4

import click
import structlog

_log = structlog.get_logger(__name__)


def _content_hash_for(manifest: list) -> str:
    chashes = [getattr(row, "chash", None) or (row.get("chash") if isinstance(row, dict) else "") for row in manifest]
    chashes = [c for c in chashes if c]
    if len(chashes) == 1:
        return chashes[0]
    return hashlib.sha256("".join(chashes).encode("ascii")).hexdigest()


def reconcile_fences(reader, writer, *, dry_run: bool, limit: int, echo=click.echo) -> dict:
    """Walk, select, stamp. Returns the counts it printed."""
    from nexus.errors import IndexRunVerifyRefused  # noqa: PLC0415 — deferred: command-local

    owner_types = {
        str(o.get("tumbler_prefix", "")): str(o.get("owner_type", ""))
        for o in reader.list_owners(include_deactivated=True)
    }
    counts = {"candidates": 0, "stamped": 0, "refused": 0, "empty_manifest": 0, "skipped_repo": 0}
    for entry in reader.all_documents(limit=0):
        if not getattr(entry, "index_state_reported", True):
            continue
        if getattr(entry, "index_state", None) is not None:
            continue
        tumbler = str(getattr(entry, "tumbler", "") or "")
        prefix = ".".join(tumbler.split(".")[:2])
        if owner_types.get(prefix, "repo") == "repo":
            counts["skipped_repo"] += 1
            continue
        counts["candidates"] += 1
        if limit and counts["stamped"] + counts["refused"] >= limit:
            continue
        manifest = reader.get_manifest(tumbler)
        if not manifest:
            counts["empty_manifest"] += 1
            continue
        content_hash = _content_hash_for(manifest)
        collection = str(getattr(entry, "physical_collection", "") or "")
        if dry_run:
            echo(f"  would stamp {tumbler} ({collection}, {len(manifest)} chunk(s))")
            counts["stamped"] += 1
            continue
        try:
            writer.begin_index_run(tumbler, content_hash, uuid4().hex, collection)
            writer.complete_index_run(tumbler, content_hash, len(manifest))
        except IndexRunVerifyRefused as exc:
            counts["refused"] += 1
            echo(f"  refused {tumbler}: {exc}")
            _log.warning("reconcile_fences_refused", tumbler=tumbler, error=str(exc))
            continue
        counts["stamped"] += 1
    verb = "would stamp" if dry_run else "stamped"
    echo(
        f"{verb} {counts['stamped']} of {counts['candidates']} non-repo document(s) with no fence; "
        f"{counts['refused']} refused (manifest not whole in T3), "
        f"{counts['empty_manifest']} with an empty manifest left alone; "
        f"{counts['skipped_repo']} repo document(s) skipped (use `nx index <path> --force`)."
    )
    return counts


@click.command("reconcile-fences")
@click.option("--dry-run", is_flag=True, help="Name the documents that would be stamped; write nothing.")
@click.option("--limit", type=int, default=0, show_default=True,
              help="Stop after this many stamp attempts (0 = all).")
def reconcile_fences_cmd(dry_run: bool, limit: int) -> None:
    """Stamp index-run fences on non-repo documents that carry none (GH #1512).

    The remedy `nx doctor` names for its "stale index-run fences" row when
    the documents belong to the knowledge store's owner rather than a repo.
    """
    from nexus.catalog.factory import make_catalog_reader, make_catalog_writer  # noqa: PLC0415 — deferred: catalog only on this path

    reader = make_catalog_reader()
    if reader is None:
        raise click.ClickException("catalog not initialized (nx catalog setup)")
    writer = None if dry_run else make_catalog_writer()
    try:
        counts = reconcile_fences(reader, writer, dry_run=dry_run, limit=limit)
    finally:
        if writer is not None:
            writer.close()
    if counts["refused"]:
        raise click.ClickException(
            f"{counts['refused']} document(s) refused: their manifest chunks are not all present in T3; "
            "re-put them"
        )


def register(group: click.Group) -> None:
    """Attach ``reconcile-fences`` to the shared ``catalog`` group."""
    group.add_command(reconcile_fences_cmd)
