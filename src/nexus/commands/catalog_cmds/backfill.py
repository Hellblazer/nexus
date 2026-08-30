# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Backfill verb family for the ``nx catalog`` group.

``backfill-owner-id`` — the one-time RDR-137 P1.5a migration that populated
``collections.owner_id`` on the LOCAL SQLite catalog — died with that catalog
(nexus-i711w terminal deletion): it wrote through a raw ``cat._db`` SQLite
handle by contract and already refused in service mode, which is now the only
mode.

``backfill-source-uri`` (nexus-poigc) is the live verb in this family: it
re-derives ``chroma://`` catalog ``source_uri`` values for filesystem-backed
collections (``rdr__``/``docs__``/``code__``) whose identity was minted by an
older ``uri_for`` that predated those prefixes routing to ``file://``. See
the command's own docstring for the mechanism and safety rails.
"""
from __future__ import annotations

import json

import click


@click.command(name="backfill-source-uri")
@click.option(
    "--apply", is_flag=True,
    help="Perform the rewrite (default: dry-run report only).",
)
@click.option(
    "--json", "as_json", is_flag=True,
    help="Emit JSON instead of the human-readable report.",
)
def backfill_source_uri_cmd(apply: bool, as_json: bool) -> None:
    """Re-derive ``chroma://`` catalog ``source_uri`` values (nexus-poigc).

    ``chroma://`` is ALSO the internal store_put-origin marker
    (``nexus.commands.catalog_cmds.reconcile_stale._STORE_PUT_URI_PREFIX``)
    used by ``knowledge__`` documents minted via ``nx store put`` / MCP
    ``store_put`` — that usage is unrelated to the retired ChromaDB
    dependency (RDR-155 P4b) and is left alone. This verb ONLY rewrites
    rows whose PHYSICAL COLLECTION routes to ``file://`` in
    ``nexus.aspect_readers.uri_for`` (the ``rdr__``/``docs__``/``code__``
    prefixes, ``FILE_ROUTED_PREFIXES``): those rows carry a ``chroma://``
    identity ONLY because they were minted by an older ``uri_for`` build
    that predated those prefixes routing to ``file://`` — migration
    residue, not a legitimate origin marker.

    For each candidate row, parses the path component out of the stored
    ``chroma://<collection>/<path>`` URI and re-derives through the
    CURRENT ``uri_for(collection, path)``. Refuses (reports, never
    writes) a row whose parsed path is not absolute — there is no
    ``repo_root`` to anchor a relative one at backfill time, and this
    verb never guesses at one.

    Always prints a per-collection census of EVERY ``chroma://`` row
    first — candidates (file-routed, rewritten under ``--apply``) vs.
    store_put-origin markers (left alone) — regardless of ``--apply``,
    so the number is explained rather than eyeballed.
    """
    from collections import Counter  # noqa: PLC0415 — command-local

    from nexus.aspect_readers import FILE_ROUTED_PREFIXES, uri_for  # noqa: PLC0415 — command-local
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible

    cat = _cat_cmd._get_catalog()

    chroma_rows = [
        d for d in cat.all_documents()
        if (getattr(d, "source_uri", "") or "").startswith("chroma://")
    ]

    by_collection = Counter(d.physical_collection for d in chroma_rows)
    census = {
        coll: {
            "count": n,
            "candidate": coll.startswith(FILE_ROUTED_PREFIXES),
        }
        for coll, n in sorted(by_collection.items())
    }

    candidates = [d for d in chroma_rows if d.physical_collection.startswith(FILE_ROUTED_PREFIXES)]

    updates: list[tuple[object, str]] = []
    refused: list[tuple[object, str]] = []
    for d in candidates:
        uri = d.source_uri or ""
        prefix = f"chroma://{d.physical_collection}/"
        if not uri.startswith(prefix):
            refused.append((d, f"malformed chroma:// URI: does not start with {prefix!r}"))
            continue
        path = uri[len(prefix):]
        if not path.startswith("/"):
            refused.append((d, f"parsed path is not absolute: {path!r}"))
            continue
        new_uri = uri_for(d.physical_collection, path)
        if not new_uri or new_uri.startswith("chroma://"):
            refused.append((d, f"uri_for did not re-derive a file:// URI (got {new_uri!r})"))
            continue
        updates.append((d, new_uri))

    report = {
        "total_chroma_rows": len(chroma_rows),
        "by_collection": census,
        "candidates": len(candidates),
        "rederivable": len(updates),
        "refused": len(refused),
        "apply": apply,
        "updates": [
            {"tumbler": str(d.tumbler), "collection": d.physical_collection,
             "was": d.source_uri, "now": new_uri}
            for d, new_uri in updates
        ],
        "refusals": [
            {"tumbler": str(d.tumbler), "collection": d.physical_collection,
             "source_uri": d.source_uri, "reason": reason}
            for d, reason in refused
        ],
    }

    if as_json:
        click.echo(json.dumps(report, indent=2, default=str))
    else:
        click.echo(f"chroma:// source_uri rows: {report['total_chroma_rows']}")
        for coll, info in census.items():
            tag = "candidate" if info["candidate"] else "store_put-origin marker (left alone)"
            click.echo(f"  {coll}: {info['count']}  [{tag}]")
        click.echo(
            f"Re-derivable: {report['rederivable']}, refused: {report['refused']}"
        )
        for d, reason in refused[:10]:
            click.echo(f"  REFUSED {d.tumbler} {d.source_uri!r}: {reason}")
        if len(refused) > 10:
            click.echo(f"  … {len(refused) - 10} more refusal(s) (--json for all)")

    if not updates:
        if not as_json:
            click.echo("No file-routed chroma:// rows to backfill.")
        return

    if not apply:
        if not as_json:
            click.echo(f"Dry-run: would rewrite {len(updates)} row(s). Re-run with --apply.")
            for d, new_uri in updates[:10]:
                click.echo(f"  {d.tumbler}  {d.source_uri!r} -> {new_uri!r}")
            if len(updates) > 10:
                click.echo(f"  … {len(updates) - 10} more (--json for all)")
        return

    writer = _cat_cmd._get_catalog_writer()
    try:
        payload = [
            {"tumbler": str(d.tumbler), "source_uri": new_uri}
            for d, new_uri in updates
        ]
        counts = writer.update_many(payload)
    finally:
        writer.close()
    updated = sum(1 for c in counts if c)
    click.echo(f"Rewrote {updated}/{len(updates)} source_uri value(s).")
    if updated != len(updates):
        raise click.exceptions.Exit(1)


def register(group: click.Group) -> None:
    """Attach the live backfill verb(s) to the shared ``catalog`` group."""
    group.add_command(backfill_source_uri_cmd)
