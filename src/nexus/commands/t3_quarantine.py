# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx t3 quarantine`` — operate on the engine's ``quarantine-*`` collections.

``nx t3 quarantine restore`` (RDR-192 Step 9 Day-2, bead nexus-2x9xa, Sam's ruling
2026-10-01) moves chunks from a collection's ``quarantine-`` sibling back to the
collection, through the engine route ``POST /v1/vectors/gc/quarantine-restore``
(``nexus.quarantine_restore_chunks``, changeset ``vectors-025``).

Why a verb of its own: until now the only way out of quarantine was
``gc_restore_rereferenced``, which restores a chunk only when the origin's manifest
names it again. A chunk the reaper moved WRONGLY has no manifest row by definition, so
the only recovery was hand SQL from the ``gc_audit`` chash list.

The chunks are named in one of three ways, because the places that record what was
quarantined differ in completeness: explicit ``--chash`` values, a ``--audit-id`` (the
chash list of a ``reaper_quarantine`` row; a ``gc_quarantine_orphans`` row lists only a
sample and the engine refuses it), or a ``--quarantined-since`` / ``--quarantined-before``
window over the sibling itself, which covers every quarantine however it was made.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import UTC, datetime
from typing import Any, NoReturn

import click

#: Most chashes one engine call takes (``VectorHandler.MAX_QUARANTINE_RESTORE_PER_CALL``).
_BATCH = 1000

#: Exit codes. 1 is a restore that left a requested chunk unrestored for a reason the operator
#: should look at (missing, or an embedding-width conflict). 4 and 5 mirror
#: ``nx t3 census-manifest-less``: the engine predates the route, or answered with an error.
EXIT_UNRESTORED = 1
EXIT_NO_ROUTE = 4
EXIT_ENGINE_ERROR = 5

#: Rows printed in the table before the rest are summarised (``--json`` carries every row).
_TABLE_ROWS = 100

_CHASH_RE = re.compile(r"^[0-9a-f]{64}$")

_OUTCOMES = ("restored", "would_restore", "present", "dim_conflict", "missing")


def _make_t3():
    """The T3 client (``HttpVectorClient`` in every mode). Patched in tests."""
    from nexus.db import make_t3  # noqa: PLC0415 — command-local import deferred to avoid CLI startup cost (nexus.db)

    return make_t3()


def _quarantine_name(origin: str) -> str:
    """The origin's quarantine sibling, from its registered catalog row, never a name parse
    (RDR-204; ``chunk_quarantine.quarantine_collection_name``). Patched in tests."""
    from nexus.catalog.chunk_quarantine import quarantine_collection_name  # noqa: PLC0415 — command-local import deferred to avoid CLI startup cost (nexus.catalog.chunk_quarantine)

    return quarantine_collection_name(origin)


def _instant(option: str, raw: str | None) -> str | None:
    """*raw* as the ``YYYY-MM-DDTHH:MM:SSZ`` the engine parses. A date or a datetime; a naive
    value is UTC."""
    if raw is None:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise click.BadParameter(
            f"{raw!r} is not an ISO-8601 date or datetime (for example 2026-09-01 or 2026-09-01T12:00:00Z)",
            param_hint=option,
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _actor() -> str:
    try:
        import getpass  # noqa: PLC0415 — only this verb needs it

        return f"nx t3 quarantine restore ({getpass.getuser()})"
    except Exception:  # noqa: BLE001 — a missing login name must not stop a restore
        return "nx t3 quarantine restore"


def _fail(message: str, code: int) -> NoReturn:
    click.echo(message, err=True)
    sys.exit(code)


def _pages(client: Any, origin: str, sibling: str, *, chashes: list[str], audit_id: int | None,
           since: str | None, before: str | None, dry_run: bool, actor: str):
    """Yield the engine's answer page by page for whichever source was named."""
    common = {"dry_run": dry_run, "actor": actor}
    if chashes:
        for start in range(0, len(chashes), _BATCH):
            yield client.gc_quarantine_restore(origin, sibling, chashes=chashes[start:start + _BATCH], **common)
    elif audit_id is not None:
        offset = 0
        while True:
            page = client.gc_quarantine_restore(
                origin, sibling, audit_id=audit_id, offset=offset, limit=_BATCH, **common)
            yield page
            nxt = (page.get("source") or {}).get("next_offset")
            if nxt is None:
                return
            offset = int(nxt)
    else:
        after: str | None = None
        while True:
            page = client.gc_quarantine_restore(
                origin, sibling, quarantined_since=since, quarantined_before=before,
                after_chash=after, limit=_BATCH, **common)
            yield page
            after = page.get("next_after")
            if not after:
                return


def _render_text(origin: str, sibling: str, dry_run: bool, rows: list[dict], totals: dict[str, int],
                 audit_ids: list[int], source: dict | None, earliest: str | None) -> None:
    if dry_run:
        click.echo(f"Dry run: nothing moved. Would restore from {sibling} into {origin}.")
    else:
        click.echo(f"Restore from {sibling} into {origin}.")
    if source:
        click.echo(f"Source: gc_audit {source.get('audit_id')} ({source.get('operation')}, "
                   f"{source.get('chash_count')} chashes).")
    if rows:
        click.echo("")
        click.echo(f"{'CHASH':<64}  {'OUTCOME':<13}  NOTE")
        for row in rows[:_TABLE_ROWS]:
            note = ""
            if row["outcome"] == "restored" and row.get("no_manifest"):
                note = f"no manifest row; reapable again {row.get('reapable_after')}"
            elif row["outcome"] == "present":
                note = "already in the collection; left alone"
            elif row["outcome"] == "dim_conflict":
                note = "collection row has another embedding width; left alone"
            elif row["outcome"] == "missing":
                note = "not in the quarantine collection"
            click.echo(f"{row['chash']}  {row['outcome']:<13}  {note}".rstrip())
        if len(rows) > _TABLE_ROWS:
            click.echo(f"... {len(rows) - _TABLE_ROWS} more rows (use --json for every row).")
        click.echo("")
    lead = "would restore" if dry_run else "restored"
    n = totals["would_restore"] if dry_run else totals["restored"]
    click.echo(f"{lead} {n}, present {totals['present']}, dim_conflict {totals['dim_conflict']}, "
               f"missing {totals['missing']}")
    if audit_ids:
        click.echo("gc_audit " + ", ".join(str(a) for a in audit_ids)
                   + "  (nx catalog gc-audit list --operation quarantine_restore)")
    if earliest:
        click.echo(
            "\nThese chunks have no manifest row. The engine reaper will quarantine them again on or after "
            f"{earliest} (30 days after the restore) unless an owner row is repaired first: "
            "re-index or re-put the document that owns them, or run `nx t3 backfill-manifest`.")


@click.group("quarantine")
def quarantine_group() -> None:
    """Operate on ``quarantine-*`` collections (RDR-192)."""


@quarantine_group.command("restore")
@click.option("--collection", "-c", "collection", required=True, metavar="NAME",
              help="The collection the chunks were quarantined FROM (not the quarantine- sibling).")
@click.option("--chash", "chashes", multiple=True, metavar="HEX",
              help="A chunk to restore, 64 lowercase hex characters; repeatable.")
@click.option("--audit-id", "audit_id", type=int, default=None, metavar="N",
              help="Restore the chashes a gc_audit row lists (find it with "
              "`nx catalog gc-audit list --operation reaper_quarantine`). A gc_quarantine_orphans row "
              "lists only a sample and is refused: use the window options for those.")
@click.option("--quarantined-since", "since", default=None, metavar="WHEN",
              help="Restore the chunks quarantined from this collection at or after WHEN "
              "(ISO-8601 date or datetime, UTC when naive).")
@click.option("--quarantined-before", "before", default=None, metavar="WHEN",
              help="Restore the chunks quarantined from this collection before WHEN.")
@click.option("--dry-run", is_flag=True, default=False,
              help="Report what would be restored; move nothing and write no audit row.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit one JSON document.")
def restore_cmd(collection: str, chashes: tuple[str, ...], audit_id: int | None, since: str | None,
                before: str | None, dry_run: bool, as_json: bool) -> None:
    """Restore chunks from a collection's quarantine sibling.

    \b
    Moves the chunks back in one engine statement, under the exclusive sweep
    gate, with no manifest row required: this is the way back for a chunk the
    engine reaper quarantined wrongly (a manifest defect), which has no manifest
    row for ``gc_restore_rereferenced`` to find. Name the chunks one of three ways:
      --chash HEX ...                           explicit chashes
      --audit-id N                              the chash list of a gc_audit row
      --quarantined-since / --quarantined-before   a window over the sibling

    \b
    A chunk the collection already holds is left alone and reported present
    (never overwritten); a chash that is nowhere reports missing. A restored
    chunk has no manifest row, so it starts a fresh 30 day grace and the reaper
    takes it again at the end of it unless an owner row is repaired first: the
    output says on what date. Every restore writes one gc_audit row
    (operation quarantine_restore) with the full chash list. Use --dry-run first.

    \b
    Exit codes:
      0  every requested chunk was restored or was already present.
      1  a requested chunk is missing from quarantine, or its embedding width
         conflicts with the collection's row.
      2  a bad option (nothing was sent).
      4  the connected engine predates the restore route; upgrade it.
      5  the engine refused the request or failed (the message says why).

    \b
    Examples:
      nx t3 quarantine restore -c knowledge__x__voyage-context-3__v1 --chash 3f...a9 --dry-run
      nx t3 quarantine restore -c knowledge__x__voyage-context-3__v1 --audit-id 4125
      nx t3 quarantine restore -c docs__x__voyage-context-3__v1 --quarantined-since 2026-09-28
    """
    named = [bool(chashes), audit_id is not None, since is not None or before is not None]
    if sum(named) != 1:
        raise click.UsageError(
            "Name exactly one source: --chash, --audit-id, or --quarantined-since / --quarantined-before.")
    normalised: list[str] = []
    for value in chashes:
        if not _CHASH_RE.match(value):
            raise click.BadParameter(f"{value!r} is not 64 lowercase hex characters", param_hint="--chash")
        if value not in normalised:
            normalised.append(value)
    since_iso = _instant("--quarantined-since", since)
    before_iso = _instant("--quarantined-before", before)
    if since_iso and before_iso and since_iso >= before_iso:
        raise click.UsageError("--quarantined-since must be earlier than --quarantined-before.")

    from nexus.db.http_vector_client import VectorServiceError  # noqa: PLC0415 — command-local import deferred to avoid CLI startup cost (nexus.db.http_vector_client)

    sibling = _quarantine_name(collection)
    client = _make_t3()
    rows: list[dict] = []
    totals = dict.fromkeys(_OUTCOMES, 0)
    audit_ids: list[int] = []
    source: dict | None = None
    try:
        for page in _pages(client, collection, sibling, chashes=normalised, audit_id=audit_id,
                           since=since_iso, before=before_iso, dry_run=dry_run, actor=_actor()):
            rows.extend(page.get("rows") or [])
            for outcome in _OUTCOMES:
                totals[outcome] += int(page.get(outcome) or 0)
            if page.get("audit_id") is not None:
                audit_ids.append(int(page["audit_id"]))
            if page.get("source"):
                source = {**(source or {}), **page["source"]}
    except VectorServiceError as exc:
        if exc.code == 404:
            _fail("This engine does not carry the quarantine-restore route (RDR-192 Step 9, bead "
                  "nexus-2x9xa): the connected engine predates it. Upgrade the engine (compare its "
                  "version against REQUIRED_ENGINE_VERSION in src/nexus/engine_version.py).",
                  EXIT_NO_ROUTE)
        _fail(f"quarantine restore refused by the engine: {exc}", EXIT_ENGINE_ERROR)

    reapable = sorted(r["reapable_after"] for r in rows
                      if r.get("outcome") == "restored" and r.get("no_manifest") and r.get("reapable_after"))
    earliest = reapable[0] if reapable else None
    unrestored = totals["missing"] + totals["dim_conflict"]

    if as_json:
        click.echo(json.dumps({
            "origin_collection": collection,
            "quarantine_collection": sibling,
            "dry_run": dry_run,
            "source": source,
            "totals": totals,
            "audit_ids": audit_ids,
            "reapable_again_after": earliest,
            "rows": rows,
        }, indent=2))
    else:
        _render_text(collection, sibling, dry_run, rows, totals, audit_ids, source, earliest)
    if unrestored:
        sys.exit(EXIT_UNRESTORED)
