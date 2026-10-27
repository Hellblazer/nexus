# SPDX-License-Identifier: AGPL-3.0-or-later
import fnmatch
import re
import sys
import time
from collections.abc import Callable
from typing import Any

import click
import structlog

from nexus.commands.store import _t3
from nexus.corpus import (
    CANONICAL_EMBEDDING_MODELS,
    LOCAL_EMBEDDING_MODELS,
    embedding_model_for_collection,
    index_model_for_collection,
)

_log = structlog.get_logger(__name__)


def _doc_id_to_file_path(doc_id: str) -> str:
    """nexus-7b5n: resolve a chunk's ``doc_id`` to the catalog's
    ``file_path``. Returns "" when the catalog is uninitialized or has
    no entry. Used by ``reindex_cmd``'s pre-delete safety check so
    post-prune chunks (which lack ``source_path``) still drive the
    reindex via the catalog projection. Best-effort; any failure
    returns "" and the caller treats the chunk as sourceless.
    """
    try:
        from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

        cat = make_catalog_reader()
        if cat is None:
            return ""
        entry = cat.by_doc_id(doc_id)
        if entry is None:
            return ""
        return entry.file_path or ""
    except Exception:  # noqa: BLE001 — best-effort metadata read for display; empty string on any failure
        return ""


@click.group()
def collection() -> None:
    """Manage T3 vector collections (list, info, verify, delete)."""


_LIST_COLUMNS: tuple[tuple[str, str], ...] = (
    ("CONTENT_TYPE", "content_type"),
    ("OWNER", "owner_id"),
    ("MODEL", "embedding_model"),
    ("DIM", "dimension"),
    ("STATE", "lifecycle_state"),
)


def _catalog_collection_rows() -> tuple[dict[str, dict], str]:
    """The catalog's collection rows keyed by name, and an error text
    ("" on success). The rows are the columns of record (RDR-204); the
    vectors stats join carries four of the five but not ``dimension``."""
    try:
        from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

        cat = make_catalog_reader()
        if cat is None:
            return {}, "catalog reader unavailable"
        return {str(r.get("name", "")): r for r in cat.list_collections()}, ""
    except Exception as exc:  # noqa: BLE001 — boundary: the listing still prints names and counts; the columns are reported as unread, never parsed from the name
        return {}, f"{type(exc).__name__}: {exc}"


@collection.command("list")
@click.option(
    "--all", "show_all", is_flag=True,
    help="Also list retired names (a rename's old name) that hold no chunks.",
)
def list_cmd(show_all: bool) -> None:
    """List T3 collections: live and stored chunk counts, and the catalog's
    columns (content type, owner, embedding model, dimension, lifecycle state).

    RDR-204 Day 2 (nexus-ft04v.32): every column is read from the
    collection's catalog row, never derived from its name, so a row whose
    name disagrees with its columns is visible rather than hidden behind a
    consistent-looking name. A collection with no catalog row prints ``-``
    in every column; a catalog row with no chunks prints 0.

    nexus-sis0m.3: a rename keeps the old name's catalog row as a retired
    tombstone (superseded_by names the new one). An empty tombstone is not a
    collection and is left out unless ``--all``; one still holding chunks is
    listed, with STATE ``superseded-><new name>``.
    """
    # nexus-sis0m.1: strict, so a service error is reported rather than read
    # as "No collections found." at exit 0.
    listed = _t3().list_collections(strict=True)
    counts = {c["name"]: c.get("count", 0) for c in listed}
    # nexus-7q8zg: CHUNKS is live (owned) chunks; STORED is what physically
    # sits in the collection. They differ for quarantine siblings and for
    # chunks with no manifest owner, which shape and catalog verify count.
    stored = {c["name"]: c.get("stored_count", c.get("count", 0)) for c in listed}
    rows, rows_error = _catalog_collection_rows()

    def _successor(name: str) -> str:
        return str(rows.get(name, {}).get("superseded_by") or "")

    names = sorted(
        n for n in set(counts) | set(rows)
        if show_all or not _successor(n) or stored.get(n, 0)
    )
    if not names:
        click.echo("No collections found.")
        return
    if rows_error:
        click.echo(f"catalog columns could not be read ({rows_error}); names and counts only")
    width = max(len(n) for n in names)
    def _cell(name: str, key: str) -> str:
        if key == "lifecycle_state" and _successor(name):
            return f"superseded->{_successor(name)}"
        val = rows.get(name, {}).get(key)
        return "-" if val is None or val == "" else str(val)

    cells = {n: [_cell(n, key) for _, key in _LIST_COLUMNS] for n in names}
    col_widths = [
        max(len(label), *(len(cells[n][i]) for n in names))
        for i, (label, _) in enumerate(_LIST_COLUMNS)
    ]
    header = f"{'NAME':<{width}}  {'CHUNKS':>6}  {'STORED':>6}  " + "  ".join(
        f"{label:<{w}}" for (label, _), w in zip(_LIST_COLUMNS, col_widths)
    )
    click.echo(header.rstrip())
    for n in names:
        line = f"{n:<{width}}  {counts.get(n, 0):>6}  {stored.get(n, 0):>6}  " + "  ".join(
            f"{cell:<{w}}" for cell, w in zip(cells[n], col_widths)
        )
        click.echo(line.rstrip())


def _shape_catalog() -> Any:
    """Read-facing catalog for the audit; a separate seam so tests can swap it."""
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    cat = make_catalog_reader()
    if cat is None:
        raise click.ClickException("catalog is not available; the audit needs it (nx catalog setup)")
    return cat


def _shape_write_model() -> Callable[[str], str | None]:
    """The install's model per content type (the RDR-204 profile, as code today).

    Read-shaped on purpose: ``effective_embedding_model_for_writes`` is
    documented as unsafe on a read path (it raises when ``local.embed_model``
    is voyage-shaped and no key is configured, nexus-35ok4), and the shape
    audit never writes. ``resolve_read_embedding_model`` is the credential-
    free counterpart and never raises.
    """
    from nexus.corpus import resolve_read_embedding_model  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    return resolve_read_embedding_model


#: Human output lists at most this many collections per check before
#: summarising the rest; ``--full`` or ``--json`` lifts the cap. 151 ghost
#: rows on the first live run buried the six findings that needed a human.
_SHAPE_MAX_LISTED_PER_CHECK: int = 10


@collection.command("shape")
@click.option("--json", "as_json", is_flag=True, help="Emit the report as JSON.")
@click.option("--full", is_flag=True, help=f"List every collection per check instead of the first {_SHAPE_MAX_LISTED_PER_CHECK}.")
def shape_cmd(as_json: bool, full: bool) -> None:
    """Read-only shape audit of the whole collection SET against docs/collections.md.

    Distinct from ``nx collection audit NAME`` (the RDR-087 deep dive of one
    collection).

    One finding per rule violation with a proposed action; never writes,
    never calls a model. Exit code is 0 whenever the tenant could be read,
    findings or not; a read failure is an error, never an empty report.
    """
    from nexus.collection_shape import audit  # noqa: PLC0415 — deferred to keep CLI startup fast

    try:
        report = audit(catalog=_shape_catalog(), t3=_t3(), write_model_for=_shape_write_model())
    except click.ClickException:
        raise
    except Exception as exc:  # noqa: BLE001 — boundary: surface the read failure loudly, never a clean-looking report
        raise click.ClickException(f"collection shape could not read the tenant: {exc}") from exc

    if as_json:
        import json as _json  # noqa: PLC0415 — deferred to keep CLI startup fast

        click.echo(_json.dumps(report.to_dict(), indent=2, sort_keys=True))
        return

    click.echo(
        f"collection shape: examined {report.collections_examined} collection(s)"
        + (f", skipped {report.collections_skipped_quarantine} quarantine" if report.collections_skipped_quarantine else "")
        + f"; {len(report.findings)} finding(s)"
    )
    if not report.findings:
        click.echo("  no findings against docs/collections.md")
        return
    by_check: dict[str, list] = {}
    for f in report.findings:
        by_check.setdefault(f.check, []).append(f)
    for check in sorted(by_check, key=lambda c: (by_check[c][0].rule, c)):
        items = by_check[check]
        click.echo(f"\n[rule {items[0].rule}] {check} ({len(items)})")
        shown = items if full else items[:_SHAPE_MAX_LISTED_PER_CHECK]
        for f in shown:
            target = f.collection or "(install)"
            rel = f" <> {f.related}" if f.related else ""
            click.echo(f"  {f.severity:<4} {target}{rel}")
            click.echo(f"       {f.message}")
            click.echo(f"       action: {f.action}")
        hidden = len(items) - len(shown)
        if hidden > 0:
            click.echo(f"  ... and {hidden} more (--full or --json to list all)")
    click.echo("\nrules: docs/collections.md  (this command never writes)")


@collection.command("info")
@click.argument("name")
def info_cmd(name: str) -> None:
    """Show details for a single collection."""
    db = _t3()
    cols = db.list_collections()
    match = next((c for c in cols if c["name"] == name), None)
    if match is None:
        # nexus-sis0m.3: a renamed collection's old name keeps a retired
        # catalog row naming its successor; say where it went.
        successor = str(_catalog_collection_rows()[0].get(name, {}).get("superseded_by") or "")
        if successor:
            raise click.ClickException(
                f"collection {name!r} was renamed to {successor} — use: nx collection info {successor}"
            )
        raise click.ClickException(f"collection not found: {name!r} — use: nx collection list")

    # nexus-sis0m F8: the collection's own catalog row names its model. The
    # local branch used to print the local embedder's name (MiniLM) for
    # every collection, whatever it was indexed with.
    idx_model = query_model = match.get("embedding_model") or ""
    if not idx_model:
        query_model = embedding_model_for_collection(name)
        idx_model = index_model_for_collection(name)
    else:
        # The row and the name can disagree (a disputed row, RDR-204); say so
        # rather than print one of them as the whole truth. Only a name that
        # encodes a model can disagree: for a subject name
        # (knowledge__distributed-systems) the name-derived model is a
        # prefix-table guess, not something the name says.
        from nexus.corpus import is_conformant_collection_name  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

        named = index_model_for_collection(name) if is_conformant_collection_name(name) else ""
        if named and named != idx_model:
            idx_model = query_model = f"{idx_model} (catalog row; the name says {named})"

    # nexus-ktsa1: the latest document indexed_at from the catalog, in one
    # read. Paging every chunk's metadata for MAX(indexed_at) (nexus-j857)
    # took 5 minutes on a 48k-chunk collection.
    last_indexed = _latest_document_indexed_at(name)

    click.echo(f"Collection:  {match['name']}")
    stored_count = match.get("stored_count", match["count"])
    if stored_count != match["count"]:
        click.echo(f"Chunks:      {match['count']} live, {stored_count} stored "
                   "(stored chunks with no live manifest owner are hidden from reads)")
    else:
        click.echo(f"Chunks:      {match['count']}")
    click.echo(f"Index model: {idx_model}")
    click.echo(f"Query model: {query_model}")
    click.echo(f"Indexed:     {last_indexed}")


def _latest_document_indexed_at(name: str) -> str:
    """Most recent ``indexed_at`` over the live catalog documents in
    collection *name*; ``"unknown"`` when it has none, and the error when
    the catalog cannot be read, so the two never look alike.

    One row from the engine's collection-health aggregate (nexus-dsu5z), the
    read ``nx collection health`` already makes."""
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    reader = None
    try:
        reader = make_catalog_reader()
        if reader is None:
            return "unknown (no catalog)"
        return str(reader.collection_health_meta(name).get("last_indexed") or "unknown")
    except Exception as exc:  # noqa: BLE001 — informational line; the error is printed, not raised
        return f"unknown (catalog could not be read: {exc})"
    finally:
        close = getattr(reader, "close", None)
        if close is not None:
            close()


def _require_docs_collection(row: dict, name: str) -> None:
    """Refuse a non-``docs__`` collection (round-2 critic decision item 4):
    ``aspects_enabled`` only ever gates the docs__ prose-extraction path
    (``knowledge__``/``rdr__`` are always extracted; other prefixes are
    never extracted at all), so setting or even showing it on anything
    else is a no-op dressed up as a real setting."""
    content_type = row.get("content_type")
    if content_type != "docs":
        raise click.ClickException(
            f"{name!r} is not a docs__ collection (content_type={content_type!r}); "
            "aspects_enabled only applies to docs__ collections"
        )


@collection.command("aspects")
@click.argument("name", required=False, default=None)
@click.option(
    "--enable/--disable", "enable", default=None,
    help="Opt this docs__ collection in to (or out of) engine-side aspect "
         "extraction, tenant-wide. Omit to show the current value.",
)
@click.option(
    "--from-config", "from_config", is_flag=True, default=False,
    help="Sync every registered docs__ collection matching this machine's "
         "local aspects.docs_collections onto the engine (sets "
         "aspects_enabled=True there). Cannot be combined with NAME or "
         "--enable/--disable.",
)
@click.option(
    "--dry-run", "dry_run", is_flag=True, default=False,
    help="With --from-config, report what would change without writing.",
)
@click.option("--yes", "-y", "--confirm", is_flag=True, help="Skip interactive confirmation prompt")
def aspects_cmd(
    name: str | None, enable: bool | None, from_config: bool, dry_run: bool, yes: bool,
) -> None:
    """Show or set a collection's engine-side ``aspects_enabled`` attribute.

    nexus-l46pu (follow-up to nexus-kk4ut): the tenant-wide, engine-hosted
    home for the docs__ aspect-extraction opt-in, read by every machine
    indexing this collection. Round-2 critic decision (T2 critique-nexus-
    l46pu-tenant-wide-aspects-enabled item 1): the ENGINE IS AUTHORITATIVE
    once it carries an opinion — a local ``aspects.docs_collections``
    config.yml entry is consulted only as a fallback for an engine that has
    none yet (see ``nexus.aspect_extractor.docs_collection_opted_in``).
    ``--from-config`` is the migration path off the local-only list.

    Bare ``nx collection aspects NAME`` shows the current value;
    ``--enable``/``--disable`` sets it, tenant-wide, for every machine —
    each write prompts for confirmation unless ``--yes``/``-y`` is given.
    """
    if from_config:
        if name is not None or enable is not None:
            raise click.ClickException(
                "--from-config cannot be combined with NAME or --enable/--disable"
            )
        _aspects_from_config(dry_run=dry_run, yes=yes)
        return
    if name is None:
        raise click.ClickException("NAME is required unless --from-config is given")

    from nexus.catalog.factory import make_catalog_reader, make_catalog_writer  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    reader = make_catalog_reader()
    row = reader.get_collection(name) if reader is not None else None
    if row is None:
        raise click.ClickException(f"collection not found: {name!r} — use: nx collection list")
    _require_docs_collection(row, name)

    if enable is None:
        current = row.get("aspects_enabled")
        if current is None:
            # round-2 fix round item 5a: "no opinion" now covers both an old
            # engine (never sends the key) AND an untouched row on a current
            # one (catalog-040's column is nullable, no default) — the two
            # are deliberately indistinguishable, and both fall back to the
            # SAME local answer, so show what that fallback actually
            # resolves to instead of leaving the operator to check it by hand.
            from nexus.aspect_extractor import docs_collection_opted_in  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
            local_answer = docs_collection_opted_in(name)
            click.echo(
                f"{name}: aspects_enabled has no opinion on this engine yet "
                "(never set, or an engine older than nexus-l46pu) — falling "
                f"back to local config.yml, which resolves to {local_answer} "
                "for this machine"
            )
        else:
            click.echo(f"{name}: aspects_enabled={bool(current)}")
        return

    # nexus-l46pu round-2 item 3: state the blast radius and the cost at
    # EXECUTION time, not only in --help — this is a tenant-wide setting
    # (every machine indexing this collection agrees the instant it lands),
    # and enabling it means an LLM call per changed prose document from now on.
    click.echo(
        f"Setting aspects_enabled={enable} for {name!r} on the ENGINE — "
        "tenant-wide: every machine indexing this collection will see this "
        "value." + (
            " Prose documents will cost an LLM call each time they change."
            if enable else
            " No further extraction after this; existing document_aspects "
            "rows are left in place (see: nx enrich delete)."
        )
    )
    if not yes:
        click.confirm(f"Set aspects_enabled={enable} for {name!r} on the engine?", abort=True)

    writer = make_catalog_writer()
    updated = writer.set_collection_aspects_enabled(name, enable)
    if not updated:
        raise click.ClickException(f"collection not found: {name!r}")

    from nexus.aspect_extractor import invalidate_engine_aspects_enabled_cache  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
    invalidate_engine_aspects_enabled_cache(name)
    _log.info("collection_aspects_enabled_set", collection=name, aspects_enabled=enable, tenant_wide=True)
    click.echo(f"{name}: aspects_enabled={enable}")


def _aspects_from_config(*, dry_run: bool, yes: bool = False) -> None:
    """``nx collection aspects --from-config`` (round-2 critic item 1a):
    sync every registered docs__ collection this machine's LOCAL
    ``aspects.docs_collections`` list matches onto the engine's
    ``aspects_enabled`` attribute, so the engine becomes authoritative for
    it (see ``docs_collection_opted_in``'s new precedence) instead of the
    decision living only in this machine's config.yml.

    Never turns a collection OFF (a local list is opt-IN only, by
    construction; there is nothing in it that means "disable"). Prints
    what it changed (or would change, under ``--dry-run``) and nothing
    when the local list is empty or matches nothing registered.

    ``yes``: skip the confirmation prompt (round-2 fix round item 4,
    blast-radius confirmation). Prompted once for the whole batch, not once
    per collection — this is one operator decision ("sync everything my
    local list already opted in"), not N independent ones. Never prompts
    under ``--dry-run`` (nothing is written) or when nothing would change.
    """
    from nexus.aspect_extractor import (  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
        _docs_opt_in_patterns,
        invalidate_engine_aspects_enabled_cache,
    )
    from nexus.catalog.factory import make_catalog_reader, make_catalog_writer  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    patterns = _docs_opt_in_patterns()
    if not patterns:
        click.echo("aspects.docs_collections is empty on this machine; nothing to sync.")
        return

    reader = make_catalog_reader()
    rows = reader.list_collections() if reader is not None else []
    matched = [
        r for r in rows
        if r.get("content_type") == "docs"
        and any(fnmatch.fnmatchcase(r["name"], p) for p in patterns)
    ]
    if not matched:
        click.echo(
            "No registered docs__ collection matches this machine's "
            "aspects.docs_collections."
        )
        return

    to_change = [r for r in matched if r.get("aspects_enabled") is not True]
    if not dry_run and to_change and not yes:
        # Name what will change before asking (round-3 critique): a bare
        # count gives the operator nothing to check against.
        for r in to_change[:20]:
            click.echo(f"  will enable: {r['name']}")
        if len(to_change) > 20:
            click.echo(f"  ... and {len(to_change) - 20} more")
        click.confirm(
            f"Set aspects_enabled=True on the ENGINE for {len(to_change)} "
            "docs__ collection(s), tenant-wide — every machine indexing "
            "them will see this, and prose documents will cost an LLM call "
            "each time they change. Proceed?",
            abort=True,
        )

    # nexus-l46pu round-2 fix round item 5b: hoisted out of the loop — a
    # lightweight proxy over a shared handle, not a new connection per row.
    writer = make_catalog_writer()

    changed = 0
    for row in matched:
        name = row["name"]
        if row.get("aspects_enabled") is True:
            click.echo(f"{name}: already aspects_enabled=True on the engine (no change)")
            continue
        if dry_run:
            click.echo(f"{name}: would set aspects_enabled=True (dry run)")
            changed += 1
            continue
        writer.set_collection_aspects_enabled(name, True)
        invalidate_engine_aspects_enabled_cache(name)
        _log.info("collection_aspects_enabled_set", collection=name, aspects_enabled=True, tenant_wide=True, source="from_config")
        click.echo(f"{name}: aspects_enabled=True")
        changed += 1

    click.echo(
        f"{'Would change' if dry_run else 'Changed'} {changed} of "
        f"{len(matched)} matching collection(s)."
    )


@collection.command("delete")
@click.argument("name")
@click.option("--yes", "-y", "--confirm", is_flag=True, help="Skip interactive confirmation prompt")
def delete_cmd(name: str, yes: bool) -> None:
    """Delete a T3 collection + cascade-purge taxonomy state (irreversible).

    Deleting an origin also deletes its rows in its ``quarantine-`` siblings
    (one gc_audit row per sibling). Deleting a ``quarantine-*`` collection
    itself is the audited manual route for quarantined rows whose origin is
    gone (gc_audit: quarantine_collection_delete).
    """
    if not yes:
        click.confirm(f"Delete collection '{name}'? This cannot be undone.", abort=True)

    # nexus-jm3z / nexus-8a8e / nexus-vxz3: the full delete cascade (T3 +
    # taxonomy + chash + pipeline + catalog docs + projection) is the
    # canonical ``purge_collection_cascade`` (RDR-144 nexus-prgf4), shared
    # with the 384->768 migration so both leave the same clean state.
    # nexus-lub: a T3 delete may find the collection already absent (a prior
    # half-delete); the cascade still runs to clean the orphan rows.
    from nexus.db.collection_purge import purge_collection_cascade  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    cascade = purge_collection_cascade(_t3(), name)
    if cascade.t3_absent:
        click.echo(
            f"note: T3 collection '{name}' already absent — running cascade anyway",
            err=True,
        )
    # Surface any cascade-step failure to the user (orphan rows may remain).
    for failure in cascade.failures:
        click.echo(f"warn: {failure}", err=True)
    if cascade.service_delete_failed:
        # nexus-oq5gd: the service never confirmed the delete (a read timeout
        # makes ONE attempt, so it may or may not have committed). Do not
        # claim "Deleted:".
        raise click.ClickException(
            f"delete of '{name}' is unconfirmed: the service cascade failed. "
            f"Re-run `nx collection delete {name}`."
        )

    taxonomy_counts = cascade.taxonomy
    chash_deleted = cascade.chash_deleted
    pipeline_rows_deleted = cascade.pipeline_rows_deleted
    catalog_docs_deleted = cascade.catalog_docs_deleted
    catalog_projection_deleted = cascade.catalog_projection_deleted
    quarantine_chunks_deleted = cascade.quarantine_chunks_deleted

    from nexus.catalog.chunk_quarantine import is_quarantine_sibling_name  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    # The one sanctioned name parser for the quarantine flag (no new raw parse sites, RDR-204).
    is_quarantine_name = is_quarantine_sibling_name(name)
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
    if chash_deleted:
        parts.append(f"{chash_deleted} chash rows")
    if pipeline_rows_deleted:
        parts.append(f"{pipeline_rows_deleted} pipeline rows")
    if catalog_docs_deleted:
        parts.append(f"{catalog_docs_deleted} catalog docs")
    if catalog_projection_deleted:
        parts.append(f"{catalog_projection_deleted} catalog projection row")
    if quarantine_chunks_deleted:
        # nexus-wbfpw.68/.71: the origin's quarantine rows go with it, audited per sibling.
        audit_op = "quarantine_collection_delete" if is_quarantine_name else "collection_delete_quarantine"
        parts.append(f"{quarantine_chunks_deleted} quarantined chunks (gc_audit: {audit_op})")
    if parts:
        click.echo(f"Deleted: {name} ({'; '.join(parts)})")
    else:
        click.echo(f"Deleted: {name}")


# ── prune (nexus-9tsdf / GH #1113) ──────────────────────────────────────────


def _dim_for_model_token(token: str) -> int | None:
    """Dimension implied by a conformant collection's ``__<model>__`` segment.

    Local-mode tokens encode the dim in the suffix
    (``minilm-l6-v2-384`` -> 384, ``bge-base-en-v15-768`` -> 768). Voyage
    tokens are hardcoded to 1024. Mirrors
    ``nexus.commands.catalog_cmds.doctor._expected_dim_for_model_token``
    (a self-consistency check: name-token vs THIS collection's own
    content). That check lives outside this file's scope; duplicated here
    rather than imported since it is a small pure function and the two
    checks answer different questions (name-vs-own-content there,
    name-vs-ACTIVE-serving-embedder here).
    """
    if token in CANONICAL_EMBEDDING_MODELS:
        return 1024
    if token in LOCAL_EMBEDDING_MODELS:
        tail = token.rsplit("-", 1)[-1]
        try:
            return int(tail)
        except ValueError:
            return None
    return None


#: Every Voyage model a key would serve (RDR-210 Technical Design 10). A
#: Voyage-named collection is never an orphan: on a keyless engine it is
#: "needs the key", not "unsearchable", and deleting it would destroy data
#: the install can serve the moment a key arrives.
_VOYAGE_MODEL_TOKENS: frozenset[str] = frozenset({
    "voyage-code-3", "voyage-context-3", "voyage-3",
})

#: The current local embedder (Sam, 2026-10-18). A bge collection is never an
#: orphan merely because the engine reports no bge model: a keyed engine with no
#: bge files reports only the Voyage set, but `nx init` provisions the files and
#: serves the collection again, while `prune --yes` would have destroyed it. A
#: stale local generation (minilm-384) is still an orphan.
_LOCAL_MODEL_TOKENS: frozenset[str] = frozenset({"bge-base-en-v15-768"})


def _engine_embedding_models(t3: Any) -> frozenset[str] | None:
    """The engine's ``GET /version`` ``embedding_models`` as a set, or
    ``None`` when it cannot be read (probe failure, an engine older than the
    field, or a malformed value). ``None`` means "cannot verify"; an empty
    set is returned only when the engine really reported ``[]``.
    """
    try:
        from nexus.db.http_vector_client import _get, _process_default_tenant  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

        info = _get("/version", tenant=getattr(t3, "_tenant", None) or _process_default_tenant())
    except Exception as exc:  # noqa: BLE001 — probe is best-effort; absence falls back to "cannot verify"
        _log.debug("served_models_probe_failed", error=str(exc))
        return None
    models = info.get("embedding_models") if isinstance(info, dict) else None
    if not isinstance(models, list) or not all(isinstance(m, str) for m in models):
        return None
    return frozenset(models)


def _served_embedding_models(t3: Any) -> tuple[frozenset[str] | None, str]:
    """Return ``(models, label)``: the model tokens the ENGINE serves, and a
    display label.

    ``models`` is ``None`` (label ``"unknown"``) when they cannot be read —
    never guessed, since a wrong guess would flag healthy collections as
    orphans and ``prune --yes`` would delete real data.

    Two dispatch paths:

    * ``t3`` exposes ``embedding_mode()`` (the production
      :class:`HttpVectorClient` handle): the engine's own ``/version``
      ``embedding_models`` is the authority (RDR-210 Technical Design 10).
      ``embedding_mode`` (``onnx-local`` / ``voyage`` / ``dual``, or a value
      this client has never seen) is display only and decides nothing, so an
      engine reporting a mode this client does not know cannot make it
      delete anything it should not.
    * ``t3`` has no ``embedding_mode`` (a raw :class:`T3Database` — test /
      migration-ETL substitute): its own ``_local_mode`` flag is
      authoritative; local serves the persisted service token, cloud serves
      Voyage only.
    """
    embedding_mode = getattr(t3, "embedding_mode", None)
    if callable(embedding_mode):
        models = _engine_embedding_models(t3)
        if models is None:
            return None, "unknown"
        try:
            mode = embedding_mode()
        except Exception:  # noqa: BLE001 — display only; the model set already decided
            mode = None
        listed = ", ".join(sorted(models)) or "no local models"
        label = f"{mode} [{listed}]" if isinstance(mode, str) and mode else listed
        return models, label

    local_mode = getattr(t3, "_local_mode", True)
    if local_mode:
        token = _service_local_token()
        return frozenset({token}), token
    return frozenset(), "voyage"


def _service_local_token() -> str:
    """Token of the local-mode SERVICE embedder: the persisted
    ``local.embed_model`` choice first, client-EF auto-select fallback."""
    from nexus.config import local_embed_model_choice  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
    from nexus.db.local_ef import _MODEL_TOKENS, local_model_token  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    choice = local_embed_model_choice()
    if choice in _MODEL_TOKENS:
        return _MODEL_TOKENS[choice]
    return local_model_token()


def _find_dimension_mismatched_collections(
    t3: Any,
) -> tuple[list[dict], int, str]:
    """Collections whose ``__<model>__`` name segment names a model this
    install cannot serve (orphans from a prior embedder generation — GH #1113).

    The rule (RDR-210 Technical Design 10): a collection is an orphan only
    when its model is in NONE of: the engine's served ``embedding_models``, the
    Voyage model set, or the current local model (bge). A served collection can
    never be listed, and neither can one the install could serve after ``nx init``.

    Returns ``(mismatches, skipped_non_conformant, active_label)``.
    ``mismatches`` items: ``{name, declared_model, declared_dim, served_models,
    count}``. When the served set cannot be read the label is ``"unknown"`` and
    nothing is listed. Bypass-schema (``taxonomy__*`` etc.), non-conformant
    (legacy 2-segment, no embedder-model token) and unrecognised-token
    collection names are skipped, not flagged: a token this client has never
    heard of may well be one a newer engine serves, and guessing would risk a
    false-positive delete.
    """
    # RDR-204 Phase 3 (nexus-ft04v.26), class (d): deliberately NOT the
    # row-based collection_model. This diagnostic's whole point is
    # comparing what the NAME CLAIMS against what the engine serves -- a
    # mismatch between the row (authoritative) and reality should not
    # even be possible under Phase 1's constraints, which would make this
    # scan vacuous if repointed to the row. embedding_model_for_collection_name
    # (regex, unaffected by the repoint) reads the name's own segment
    # directly, exactly as this function's docstring describes.
    from nexus.corpus import embedding_model_for_collection_name, is_conformant_collection_name  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
    from nexus.db.t3 import _BYPASS_SCHEMA_PREFIXES  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    engine_models, active_label = _served_embedding_models(t3)
    mismatches: list[dict] = []
    skipped = 0
    if engine_models is None:
        return mismatches, skipped, active_label
    served = engine_models | _VOYAGE_MODEL_TOKENS | _LOCAL_MODEL_TOKENS

    for c in t3.list_collections():
        name = c["name"]
        if name.startswith(_BYPASS_SCHEMA_PREFIXES):
            continue
        if not is_conformant_collection_name(name):
            skipped += 1
            continue
        # name is already confirmed conformant above -- the regex read
        # never returns None here.
        token = embedding_model_for_collection_name(name) or ""
        if token in served:
            continue
        declared_dim = _dim_for_model_token(token)
        if declared_dim is None:
            skipped += 1
            continue
        mismatches.append({
            "name": name,
            "declared_model": token,
            "declared_dim": declared_dim,
            "served_models": sorted(engine_models),
            # RDR-192 Step 5 amendment: prune deletes every stored chunk,
            # so the count shown before it is the stored count.
            "count": c.get("stored_count", c.get("count", 0)),
        })
    return mismatches, skipped, active_label


@collection.command("prune")
@click.option("--yes", "-y", is_flag=True, help="Delete the listed orphans (non-interactive; the ONLY way to delete).")
@click.option("--dry-run", "dry_run", is_flag=True, help="List only; never delete. This is the default behavior — the flag exists for explicit scripting and always wins over --yes.")
def prune_cmd(yes: bool, dry_run: bool) -> None:
    """List (and, with --yes, delete) orphaned dimension-mismatched T3
    collections (GH #1113).

    A collection created under a prior embedder generation (e.g. a
    minilm-l6-v2-384 collection left behind after the active embedder
    moved to bge-base-en-v15-768) can never be searched — every
    cross-corpus search silently skips it. Before this command the only
    way to find and remove such an orphan was to read structured logs and
    run ``nx collection delete <name>`` by hand.

    Fail-safe by default: with no flags, this command ONLY lists what it
    found — nothing is ever deleted. Pass --yes to actually delete the
    listed collections (cascade-purges taxonomy/chash/pipeline/catalog
    state the same way ``nx collection delete`` does). --dry-run is an
    explicit, always-safe alias for the default and takes precedence over
    --yes if both are passed.
    """
    t3 = _t3()
    mismatches, skipped, active_label = _find_dimension_mismatched_collections(t3)

    if active_label == "unknown":
        # nexus-bwulw: an unresolved active-embedder probe means NOTHING was
        # checked — say so, aligned with doctor's wording. The old
        # "No dimension-mismatched collections found (active embedder:
        # unknown)" read as verified-clean.
        click.echo(
            "Skipped (active embedder unresolved — cannot verify): the "
            "service /version probe reported no embedding_models, so orphan "
            "detection has no served-model set to compare against and never "
            "guesses. Nothing was checked or deleted."
        )
        return

    if not mismatches:
        click.echo(
            f"No dimension-mismatched collections found "
            f"(active embedder: {active_label})."
        )
        if skipped:
            click.echo(
                f"({skipped} non-conformant collection name(s) skipped — "
                f"cannot verify their dimension from the name alone.)"
            )
        return

    click.echo(f"Active embedder: {active_label}")
    click.echo(f"{len(mismatches)} dimension-mismatched collection(s):")
    for m in mismatches:
        click.echo(
            f"  {m['name']}"
            f"  (declared {m['declared_model']}={m['declared_dim']}d,"
            f" not served by this engine, {m['count']} chunk(s))"
        )
    if skipped:
        click.echo(
            f"({skipped} non-conformant collection name(s) skipped — "
            f"cannot verify their dimension from the name alone.)"
        )

    if dry_run or not yes:
        click.echo("\nDry run — nothing deleted. Re-run with --yes to delete.")
        return

    from nexus.db.collection_purge import purge_collection_cascade  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    unconfirmed: list[str] = []
    for m in mismatches:
        cascade = purge_collection_cascade(t3, m["name"])
        for failure in cascade.failures:
            click.echo(f"warn: {failure}", err=True)
        if cascade.service_delete_failed:
            unconfirmed.append(m["name"])
    deleted_count = len(mismatches) - len(unconfirmed)
    click.echo(f"\nDeleted {deleted_count} collection(s).")
    if unconfirmed:
        # nexus-oq5gd: see delete_cmd — a failed service cascade is unconfirmed.
        raise click.ClickException(
            f"{len(unconfirmed)} delete(s) unconfirmed (service cascade failed): "
            f"{', '.join(unconfirmed)}. Re-run `nx collection prune --yes`."
        )


# nexus-8g79.10 (V5): rename_collection_data_plane extracted to
# nexus.collection_rename (peer to indexer.py) so the indexer's
# RDR-103 P5 conformant-shape migrator at indexer.py:435 doesn't
# reach up into commands/. The library version requires explicit
# ``t3_db``; CLI callers within this module pass ``_t3()``.
from nexus.collection_rename import rename_collection_data_plane as _rename_collection_data_plane  # noqa: E402


def rename_collection_data_plane(
    old: str,
    new: str,
    *,
    t3_db=None,
    catalog: Any | None = None,
    on_warn: Callable[[str], None] | None = None,
) -> dict[str, int]:
    """CLI-shape wrapper: default ``t3_db=_t3()`` when omitted.

    See ``nexus.collection_rename.rename_collection_data_plane`` for
    the full data-plane contract. This wrapper preserves the
    ``t3_db=None`` → ``_t3()`` convenience used by the CLI rename
    command and by the orphan-collection cleanup paths.
    """
    if t3_db is None:
        t3_db = _t3()
    return _rename_collection_data_plane(
        old, new, t3_db=t3_db, catalog=catalog, on_warn=on_warn,
    )


@collection.command("rename")
@click.argument("old")
@click.argument("new")
@click.option(
    "--force-prefix-change",
    is_flag=True,
    help=(
        "Allow a cross-prefix rename (e.g. code__foo → docs__foo). "
        "Embedding-model spaces differ across prefixes, so the renamed "
        "collection is query-incompatible with its old clients. Use only "
        "when you've deleted every downstream reader of the old name. Also "
        "overrides the model-token guard: a conformant new name whose "
        "model token differs from the source collection's catalog-row "
        "embedding_model is refused without it (rename never re-embeds), "
        "so pass it only when the vectors really are in the token's model."
    ),
)
def rename_cmd(old: str, new: str, force_prefix_change: bool) -> None:
    """Rename a collection in-place via the engine's native rename.

    O(1) metadata update — no embedding re-upload, no Voyage cost.
    Cascades the new name through T2 taxonomy, chash_index, and the
    catalog.

    Cross-prefix renames (e.g. ``code__`` ↔ ``docs__``) change the
    embedding-model space and are rejected unless ``--force-prefix-change``
    is set; otherwise search hits would be garbage.

    A conformant new name is also checked against the source collection's
    catalog-row ``embedding_model`` (nexus-7f4o4): the engine keeps the
    row's model across a rename, so a ``__<model>__`` token that differs
    from it is refused, naming both models and the name that would be
    accepted, unless ``--force-prefix-change`` is set.
    """
    # RDR-204 Phase 3 (nexus-ft04v.26), class (a): `new` is the RENAME
    # TARGET -- it does not exist yet (rename is what creates it), so it
    # structurally has no catalog row. `old` might ALSO be a legacy,
    # never-registered collection this very rename is meant to fix.
    # Candidate-string derivation for both sides keeps the prefix-mismatch
    # guard symmetric, never a row-based lookup that could raise on either.
    from nexus.corpus import split_candidate_collection_name  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    old_prefix = split_candidate_collection_name(old)[0]
    new_prefix = split_candidate_collection_name(new)[0]
    if old_prefix != new_prefix and not force_prefix_change:
        raise click.ClickException(
            f"prefix mismatch: {old_prefix!r} → {new_prefix!r} would change "
            f"the embedding-model space. Pass --force-prefix-change if this "
            f"is intentional (rare — usually means the caller is resurrecting "
            f"an orphaned collection with the wrong prefix)."
        )
    # nexus-tcvpn: a SAME-prefix rename whose embedding-model SEGMENT differs
    # sailed through the prefix guard — but rename is an O(1) identity
    # re-home with ZERO re-embedding, so the vectors would stay in the old
    # model space while the name claims the new one (silent staleness/
    # search corruption, no Voyage call ever fires). Cross-model moves are
    # the RDR-210 re-embed job's concern (a per-document move into the model
    # sibling), not rename's.
    #
    # nexus-7f4o4: for a CONFORMANT target the comparison is the new name's
    # model token against the SOURCE ROW's embedding_model (the engine keeps
    # the row's model across a rename), not against a model read from the old
    # NAME -- a legacy bge-row collection reads as voyage by name, so the
    # name-vs-name guard passed the wrong rename and refused the right one.
    # A non-conformant target keeps the name-vs-name comparison.
    from nexus.corpus import is_conformant_collection_name, rename_model_conflict  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    if is_conformant_collection_name(new):
        # A `disputed` source (hygiene-002: its bytes disagree with its row)
        # has a row model that is itself in doubt: warn, do not compare.
        try:
            from nexus.mcp_infra import get_collection_row  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

            source_row = get_collection_row(old)
        except Exception:  # noqa: BLE001 — advisory read; a failed cache read falls to the comparison below
            source_row = None
        if (source_row or {}).get("lifecycle_state") == "disputed":
            click.echo(
                f"warning: {old!r} is a disputed collection (its bytes "
                f"disagree with its catalog row), so its row model is not "
                f"trusted and {new!r}'s model token was not compared with it.",
                err=True,
            )
            conflict = None
        else:
            conflict = rename_model_conflict(old, new)
        if conflict is not None and not force_prefix_change:
            source_model, token_model, correct_name = conflict
            raise click.ClickException(
                f"embedding-model mismatch: {old!r} holds {source_model!r} "
                f"vectors (its catalog row) but {new!r} names {token_model!r}. "
                f"rename never re-embeds -- the vectors would silently stay "
                f"{source_model!r} under a name claiming {token_model!r}. "
                f"Rename to {correct_name!r} instead, or pass "
                f"--force-prefix-change ONLY if you know the vectors are "
                f"already {token_model!r}."
            )
    else:
        old_model = embedding_model_for_collection(old)
        new_model = embedding_model_for_collection(new)
        if old_model != new_model and not force_prefix_change:
            raise click.ClickException(
                f"embedding-model mismatch: {old!r} encodes {old_model!r} but "
                f"{new!r} encodes {new_model!r}. rename never re-embeds — the "
                f"vectors would silently stay {old_model!r} under a name claiming "
                f"{new_model!r}. Cross-model moves re-index from source into the "
                f"{new_model!r}-named collection (nx index repo / nx index pdf — "
                f"the RDR-162 vector-ETL pipeline retired at RDR-155 P4b). "
                f"Pass --force-prefix-change ONLY if you know the vectors are "
                f"already {new_model!r}."
            )

    counts = rename_collection_data_plane(old, new)

    parts: list[str] = []
    if counts["tax_topics"] or counts["tax_assignments"] or counts["tax_meta"]:
        parts.append(
            f"{counts['tax_topics']} topics, "
            f"{counts['tax_assignments']} assignments, "
            f"{counts['tax_meta']} meta"
        )
    if counts["chash"]:
        parts.append(f"{counts['chash']} chash rows")
    if counts["catalog_docs"]:
        parts.append(f"{counts['catalog_docs']} catalog docs")
    if parts:
        click.echo(f"Renamed: {old} → {new} ({'; '.join(parts)})")
    else:
        click.echo(f"Renamed: {old} → {new}")


@collection.command("reindex")
@click.argument("name")
@click.option("--force", is_flag=True, help="Force reindex even if sourceless entries exist")
def reindex_cmd(name: str, force: bool) -> None:
    """Delete and re-index a collection from its source files."""
    from pathlib import Path  # noqa: PLC0415 — stdlib import kept branch-local

    # RDR-204 Phase 3 (nexus-ft04v.26), class (a): this command deletes
    # `name`'s catalog row partway through (purge_collection_cascade
    # below) and only best-effort re-registers it ("the re-index below
    # must proceed" even on a failed re-register) -- every
    # content-type dispatch in this function, before AND after that
    # window, uses the candidate-string primitive so none of them can
    # raise CollectionNotRegisteredError depending on where in the
    # function they happen to run.
    from nexus.corpus import split_candidate_collection_name  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
    from nexus.db.t3 import verify_collection_deep  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
    from nexus.doc_indexer import batch_index_markdowns, index_markdown, index_pdf  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
    from nexus.pdf_stack import PdfStackError  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    db = _t3()

    # 1. Check collection exists
    try:
        info = db.collection_info(name)
    except KeyError:
        raise click.ClickException(f"collection not found: {name!r}")

    before_count = info["count"]

    # 1b. code__ collections have NO re-index driver in this verb — the
    # branch below only echoes a suggestion to run `nx index repo`. While
    # the delete hop was dead (NotImplementedError, nexus-sjb52) that was
    # unreachable; with a WORKING delete it would silently destroy the
    # collection (chunks + catalog + taxonomy + registry) and exit 0 with
    # "0 sources processed" (nexus-caifp). Refuse BEFORE deleting anything,
    # and before the sourceless scan below: that scan asks for --force
    # first, so an operator paid a second run to reach this refusal
    # (GH #1546, nexus-rndy5).
    candidate_type = split_candidate_collection_name(name)[0]
    if candidate_type == "code":
        raise click.ClickException(
            f"Refusing to reindex code collection '{name}': this verb has "
            "no re-index driver for code — it would delete the collection "
            "and rebuild nothing. Use `nx index repo <path>` instead, "
            "which re-indexes in place."
        )

    # 2. Pre-delete safety: paginate for sourceless entries (nexus-unyc).
    # nexus-7b5n: a chunk is "reindexable" when it carries either
    # ``source_path`` (legacy chunks predating the doc_id backfill) or
    # ``doc_id`` (post-Phase-4 chunks). After the prune verb drops
    # source_path from chunk metadata, doc_id is the only signal; the
    # catalog row pointed to by doc_id holds the file_path used for
    # the actual reindex.
    #
    # nexus-vn48 (RDR-108 Phase 4 review D-M1): RDR-108 Phase 3
    # (nexus-bdag) removed doc_id from chunk metadata too. For
    # Phase-3 chunks both source_path and doc_id are gone; the
    # only remaining signal is ``chunk_text_hash``. Resolve via
    # the catalog's chash -> doc_id manifest reverse-lookup, then
    # fall through to the existing _doc_id_to_file_path helper.
    col = db.get_or_create_collection(name)
    sourceless: list[str] = []
    source_paths: set[str] = set()
    offset = 0
    # Build a one-shot catalog handle for the manifest fallback.
    _cat = None
    try:
        from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
        _cat = make_catalog_reader()
    except Exception:  # noqa: BLE001 — best-effort chash index probe; falls through to per-chunk path
        pass
    while True:
        # include_non_live (RDR-192 / nexus-wbfpw.35): the scan guards a DELETE,
        # so it reads every stored chunk. before_count is the stored count, and a
        # live-only read made a collection of unowned chunks look empty here,
        # which skipped the "would destroy every chunk" refusal below.
        batch = col.get(
            limit=300, offset=offset, include=["metadatas"], include_non_live=True,
        )
        # nexus-vn48: page-batch chash -> doc_id resolution to amortise
        # SQLite calls across the page rather than per-chunk.
        page_chashes = [
            (m or {}).get("chunk_text_hash", "")
            for m in (batch["metadatas"] or [])
        ]
        page_chashes_nonempty = [c for c in page_chashes if c]
        chash_to_doc: dict[str, str] = {}
        if _cat is not None and page_chashes_nonempty:
            try:
                by_chash = _cat.docs_for_chashes(page_chashes_nonempty)
            except Exception:  # noqa: BLE001 — best-effort batch chash resolution; empty map on failure
                by_chash = {}
            for c, doc_ids in by_chash.items():
                if doc_ids:
                    chash_to_doc[c] = sorted(doc_ids)[0]

        for mid, meta in zip(batch["ids"], batch["metadatas"] or []):
            meta = meta or {}
            sp = meta.get("source_path", "")
            did = meta.get("doc_id", "")
            # Phase-3 fallback: resolve via manifest when metadata
            # lacks doc_id but carries chunk_text_hash.
            if not did:
                chash = meta.get("chunk_text_hash", "")
                if chash:
                    did = chash_to_doc.get(chash, "")
            if sp:
                source_paths.add(sp)
            elif did:
                # Post-prune chunk: source_path was dropped but the
                # catalog still holds the file_path keyed on doc_id.
                # Resolve it so the reindex driver below has the path.
                resolved = _doc_id_to_file_path(did)
                if resolved:
                    source_paths.add(resolved)
                else:
                    # Catalog gap: treat as sourceless so the safety
                    # check fires. Post-iftc, operator restores the
                    # catalog by deleting the catalog directory and
                    # re-running ``nx catalog setup`` (the
                    # synthesize-log + t3-backfill-doc-id verbs that
                    # historically repaired this case were retired).
                    sourceless.append(mid)
            else:
                sourceless.append(mid)
        if len(batch["ids"]) < 300:
            break
        offset += 300

    # nexus-wbfpw.35 fix rounds 2-3: the scan above reads STORED chunks, which
    # includes those of TOMBSTONED catalog documents. The collection is deleted
    # wholesale below and every path in source_paths is then re-indexed into a NEW
    # live document, so a path the catalog holds only as a deleted document would
    # be revived. Drop exactly those paths from the rebuild and say so.
    #
    # The guard fails CLOSED. A path is dropped only when a tombstone's file_path
    # EQUALS it (after making it relative to that tombstone owner's repo_root) and
    # the catalog holds NO live document in this collection that names it, under
    # ANY owner and in either path form: a re-indexed file has a tombstone AND a live
    # document at one path, and one file can be catalogued under two owners
    # (nexus-z0lu4), so a deleted document's owner is not the only one to ask.
    # Dropping a path a live document names would purge its chunks and rebuild
    # none of them. A catalog that cannot be
    # read, or an engine whose trash listing carries no file_path, refuses the
    # verb instead of running it unguarded.
    deleted: list[str] = []
    if source_paths:
        from nexus.catalog.tombstones import deleted_only_sources, read_tombstones  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

        if _cat is None:
            raise click.ClickException(
                f"Refusing to reindex '{name}': the catalog could not be read, so "
                f"the verb cannot tell a source that was deleted on purpose from "
                f"one that was not, and re-indexing would revive it. Nothing was "
                f"deleted."
            )
        tombstones = read_tombstones(_cat)
        deleted = deleted_only_sources(_cat, tombstones, source_paths, collection=name)
        if deleted:
            source_paths.difference_update(deleted)
            click.echo(
                f"Skipping {len(deleted)} source(s) of deleted catalog "
                f"documents (not re-indexed): "
                + ", ".join(Path(d).name for d in deleted[:5])
                + (" ..." if len(deleted) > 5 else "")
            )

    # If EVERY entry is sourceless, --force does nothing useful — there is
    # no source to reindex from, so the operation collapses to "delete the
    # collection". GitHub #367: a user lost 28 store_put-only entries this
    # way during an embedding-model migration. Refuse unconditionally and
    # point at `nx collection delete` for the genuine-delete path.
    if sourceless and not source_paths:
        raise click.ClickException(
            f"Refusing to reindex '{name}': all {len(sourceless)} entries "
            f"lack source_path (e.g. manual store_put entries, "
            f"taxonomy__centroids, or other programmatically-populated "
            f"collections, or chunks hidden from every listing because no "
            f"live catalog document owns them). There is no source to re-index "
            f"from — this would destroy every chunk with no recovery path.\n\n"
            f"  • If you want to delete the collection, run:\n"
            f"      nx collection delete {name}\n"
            f"  • In-place re-embedding (preserve content, swap embedding "
            f"model) is not yet supported. Track at GitHub #367.\n\n"
            f"--force does not bypass this check — there is nothing to force."
        )

    if deleted and not source_paths:
        raise click.ClickException(
            f"Refusing to reindex '{name}': every source in it ({len(deleted)}) "
            f"belongs to a deleted catalog document, so there is nothing to "
            f"rebuild and the verb would purge the collection and leave it empty. "
            f"Restore a document (`nx catalog restore`), or run "
            f"`nx collection delete {name}` if the collection should go."
        )

    if sourceless and not force:
        raise click.ClickException(
            f"{len(sourceless)} entries lack source_path and "
            f"{len(source_paths)} have source files. The {len(sourceless)} "
            f"sourceless entries cannot be re-indexed and will be LOST. They "
            f"are manual store_put entries and/or hidden chunks: chunks with no "
            f"live catalog document, which a plain listing does not show "
            f"(`nx collection info {name}` reports live and stored counts; the "
            f"difference is the hidden chunks). "
            f"Use --force to proceed and accept that loss."
        )

    # 2c. The PDF stack (nexus-2k66t.4.2), BEFORE the purge below. The loop
    # that follows indexes each existing .pdf source through index_pdf, which
    # needs docling and MinerU; on a slim install every one of them fails, and
    # the failure used to be a per-file warning AFTER the collection was gone:
    # "Re-indexed: N -> 0 chunks", exit 0, the data deleted. ensure_or_exit
    # installs (and re-runs this command) or exits 69/75/70 with nothing deleted.
    if candidate_type in ("docs", "knowledge") and any(
        sp.lower().endswith(".pdf") and Path(sp).exists() for sp in source_paths
    ):
        from nexus import pdf_stack  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

        pdf_stack.ensure_or_exit("nx collection reindex")

    # 3. Delete collection — via the canonical cascade, exactly like
    # `nx collection delete` (nexus-sjb52: the old direct
    # db.delete_collection(name) call raised NotImplementedError on every
    # invocation, in BOTH modes, since RDR-155 P4b made HttpVectorClient the
    # only T3 client — the verb printed "Deleting..." and died. The cascade
    # also purges the derived state a bare T3 delete would orphan, which the
    # re-index below then rebuilds).
    from nexus.db.collection_purge import purge_collection_cascade  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    click.echo(f"Deleting collection '{name}' ({before_count} chunks)...")
    # keep_quarantine: this verb re-registers the SAME name below, so the origin's rows in its
    # quarantine- siblings stay restorable instead of being deleted with the old registration
    # (nexus-wbfpw.71; `nx collection delete` takes them, this does not).
    cascade = purge_collection_cascade(db, name, keep_quarantine=True)
    for failure in cascade.failures:
        click.echo(f"warn: {failure}", err=True)
    if cascade.service_delete_failed:
        # nexus-69n4q: the service never confirmed the delete (a read timeout makes ONE attempt since
        # nexus-oq5gd), so the old collection may still be there; re-indexing now would write into it.
        # A timed-out delete may still have committed, taking the chunks' source list with it, so a blind
        # re-run of reindex can dead-end on "not found": say how to tell the two cases apart.
        raise click.ClickException(
            f"reindex of '{name}' stopped: the delete is unconfirmed (the service cascade failed), so "
            f"the collection may or may not have been deleted. Run `nx collection info {name}`: if it "
            f"still lists chunks, re-run `nx collection reindex {name}`; if it reports not found, the "
            f"delete committed: re-index from the original sources (`nx index repo` for code, "
            f"`nx index md` or `nx index pdf` for documents), and `nx collection delete {name}` "
            f"then clears any pipeline rows the unconfirmed delete left."
        )

    # 3b. Re-register the catalog_collections registry row the cascade just
    # deleted (nexus-ync6s): no re-index path calls register_collection, so
    # without this every reindex left the collection in `nx catalog doctor
    # --collections-drift`'s t3_not_in_projection bucket. Same call shape as
    # indexer.py's phase-4 registration; best-effort with a loud warning —
    # a failed projection write must not abort the re-index that follows.
    try:
        # RDR-204 Phase 3 fix round (nexus-ft04v.28 item 4): routed
        # through the registration seam (ensure_collection_registered)
        # instead of a direct writer.register_collection call -- gets
        # the seam's early EmbeddingProfileMismatchError diagnostic this
        # re-registration previously skipped.
        #
        # `discard_cached_registration` is REQUIRED here (unlike the
        # other three seam-routed sites): `name`'s row was JUST DELETED
        # by purge_collection_cascade above, so a stale per-process
        # "already registered" cache entry from earlier in this SAME
        # process would make the seam silently skip recreating it --
        # exactly the t3_not_in_projection drift this call exists to
        # close. The default registrar (make_catalog_writer, freshly
        # minted and closed after this one call) matches what this site
        # constructed manually before.
        #
        # EXPLICIT kwargs for the conformant branch, never bare
        # name-derivation: `name` already has REAL chunks under
        # whatever model its OWN name segment encodes (this is a
        # RE-registration of an existing physical collection, not a
        # fresh mint) -- collection_registration_kwargs's generic
        # derivation always recomputes embedding_model via the CURRENT
        # write-intent (nexus-ft04v.34's `effective_embedding_model_
        # for_writes`), which can disagree with the model the
        # collection's chunks were actually embedded under (an install
        # whose local.embed_model changed since the original write).
        # parse_conformant_collection_name(name) preserves the name's
        # own segments exactly like the code this replaces did.
        from nexus.corpus import (  # noqa: PLC0415 — deferred to avoid import cycle
            discard_cached_registration,
            ensure_collection_registered,
            is_conformant_collection_name,
            parse_conformant_collection_name,
        )

        discard_cached_registration(name)
        if is_conformant_collection_name(name):
            parsed = parse_conformant_collection_name(name)
            ensure_collection_registered(
                name,
                kwargs={
                    "content_type": parsed["content_type"],
                    "owner_id": parsed["owner_id"],
                    "embedding_model": parsed["embedding_model"],
                    "model_version": parsed["model_version"],
                },
            )
        else:
            ensure_collection_registered(name)
    except Exception as reg_exc:  # noqa: BLE001 — projection write is best-effort; the re-index below must proceed
        click.echo(
            f"WARNING: could not re-register catalog_collections row for "
            f"'{name}' ({reg_exc}) — `nx catalog doctor --collections-drift` "
            "will flag it until re-registered",
            err=True,
        )

    # 5. Re-index based on collection type
    # Derive corpus from collection name so chunk metadata gets correct provenance.
    # e.g. "rdr__nexus-abc123" → "nexus-abc123", "docs__manual" → "manual"
    corpus = name.split("__", 1)[1] if "__" in name else ""

    indexed = 0
    missing: list[str] = []

    # (code__ collections are refused before the delete above, nexus-caifp —
    # this dispatch starts at the prose families.)
    if split_candidate_collection_name(name)[0] == "rdr":
        rdr_files = [Path(sp) for sp in source_paths if Path(sp).exists()]
        missing = [sp for sp in source_paths if not Path(sp).exists()]
        if rdr_files:
            click.echo(f"Re-indexing {len(rdr_files)} RDR documents...")
            try:
                # nexus-3lswy: this single-collection reindex is intentionally
                # separate from `nx index repo`'s RDR path (which now routes
                # through indexer._run_index's 4th run_file_loop category via
                # _index_prose_file). batch_index_markdowns calls
                # doc_indexer._catalog_markdown_hook, which registers under a
                # SEPARATE "curator" catalog owner — safe here because this
                # command never also runs indexer._catalog_hook's batched
                # register_many pass for these files. Do not add that pass
                # here without also dropping this call, or the RDR
                # double-registration bug nexus-3lswy fixed comes back.
                batch_index_markdowns(
                    rdr_files, corpus=corpus, collection_name=name, force=True
                )
            except Exception as exc:  # noqa: BLE001 — boundary catch surfacing re-index failure to user via click.echo + Exit
                click.echo(
                    f"Re-indexing failed: {exc}\n"
                    f"Collection '{name}' was deleted. Re-run 'nx collection reindex {name}' "
                    f"after resolving the error, or re-index manually.",
                    err=True,
                )
                raise click.exceptions.Exit(1)
            indexed = len(rdr_files)

    elif split_candidate_collection_name(name)[0] in ("docs", "knowledge"):
        for sp in source_paths:
            p = Path(sp)
            if not p.exists():
                missing.append(sp)
                continue
            try:
                # nexus-8143o: force_re_embed left at its default False -- the collection was just DELETED above, so every chash here is new and there is nothing to skip.
                if p.suffix.lower() == ".pdf":
                    index_pdf(p, corpus=corpus, collection_name=name, force=True)
                else:
                    index_markdown(p, corpus=corpus, collection_name=name, force=True)
                indexed += 1
            except PdfStackError as exc:
                # The gate at 2c passed, so this is a stack that broke mid-run. It is
                # not a per-file problem: every remaining PDF would fail the same way
                # and the exit status must say so (nexus-2fyb: never silent).
                click.echo(
                    f"Re-indexing failed: {exc}\n"
                    f"Collection '{name}' was deleted. Re-run 'nx collection reindex {name}' "
                    f"after resolving the error, or re-index manually.",
                    err=True,
                )
                raise click.exceptions.Exit(exc.exit_code)
            except Exception as exc:  # noqa: BLE001 — per-file re-index failure surfaced as warning; loop continues
                click.echo(f"  Warning: failed to re-index {p.name}: {exc}", err=True)

    else:
        click.echo(f"Unknown collection type for '{name}' — no re-index strategy available")

    # 6. Warn about missing source files
    if missing:
        click.echo(f"Warning: {len(missing)} source files not found (moved or deleted)")
        for m in missing[:5]:
            click.echo(f"  - {m}")
        if len(missing) > 5:
            click.echo(f"  ... and {len(missing) - 5} more")

    # 7. Report before/after counts and verify
    try:
        after_info = db.collection_info(name)
        after_count = after_info["count"]
    except KeyError:
        after_count = 0

    click.echo(
        f"Re-indexed: {before_count} -> {after_count} chunks ({indexed} sources processed)"
    )

    if after_count >= 2:
        try:
            result = verify_collection_deep(db, name)
            dist_str = (
                f" (distance: {result.distance:.4f})" if result.distance is not None else ""
            )
            click.echo(f"Verify: {result.status}{dist_str}")
        except Exception as exc:  # noqa: BLE001 — verify failure surfaced to user via click.echo, non-fatal
            click.echo(f"Verify failed: {exc}", err=True)

    # GH #369: run the same post-processing chain ``nx index repo``
    # runs after a successful index — taxonomy discover, Claude
    # labeling, cross-collection projection, cooccurrence links,
    # topic-link compute, L1 context refresh. Pre-fix the chain only
    # fired from index_repo_cmd, so a bulk reindex (e.g. after an
    # embedding-model upgrade) left the catalog/taxonomy/links stale
    # and the operator had to know to re-run ``nx index repo`` per
    # repo. ``run_collection_postprocessing`` is now the shared entry
    # point.
    if indexed > 0:
        # nexus-m20mf P3 fold-in (critic finding 2): this command is a
        # DIFFERENT Click group from `index` (which builds its own shared
        # client per `nx index repo` invocation) -- `run_collection_
        # postprocessing` takes an explicit client= specifically so it
        # never silently falls back to "whichever Click group happens to
        # be active" (there was no such group active here before this
        # fix, so this command's single call always built 8 unshared
        # httpx.Client()s). Built locally (not at the `collection` group
        # level) since this is the ONLY `collection` subcommand that
        # touches T2 -- building one for every other subcommand would be
        # pure overhead. Closed in the finally below, by the owner that
        # built it (never by run_collection_postprocessing itself).
        from nexus.db.t2._refreshable_client import build_shared_t2_client  # noqa: PLC0415 — deferred to avoid circular import at module load
        _t2_client = build_shared_t2_client()
        try:
            from nexus.commands.index import run_collection_postprocessing  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
            # Resolve repo_path from the registry when available so the
            # L1 context refresh fires; falls back to ``None`` for
            # collections with no registered owner (the L1 step is the
            # only thing in the chain that requires a path, and it
            # short-circuits cleanly when ``repo_path`` is ``None``).
            # RDR-137 Phase 3.4 (nexus-tts0d.9): catalog-backed lookup.
            # Find the collection's owner, then read repo_root from
            # owners. Falls back to RepoRegistry walk only if catalog
            # has no row for this collection (pre-Phase-1.5a installs).
            repo_path: Path | None = None
            try:
                from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
                from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
                cat = make_catalog_reader()
                if cat is not None:
                    # nexus-xnz0o: replaced two chained _db.execute calls with
                    # get_collection_owner_root() which is uniform across SQLite
                    # and service mode.
                    owner_id, repo_root_str = cat.get_collection_owner_root(name)
                    if repo_root_str:
                        repo_path = Path(repo_root_str)
                if repo_path is None:
                    # Fallback: legacy registry walk for pre-Phase-1.5a
                    # installs where collections.owner_id is empty.
                    from nexus.repos import _read_repos_json  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
                    for rp_str, info in _read_repos_json(
                        nexus_config_dir() / "repos.json"
                    ).items():
                        coll = info.get("collection") or info.get("docs_collection")
                        # RDR-137 followup SIG-13 (nexus-43qgm.13):
                        # also check code_collection so reindex on a
                        # code__* collection resolves on pre-Phase-1.5a
                        # installs. Pre-fix the omission silently
                        # skipped L1 refresh for code reindexes.
                        if (
                            coll == name
                            or info.get("docs_collection") == name
                            or info.get("code_collection") == name
                        ):
                            repo_path = Path(rp_str)
                            break
            except Exception:  # noqa: BLE001 — best-effort repo-path resolution; non-fatal
                pass  # repo-path resolution is best-effort
            run_collection_postprocessing([name], repo_path=repo_path, client=_t2_client)
        except Exception as exc:  # noqa: BLE001 — post-processing failure surfaced via click.echo, non-fatal
            click.echo(
                f"Note: post-processing (taxonomy / links / context) "
                f"failed: {exc}. Run `nx index repo <path>` to retry.",
                err=True,
            )
        finally:
            _t2_client.close()


@collection.command("verify")
@click.argument("name")
@click.option("--deep", is_flag=True, help="Run embedding probe query to verify index health")
def verify_cmd(name: str, deep: bool) -> None:
    """Verify a collection exists and report its document count."""
    from nexus.db.t3 import verify_collection_deep  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    db = _t3()
    cols = db.list_collections()
    match = next((c for c in cols if c["name"] == name), None)
    if match is None:
        # RDR-103 follow-up: a legacy two-segment name should still
        # resolve when the on-disk collection is the conformant
        # auto-promoted form (``foo__voyage-context-3__v1``). Same
        # fallback as ``resolve_corpus`` in nexus.corpus.
        from nexus.corpus import resolve_corpus  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

        candidates = resolve_corpus(name, [c["name"] for c in cols])
        if len(candidates) == 1:
            resolved = candidates[0]
            match = next(c for c in cols if c["name"] == resolved)
            name = resolved
        else:
            raise click.ClickException(
                f"collection not found: {name!r} — use: nx collection list"
            )

    if not deep:
        click.echo(f"Collection '{name}': {match['count']} chunks — OK")
        return

    try:
        result = verify_collection_deep(db, name)
    except KeyError:
        raise click.ClickException(f"collection not found: {name!r} — use: nx collection list")
    except Exception as exc:  # noqa: BLE001 — embedding-probe failure surfaced to user via click.echo + Exit
        click.echo(
            f"embedding probe failed for '{name}': {exc} — check voyage_api_key with: nx config get voyage_api_key",
            err=True,
        )
        raise click.exceptions.Exit(1)

    if result.status == "skipped":
        click.echo(
            f"Collection '{name}': {result.doc_count} chunks — skipped (too few for probe)"
        )
        return

    dist_str = (
        f" (distance: {result.distance:.4f}, {result.metric})"
        if result.distance is not None
        else ""
    )
    if result.probe_hit_rate is not None:
        hit_str = f" [{result.probe_hit_rate:.0%} probe hit rate]"
    else:
        hit_str = ""
    if result.status == "healthy":
        click.echo(f"Collection '{name}': {result.doc_count} chunks — embedding health OK{dist_str}{hit_str}")
    elif result.status == "degraded":
        click.echo(
            f"Collection '{name}': {result.doc_count} chunks — DEGRADED: {result.probe_hit_rate:.0%} probe hit rate{dist_str}",
            err=True,
        )
        raise click.exceptions.Exit(1)
    elif result.status == "broken":
        click.echo(
            f"Collection '{name}': {result.doc_count} chunks — BROKEN: 0% probe hit rate (no probes found in top-10){dist_str}",
            err=True,
        )
        raise click.exceptions.Exit(1)


_REEMBED_SUPPORTED_MODELS = ("voyage-3", "voyage-code-3", "voyage-context-3")

#: Chunks per upsert request during a re-embed (nexus-tysei). Every chunk in
#: the request is embedded server-side before it answers, and the edge gives
#: /v1/vectors/upsert-chunks 55 s (the engine is told 50 s and aborts CCE work
#: past it). A 300-row page is about 25 Voyage calls in three waves: 10-30 s
#: on a normal minute, past 50 s on a slow one, and an abort loses the whole
#: request. 100 keeps one wave's worth of margin.
_REEMBED_UPSERT_BATCH = 100

#: Resends of one re-embed batch after the engine refused it as ownerless
#: (RDR-223 Phase 3 Step 2). Each resend follows a live re-read that dropped at
#: least the chunks that lost their owner, so a batch converges in one or two;
#: the bound is only for a refusal the re-read cannot explain.
_REEMBED_OWNER_RETRIES = 3


def _upsert_reembed_batch(
    db, col, col_name: str,
    ids: list[str], docs: list[str], metas: list,
) -> tuple[list[str], list[str], list, int]:
    """Write one re-embed batch; returns ``(ids, docs, metas, dropped)`` of what was written.

    RDR-223 Phase 3 Step 2: the engine refuses a whole ``upsert-chunks`` request when any
    chash in it has no live manifest row. A chunk can lose its owner between this command's
    live read of the page and this write (a note superseded, a document deleted, by another
    process), which would abort the command on the first such race with no way to resume.
    On that refusal the batch is re-read through the live-filtered get and only the chashes
    still owned are resent; the ones that vanished are counted in ``dropped`` and are not
    written, so no ownerless chunk is recreated for them.

    Any other failure propagates. A refusal the re-read cannot explain (every chash still
    reads live) is retried a bounded number of times, then raised.
    """
    from nexus.db.engine_reasons import OWNERLESS_CHUNK_WRITE_REASON  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
    from nexus.db.http_vector_client import VectorServiceError  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    dropped = 0
    for attempt in range(_REEMBED_OWNER_RETRIES + 1):
        if not ids:
            return [], [], [], dropped
        try:
            db.upsert_chunks(col_name, ids, docs, metadatas=metas, force_re_embed=True)
            return ids, docs, metas, dropped
        except VectorServiceError as exc:
            if exc.reason != OWNERLESS_CHUNK_WRITE_REASON or attempt == _REEMBED_OWNER_RETRIES:
                raise
            live = set((col.get(ids=ids, include=[]).get("ids")) or [])
            keep = [i for i, cid in enumerate(ids) if cid in live]
            lost = len(ids) - len(keep)
            _log.warning(
                "reembed_batch_refused_ownerless",
                collection=col_name, requested=len(ids), still_owned=len(keep),
                lost_owner=lost, attempt=attempt + 1,
            )
            dropped += lost
            ids = [ids[i] for i in keep]
            docs = [docs[i] for i in keep]
            metas = [metas[i] for i in keep]
    return [], [], [], dropped  # unreachable: the loop returns or raises


def _reembed_collection(
    db,
    col_name: str,
    target_model: str,
    *,
    dry_run: bool,
    on_progress=None,
    hooks=None,
) -> tuple[int, int]:
    """Re-embed every LIVE chunk in *col_name* with *target_model*.

    Preserves chunk id, document text, and metadata. Only the embedding
    vector changes. Returns ``(processed, skipped)``. A chunk with no live
    catalog document is not read and keeps its vector (see the decision
    comment below); the caller reports how many stored chunks that left behind.

    nexus-bw65: in-place re-embed for Voyage models. CCE
    (``voyage-context-3``) was refused here while the CLIENT embedded,
    because the client path sent a document's chunks together for context.
    Since nexus-sghyo the engine embeds server-side, and ``CceEmbedder``
    sends every chunk as its own single-chunk document (nexus-u2mlh.1), so
    a server-side re-embed of stored text is exactly the vector a fresh
    index would write. That is what repairs chunks whose stored vector was
    computed with document context by the retired client path (nexus-tysei).

    nexus-sghyo (Hal determination 2026-07-28): the client no longer
    embeds via Voyage. Every write below routes through
    ``db.upsert_chunks(..., force_re_embed=True)`` with NO client-computed
    ``embeddings=`` — the server (or, for the test-only T3Database facade,
    the collection's injected embedding function) computes the vector.
    This retires the old "verbatim passthrough" optimisation (client
    embeds once, server skips its own billed re-embed) — there is no
    longer a client embed to avoid duplicating, so a single server-side
    embed per chunk is both correct and now the cheapest option.
    """
    from nexus.db.limits import QUOTAS  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    try:
        col = db.get_collection(col_name)
        # count() inside the same boundary: the service stub propagates
        # transient VectorServiceError raw (tail review, nexus-c9xr2) —
        # surface it as the file's ClickException convention, not a
        # traceback.
        total = col.count()
    except click.ClickException:
        raise
    except Exception as exc:  # noqa: BLE001 — boundary catch re-raised as ClickException with context
        raise click.ClickException(
            f"collection {col_name!r}: {type(exc).__name__}: {exc}"
        )
    if total == 0:
        return 0, 0

    # LIVE ROWS ONLY, deliberately (nexus-wbfpw.35 fix rounds 2-3). `total` is the
    # STORED count (an upper bound: the loop below ends on the first empty live
    # page, so hidden rows only make it stop earlier than `total`), while the
    # reads below see live rows. Chunks with no live owner are not re-embedded.
    # For a never-owned chunk the write would refresh last_written_at, the anchor
    # of the reaper's grace window (vectors-020), and extend its life; that grace
    # argument holds for those chunks only. For a chunk owned only by a
    # tombstoned document the refresh extends nothing under the final design
    # (nexus-wbfpw.15: nexus.chunk_orphaned_at, reapable keyed on
    # GREATEST(last_written_at, orphaned_at)); the reasons there are cost, since
    # every re-embed is a billed Voyage call per chunk, and pointlessness, since
    # the document is deleted. A document restored later keeps its pre-repair
    # vectors until the next run, which sees them live. The chunks left on the
    # old model are counted by the caller and reported.
    processed = 0
    skipped = 0
    page = QUOTAS.MAX_QUERY_RESULTS  # 300
    offset = 0
    while offset < total:
        page_rows = col.get(
            limit=page, offset=offset,
            include=["documents", "metadatas"],
        )
        ids = page_rows.get("ids") or []
        documents = page_rows.get("documents") or []
        metadatas = page_rows.get("metadatas") or []
        if not ids:
            break

        valid_idx = [
            i for i, d in enumerate(documents)
            if isinstance(d, str) and d.strip()
        ]
        skipped += len(ids) - len(valid_idx)
        if not valid_idx:
            offset += page
            continue

        v_ids = [ids[i] for i in valid_idx]
        v_docs = [documents[i] for i in valid_idx]
        v_metas = [metadatas[i] for i in valid_idx]

        if not dry_run:
            # Stamp the new model on metadata so check_staleness reads
            # right post-re-embed; otherwise next index pass would treat
            # every chunk as stale and re-embed twice.
            for m in v_metas:
                if isinstance(m, dict):
                    m["embedding_model"] = target_model
            # nexus-sghyo: no client-computed embeddings — the command
            # guard (reembed_cmd) has already pinned target_model ==
            # the row's model for service-backed handles, so the
            # server re-embeds with the correct model. force_re_embed=True
            # bypasses the existence-partition skip so every chash is
            # genuinely recomputed, not treated as already-current.
            # RDR-223 Phase 3 Step 2: a batch the engine refuses as ownerless (a chunk
            # lost its owner since the page was read) is narrowed to the chunks still
            # owned and resent; ``written_*`` is what actually landed.
            written_ids: list[str] = []
            written_docs: list[str] = []
            written_metas: list = []
            for s in range(0, len(v_ids), _REEMBED_UPSERT_BATCH):
                b_ids, b_docs, b_metas, b_dropped = _upsert_reembed_batch(
                    db, col, col_name,
                    v_ids[s:s + _REEMBED_UPSERT_BATCH],
                    v_docs[s:s + _REEMBED_UPSERT_BATCH],
                    v_metas[s:s + _REEMBED_UPSERT_BATCH],
                )
                written_ids += b_ids
                written_docs += b_docs
                written_metas += b_metas
                skipped += b_dropped
            v_ids, v_docs, v_metas = written_ids, written_docs, written_metas
            # nexus-bw65 / nexus-9099: fire post-store chains so the
            # invariant 'every CLI T3 write also fires the chain'
            # (test_every_cli_t3_write_function_fires_store_chains)
            # holds. Re-embed preserves doc_id / chash / manifest
            # position, so the chain's hooks (taxonomy_assign,
            # manifest_write; the chash dual-write hook was retired by
            # RDR-187/nexus-piwya.4) re-touch existing rows idempotently.
            if hooks is None:
                from nexus.hook_registry import HookRegistry, install_default_hooks  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
                hooks = HookRegistry()
                install_default_hooks(hooks)

            if v_ids:  # every chunk of the page may have lost its owner
                hooks.fire_store_chains(
                    v_ids, col_name, v_docs,
                    source_paths=[
                        (m.get("source_path", "") if isinstance(m, dict) else "")
                        for m in v_metas
                    ],
                    # nexus-sghyo: no client-computed vector to pass — the
                    # server (or test EF) computed it inside upsert_chunks
                    # above.
                    embeddings=None,
                    metadatas=v_metas,
                    catalog_doc_id="",
                )

        processed += len(v_ids)
        if on_progress is not None:
            on_progress(processed, total)
        offset += page

    return processed, skipped


def _reembed_source_model(db: object, name: str) -> str:
    """The model the engine will embed *name* with: the catalog row's
    ``embedding_model`` when a row backs the name (RDR-210 Technical Design 10;
    the engine routes by the row, never by a name segment), else the model the
    name encodes. The row read goes through the client's own resolver, so it
    never crosses tenants (nexus-dxifx)."""
    from nexus.corpus import resolve_row_preferred  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    model = resolve_row_preferred(
        name, "embedding_model", embedding_model_for_collection,
        row_resolver=getattr(db, "_resolve_collection_row", None),
    )
    return model if model else embedding_model_for_collection(name)


#: The model the on-device engine embeds with (the bge profile). ``--to`` takes it since RDR-210 Phase 6.
_REEMBED_BGE_MODEL = "bge-base-en-v15-768"

#: The ``--status`` value that means "every job", given when the flag carries no job id.
_ALL_JOBS = "__all__"

#: The states after which the target holds the moved documents and its topics are due (``--wait`` runs the discover).
_DISCOVER_STATES = frozenset({"done", "done_with_skips"})


#: The 404 bodies the re-embed handler itself sends (``ReembedHandler``): an unknown job, an unregistered source.
#: These are answers, shown verbatim. Any other 404 (a route the engine does not have) means the engine is older
#: than the routes.
_ENGINE_404_MESSAGES = re.compile(r"^(?:no re-embed job \S+|source collection .+ is not registered)$")


def _reembed_job_error(exc: Exception) -> click.ClickException:
    """A re-embed route's refusal as a ClickException that says what the engine said.

    503 is the engine's local-mode refusal (a managed engine, or one started without the supervisor's
    ``NX_ENGINE_LOCAL``); 404 is an engine below the routes, unless its body is one the handler itself sends; 409 names the job that holds the source or the
    state a control was refused from.
    """
    code = getattr(exc, "code", None)
    body = getattr(exc, "engine_body", None)
    text = str(body.get("error")) if isinstance(body, dict) and body.get("error") else str(exc)
    if code == 404 and not _ENGINE_404_MESSAGES.match(text):
        return click.ClickException(
            f"this engine does not serve the re-embed routes ({text}); upgrade it "
            "(nx upgrade, then nx daemon service stop && nx daemon service start)."
        )
    if code == 503:
        return click.ClickException(f"the engine refuses the re-embed job: {text}")
    if isinstance(body, dict) and body.get("job_id"):
        return click.ClickException(f"{text} (job {body['job_id']}, {body.get('state', '?')})")
    return click.ClickException(text)


def _require_local_engine(db: object) -> None:
    """The re-embed job runs on the local engine only (Sam, 2026-10-17): refuse in any other mode."""
    from nexus.config import is_local_mode  # noqa: PLC0415 — deferred import; patched in tests at the module attribute
    from nexus.db.http_vector_client import is_service_backed  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    if not is_local_mode():
        raise click.ClickException(
            "the cross-model re-embed job runs on the local engine only; this install is not in local mode. "
            "A managed install re-indexes from source (nx index repo / nx index pdf)."
        )
    if not is_service_backed(db):
        raise click.ClickException("the re-embed job needs the nexus service; this handle is not backed by it.")


def _job_line(job: dict) -> str:
    """One job row as a line: id, state (disposition, failure reason), source to target, counts."""
    state = str(job.get("state"))
    if job.get("disposition"):
        state += f"/{job['disposition']}"
    if job.get("failure_reason"):
        state += f" ({job['failure_reason']})"
    cap = job.get("token_cap")
    tokens = f"{job.get('tokens_billed', 0)} of {cap}" if cap else f"{job.get('tokens_billed', 0)}"
    line = (
        f"{job.get('job_id')}  {state}  {job.get('source_collection')} -> {job.get('target_collection')}  "
        f"moved {job.get('documents_moved', 0)}, skipped {job.get('documents_skipped', 0)}, "
        f"embedded {job.get('chunks_embedded', 0)}, reused {job.get('chunks_reused', 0)}, tokens {tokens}"
    )
    if job.get("last_error"):
        line += f"\n    last error: {job['last_error']}"
    # RDR-231: each skipped document with its reason; a source_uri the sibling already holds names the holder.
    for skipped in job.get("skipped_documents") or ():
        holder = skipped.get("holder")
        line += f"\n    skipped {skipped.get('doc_id')}: {skipped.get('reason')}" + (
            f" (source_uri held by {holder})" if holder else ""
        )
    counts = job.get("delete_counts")
    if isinstance(counts, dict):
        line += (
            "\n    the source's delete removes: "
            f"{counts.get('quarantine_rows', 0)} quarantine row(s); "
            f"{counts.get('foreign_topic_assignments', 0)} topic assignment(s) of other collections; "
            f"{counts.get('topic_links', 0)} topic link(s); "
            f"{counts.get('unmatched_satellite_rows', 0)} satellite row(s) that match no moved document"
        )
    if job.get("state") in _DISCOVER_STATES:
        from nexus.reembed_job import discover_command  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

        line += (
            f"\n    next: {discover_command(str(job.get('target_collection')))} "
            f"(topic search misses the moved documents until it has run; --wait runs it)"
        )
    return line


def _reembed_controls(
    db: object, *, status_job: str | None, pause_job: str | None, resume_job: str | None,
    abort_job: str | None, token_cap: int | None, wait: bool = False,
) -> None:
    """``--status [JOB]``, ``--pause JOB``, ``--resume JOB`` (``--token-cap`` raises the cap; ``--wait`` waits on
    it), ``--abort JOB``."""
    _require_local_engine(db)
    try:
        if status_job is not None:
            jobs = db.reembed_status(counts=True)  # type: ignore[attr-defined]
            _log.info("reembed_status_poll", jobs=len(jobs), job=None if status_job == _ALL_JOBS else status_job)
            if status_job != _ALL_JOBS:
                jobs = [j for j in jobs if j.get("job_id") == status_job]
                if not jobs:
                    raise click.ClickException(f"no re-embed job {status_job}")
            if not jobs:
                click.echo("No re-embed jobs.")
            for job in jobs:
                click.echo(_job_line(job))
            return
        if pause_job is not None:
            job = db.reembed_pause(pause_job)  # type: ignore[attr-defined]
        elif resume_job is not None:
            job = db.reembed_resume(resume_job, token_cap=token_cap)  # type: ignore[attr-defined]
        else:
            job = db.reembed_abort(abort_job)  # type: ignore[attr-defined]
    except click.ClickException:
        raise
    except Exception as exc:  # noqa: BLE001 — boundary: the engine's refusal reaches the user as its own words
        raise _reembed_job_error(exc) from exc
    unchanged = " (already in that state; nothing changed)" if job.get("unchanged") else ""
    click.echo(_job_line(job) + unchanged)
    if wait:
        _wait_and_report(db, [job])


def _run_taxonomy_discover(target: str) -> bool:
    """``nx taxonomy discover --collection TARGET`` in-process (what ``nx index`` runs after indexing). True on success.

    A seam: tests replace it. A failure is reported and returned, never raised: the job is done whether or not its
    topics could be built, and the command to retry is in the message.
    """
    from nexus.commands.taxonomy_cmd import discover_cmd  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
    from nexus.reembed_job import discover_command  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    try:
        discover_cmd.callback(collection=target, discover_all=False, force=False)  # type: ignore[misc]
    except Exception as exc:  # noqa: BLE001 — boundary: the job finished; a failed discover is told, with its retry command
        _log.warning("reembed_wait_discover_failed", target=target, error=str(exc))
        click.echo(f"taxonomy discover for {target} failed ({exc}); run: {discover_command(target)}", err=True)
        return False
    return True


def _wait_and_report(db: object, jobs: list[dict]) -> None:
    """``--wait``: poll until every job in *jobs* is done, done_with_skips, aborted, failed or paused; name each state.

    Runs taxonomy discover for the target of each job that ended ``done`` or ``done_with_skips`` (once per target).
    Exits 1 unless every job finished and every discover ran: aborted, failed, paused and blocked-in-cutover jobs stopped short and need a person, and a failed discover is told with its retry command. A Ctrl-C
    leaves the jobs running (the engine owns them) and says how to look again.
    """
    from nexus.reembed_job import DISCOVER_STATES, FINISHED_STATES, WaitError, is_blocked, wait_for_jobs  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    ids = [str(j.get("job_id")) for j in jobs]
    if not ids:
        return
    click.echo(f"Waiting on {len(ids)} job(s); the engine keeps running them if this is interrupted.")

    def changed(row: dict) -> None:
        click.echo(f"{row.get('job_id')}: {row.get('state')}")

    try:
        final = wait_for_jobs(
            lambda: db.reembed_status(),  # type: ignore[attr-defined]
            ids, sleep=time.sleep, on_change=changed,
        )
    except KeyboardInterrupt:
        click.echo("Stopped waiting. Look again with: nx collection re-embed --status", err=True)
        raise click.exceptions.Exit(130) from None
    except WaitError as exc:
        raise click.ClickException(str(exc)) from exc
    except click.ClickException:
        raise
    except Exception as exc:  # noqa: BLE001 — boundary: an unreadable engine is the whole answer
        raise _reembed_job_error(exc) from exc
    discovered: set[str] = set()
    stopped_short = False
    discover_failed = False
    for job_id in ids:
        row = final[job_id]
        state = str(row.get("state"))
        blocked = " (blocked: the job waits for a person; its last error says why)" if is_blocked(row) else ""
        click.echo(f"{_job_line(row)}\n  -> {state}{blocked}")
        if state not in FINISHED_STATES:
            stopped_short = True
        if state in DISCOVER_STATES:
            target = str(row.get("target_collection"))
            if target not in discovered:
                discovered.add(target)
                click.echo(f"Discovering topics for {target}...")
                if not _run_taxonomy_discover(target):
                    discover_failed = True
    if stopped_short or discover_failed:
        raise click.exceptions.Exit(1)


class _Planned:
    """One source's plan, or the reason it cannot start."""

    def __init__(self, row: dict, target_model: str | None) -> None:
        self.row = row
        self.target_model = target_model
        self.plan: dict | None = None
        self.cap: int | None = None
        self.refusal: str | None = None


def _plan_lines(p: _Planned) -> list[str]:
    """The plan as text: what moves, what it costs, what the source's delete takes with it."""
    plan = p.plan or {}
    lines = [
        f"{p.row.get('name')} ({p.row.get('embedding_model')}) -> {plan.get('target_collection')} "
        f"({p.target_model}); target: {plan.get('target_action')}",
        f"  documents: {plan.get('documents', 0)} live to move, {plan.get('tombstoned_documents', 0)} tombstoned "
        f"(not moved; a source holding one is kept, not deleted)",
        f"  chunks: {plan.get('chunks', 0)} to embed; {plan.get('ownerless_chunks', 0)} ownerless (not moved)",
    ]
    if p.cap is not None:
        lines.append(f"  tokens: about {plan.get('token_estimate', 0):,} estimated, cap {p.cap:,}")
    else:
        lines.append(
            f"  tokens: none billed (the model runs on this machine; no cap). {plan.get('chunks', 0)} chunk(s) "
            f"to embed here; the time depends on the machine"
        )
    lines.append(
        "  the source's delete removes (--keep-source keeps the source and all of it): "
        f"{plan.get('quarantine_rows', 0)} quarantine row(s); "
        f"{plan.get('foreign_topic_assignments', 0)} topic assignment(s) of other collections that point at its "
        f"topics; {plan.get('topic_links', 0)} topic link(s)"
    )
    lines.append(
        f"  satellite rows naming it that match no document the job moves: {plan.get('unmatched_satellite_rows', 0)} "
        f"(the source is kept, not deleted, while any remain unless --force-delete)"
    )
    homed = plan.get("live_homed_elsewhere")
    if homed is None:
        # An engine that predates the field: say what the plan cannot, rather than print a zero it did not count.
        lines.append(
            "  this engine's plan does not count live documents homed here whose chunks sit in another collection; "
            "the cutover's parity check refuses to delete while any remain"
        )
    else:
        lines.append(
            f"  live documents homed here whose chunks sit in another collection: {homed} "
            f"(not moved; the cutover's parity check fails the job and deletes nothing while any remain, "
            f"--force-delete does not lift it)"
        )
    return lines


def _plan_source(
    db: object, row: dict, profile: dict[str, str], served: list[str] | None, token_cap: int | None,
) -> _Planned:
    """Plan one source: the profile's model, can the engine serve it, the engine's plan, the cap."""
    from nexus.reembed_job import (  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
        effective_token_cap,
        TargetNameError,
        missing_piece,
        target_collection_name,
    )

    name = str(row.get("name"))
    content_type = str(row.get("content_type"))
    target_model = profile.get(content_type)
    planned = _Planned(row, target_model)
    if target_model is None:
        planned.refusal = f"{name}: the engine's profile has no model for content type {content_type}"
        return planned
    if served is not None and (piece := missing_piece(target_model, served)):
        planned.refusal = f"{name}: {piece}"
        return planned
    try:
        target_name = target_collection_name(
            content_type, row.get("owner_id"), target_model,
            tenant_ids=(row.get("tenant_id"), getattr(db, "_tenant", None)),
        )
    except TargetNameError as exc:
        planned.refusal = f"{name}: {exc}"
        return planned
    try:
        planned.plan = db.reembed_plan(name, target_collection=target_name)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 — boundary: the engine's refusal is this source's reason
        planned.refusal = f"{name}: {_reembed_job_error(exc).message}"
        return planned
    _log.info(
        "reembed_plan_submitted", source=name, target=planned.plan.get("target_collection"),
        target_model=target_model, chunks=planned.plan.get("chunks"),
        token_estimate=planned.plan.get("token_estimate"),
    )
    planned.cap = effective_token_cap(target_model, token_cap)
    estimate = int(planned.plan.get("token_estimate") or 0)
    if planned.cap is not None and estimate > planned.cap:
        planned.refusal = (
            f"{name}: the plan needs about {estimate:,} tokens, above the cap of {planned.cap:,}. A Voyage "
            f"re-embed bills them; raise the cap with --token-cap N to run it."
        )
    return planned


def _reembed_jobs(
    db: object,
    sources: list[dict],
    *,
    profile: dict[str, str],
    held: list[tuple[dict, dict]],
    reentries: list,
    dry_run: bool,
    yes: bool,
    keep_source: bool,
    force_delete: bool,
    token_cap: int | None,
    skipped: list[str] | None = None,
    wait: bool = False,
) -> None:
    """Plan each source and, with ``--no-dry-run``, start one job per source and re-enter each ``kept_tombstoned`` job.

    Every source is planned before any is started, and a refusal on any (the cap, an engine that cannot serve
    the target, a source another job holds as a target) stops the whole run before a write: the RDR's "refused
    before any write". A re-entry is not planned: the engine's job keeps its own target, cap and flags, so
    ``--keep-source``, ``--force-delete`` and ``--token-cap`` do not reach it (the run says so).
    """
    served = db.served_embedding_models()  # type: ignore[attr-defined]
    planned = [_plan_source(db, row, profile, served, token_cap) for row in sources]
    for p in planned:
        if p.plan is not None:
            for line in _plan_lines(p):
                click.echo(line)
            if p.cap is None and token_cap is not None:
                click.echo("  --token-cap is not sent: this model has no cap")
    for row, job in held:
        click.echo(
            f"{row.get('name')}: held by job {job.get('job_id')} ({job.get('state')}); "
            f"not planned. nx collection re-embed --status {job.get('job_id')}"
        )
    for r in reentries:
        what = (
            "a document is live in the source again, so the job moves it and then finishes"
            if r.kind == "running" else "the source's tombstones are gone, so the job finishes"
        )
        click.echo(
            f"{r.source_row.get('name')}: re-entry of job {r.job.get('job_id')} ({r.kind}): {what}; the job keeps "
            f"its own target, token cap, keep_source and force_delete."
        )
    for name in skipped or ():
        click.echo(f"{name}: skipped (--skip)")
    refused = [p for p in planned if p.refusal]
    if refused:
        for p in refused:
            click.echo(f"refused: {p.refusal}", err=True)
        raise click.exceptions.Exit(1)
    if not planned and not reentries:
        if not held and not skipped:
            click.echo("No collection is off the profile.")
        return
    if dry_run:
        click.echo(
            f"dry-run: {len(planned)} job(s) would start"
            + (f" and {len(reentries)} would be re-entered" if reentries else "")
            + ". Pass --no-dry-run --yes to start them."
            + (" --wait has nothing to wait for in a dry run." if wait else "")
        )
        return
    if reentries and (keep_source or force_delete or token_cap is not None):
        click.echo(
            "--keep-source, --force-delete and --token-cap apply to the jobs started now, not to a re-entered job, "
            "which keeps what it was started with.",
            err=True,
        )
    if not yes:
        billed = sum(int((p.plan or {}).get("token_estimate") or 0) for p in planned if p.cap is not None)
        caps = sorted({p.cap for p in planned if p.cap is not None})
        local_chunks = sum(int((p.plan or {}).get("chunks") or 0) for p in planned if p.cap is None)
        cost = []
        if caps:
            cost.append(
                f"About {billed:,} tokens will be billed in all (each Voyage job is capped at "
                f"{', '.join(f'{c:,}' for c in caps)} tokens on its own)."
            )
        if local_chunks:
            cost.append(f"{local_chunks:,} chunk(s) are embedded on this machine and bill nothing.")
        click.confirm(
            f"Start {len(planned)} re-embed job(s)"
            + (f" and re-enter {len(reentries)}" if reentries else "")
            + f"? Each moves its collection's documents to the profile's model "
            f"and then {'keeps' if keep_source else 'deletes'} the source. {' '.join(cost)}".rstrip(),
            abort=True,
        )
    failures = 0
    started: list[dict] = []
    for p in planned:
        name = str(p.row.get("name"))
        try:
            job = db.reembed_start(  # type: ignore[attr-defined]
                name,
                target_collection=(p.plan or {}).get("target_collection"),
                token_cap=p.cap,
                keep_source=keep_source,
                force_delete=force_delete,
            )
        except Exception as exc:  # noqa: BLE001 — boundary: one source's refusal must not stop the others
            failures += 1
            click.echo(f"{name}: not started: {_reembed_job_error(exc).message}", err=True)
            continue
        started.append(job)
        click.echo(f"started {_job_line(job)}")
    for r in reentries:
        name = str(r.source_row.get("name"))
        try:
            job = db.reembed_reenter(name)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 — boundary: one source's refusal must not stop the others
            failures += 1
            click.echo(f"{name}: not re-entered: {_reembed_job_error(exc).message}", err=True)
            continue
        started.append(job)
        click.echo(f"re-entered {_job_line(job)}")
    if wait and started:
        _wait_and_report(db, started)
    if failures:
        raise click.exceptions.Exit(1)
    if not wait:
        click.echo("Watch with: nx collection re-embed --status")
        from nexus.reembed_job import discover_command  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

        targets = sorted({str(j.get("target_collection")) for j in started})
        if targets:
            click.echo(
                "When a job is done, build its target's topics (topic search misses the moved documents until "
                "then; --wait does it): " + "; ".join(discover_command(t) for t in targets)
            )


def _reembed_all(
    db: object, *, dry_run: bool, yes: bool, keep_source: bool, force_delete: bool, token_cap: int | None,
    skip: tuple[str, ...] = (), wait: bool = False,
) -> None:
    """``--all``: every live collection off the engine's profile, one job each, less any ``--skip`` NAME.

    nexus-kyzmw: a skipped source is not planned and is listed as skipped, so a source that is refused for
    good (a held target name, a target collision) does not make ``--all`` unusable. Without ``--skip`` the
    run stays all-or-nothing: a refused plan starts nothing. A NAME that is not one of ``--all``'s candidates
    (stale, held by a job, or a re-entry) is an error.
    """
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
    from nexus.reembed_job import candidates, profile_models  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    _require_local_engine(db)
    reader = make_catalog_reader()
    try:
        profile = profile_models(reader)
        rows = reader.list_collections()
        jobs = db.reembed_status()  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 — boundary: an unreadable catalog or engine is the whole answer
        raise _reembed_job_error(exc) from exc
    if not profile:
        raise click.ClickException(
            "the engine has written no embedding profile for this tenant yet, so there is no model to re-embed "
            "to (the first collection registration seeds it)."
        )
    counts: dict[bool, dict[str, int]] = {}

    def doc_count(deleted: bool) -> Callable[[str], int]:
        def count(name: str) -> int:
            if deleted not in counts:
                counts[deleted] = reader.collection_doc_counts(include_deleted=deleted)
            return counts[deleted].get(name, 0)
        return count

    found = candidates(rows, profile, jobs, doc_count(False), doc_count(True))
    skipped = sorted(set(skip))
    if skipped:
        known = sorted(
            {str(r.get("name")) for r in found.stale}
            | {str(row.get("name")) for row, _job in found.held}
            | {str(r.source_row.get("name")) for r in found.reentries}
        )
        unknown = [n for n in skipped if n not in known]
        if unknown:
            raise click.ClickException(
                f"--skip names {', '.join(unknown)}, which is not one of --all's candidates (stale, held by a "
                f"job, or a re-entry); they are: "
                f"{', '.join(known) or '(none)'}."
            )
    _reembed_jobs(
        db, [r for r in found.stale if str(r.get("name")) not in skipped], profile=profile,
        held=[(row, j) for row, j in found.held if str(row.get("name")) not in skipped],
        reentries=[r for r in found.reentries if str(r.source_row.get("name")) not in skipped],
        dry_run=dry_run, yes=yes, keep_source=keep_source, force_delete=force_delete, token_cap=token_cap,
        skipped=skipped, wait=wait,
    )


def _kept_tombstoned_job(db: object, name: str, row: dict) -> dict | None:
    """The newest job on *name* when it ended ``done``/``kept_tombstoned`` and *name* is still superseded by its target."""
    try:
        jobs = db.reembed_status()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — a hint only: an unreadable status leaves the plain refusal
        return None
    newest = next((j for j in jobs if j.get("source_collection") == name), None)
    if (
        newest is not None and newest.get("state") == "done" and newest.get("disposition") == "kept_tombstoned"
        and newest.get("target_collection") == row.get("superseded_by")
    ):
        return newest
    return None


def _reembed_cross_model(
    db: object, name: str, target_model: str, *, dry_run: bool, yes: bool, keep_source: bool,
    force_delete: bool, token_cap: int | None, wait: bool = False,
) -> None:
    """``NAME --to MODEL`` where MODEL differs from the collection's: the job, if MODEL is the profile's."""
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
    from nexus.reembed_job import is_superseded, profile_models  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    _require_local_engine(db)
    reader = make_catalog_reader()
    try:
        row = reader.get_collection(name)
        profile = profile_models(reader)
    except Exception as exc:  # noqa: BLE001 — boundary: an unreadable catalog or engine is the whole answer
        raise _reembed_job_error(exc) from exc
    if row is None:
        raise click.ClickException(f"collection {name!r} has no catalog row; there is nothing to move.")
    if row.get("lifecycle_state") == "live" and is_superseded(row) and _kept_tombstoned_job(db, name, row) is not None:
        raise click.ClickException(
            f"collection {name!r} is the superseded source of a re-embed job that kept it because tombstoned "
            f"documents still held it. Once they are purged or restored, `nx collection re-embed --all` re-enters "
            f"that job; NAME --to MODEL starts new jobs only."
        )
    if row.get("lifecycle_state") != "live" or is_superseded(row):
        raise click.ClickException(
            f"collection {name!r} is not a live, current collection ({row.get('lifecycle_state')}"
            f"{', superseded by ' + str(row['superseded_by']) if is_superseded(row) else ''}); it is not re-embedded."
        )
    want = profile.get(str(row.get("content_type")))
    if want is None:
        raise click.ClickException(
            f"the engine's profile has no model for content type {row.get('content_type')}; nothing to move to."
        )
    if target_model != want:
        raise click.ClickException(
            f"--to {target_model!r} is not the profile's model for {row.get('content_type')} collections "
            f"({want!r}). A cross-model re-embed moves a collection to the profile's model; to change the "
            f"profile, nx config set local.embed_model (or voyage_api_key) and restart the service."
        )
    _reembed_jobs(
        db, [row], profile=profile, held=[], reentries=[], dry_run=dry_run, yes=yes,
        keep_source=keep_source, force_delete=force_delete, token_cap=token_cap, wait=wait,
    )


@collection.command("re-embed")
@click.argument("name", required=False)
@click.option(
    "--to", "target_model",
    type=click.Choice((*_REEMBED_SUPPORTED_MODELS, _REEMBED_BGE_MODEL)),
    help="Target embedding model. The collection's own model re-embeds it in place (service mode: the "
         "model its catalog row records). A different model starts a re-embed job, and must be the "
         "profile's model for the collection's content type (local engine only).",
)
@click.option("--all", "all_collections", is_flag=True, default=False,
              help="Plan every live collection off the engine's profile; with --no-dry-run --yes, start one "
                   "job per collection (local engine only).")
@click.option("--skip", "skip", multiple=True, metavar="NAME",
              help="With --all: leave NAME out of the run (repeatable). It is listed as skipped; a NAME that is "
                   "not one of --all's candidates (stale, held or a re-entry) is an error. Without it, one refused "
                   "plan starts nothing.")
@click.option("--keep-source", is_flag=True, default=False,
              help="Job only: keep the emptied source collection (superseded, with its quarantine rows and "
                   "topic data) instead of deleting it.")
@click.option("--force-delete", is_flag=True, default=False,
              help="Job only: delete the source even when aspect, queue or highlight rows that match no moved "
                   "document still name it (otherwise the job ends kept_residue and keeps the source). "
                   "Does not lift a parity failure.")
@click.option("--token-cap", type=click.IntRange(min=1), default=None,
              help="Job only: tokens a Voyage target may bill (default 10000000; none for the on-device "
                   "model). With --resume, raises the cap of a job that stopped on it.")
@click.option("--status", "status_job", is_flag=False, flag_value=_ALL_JOBS,
              metavar="[JOB]", help="Show re-embed jobs: all, or the one JOB id.")
@click.option("--pause", "pause_job", default=None, metavar="JOB", help="Pause a running job.")
@click.option("--resume", "resume_job", default=None, metavar="JOB",
              help="Resume a paused or failed job (re-checks the profile).")
@click.option("--abort", "abort_job", default=None, metavar="JOB",
              help="Abort a job; documents already moved stay moved.")
@click.option("--wait", "wait", is_flag=True, default=False,
              help="Job only: with a start (--all, or NAME --to MODEL with --no-dry-run) or --resume JOB, wait "
                   "until every job is done, done_with_skips, aborted, failed or paused, naming the state; on "
                   "the first two, run taxonomy discover for the target. Exits 1 on the other three. Without "
                   "it, the done report names the discover command.")
@click.option("--dry-run/--no-dry-run", default=True,
              help="Default dry-run. Pass --no-dry-run to actually write.")
@click.option("--yes", is_flag=True, default=False,
              help="Skip the destructive-action confirmation prompt.")
def reembed_cmd(
    name: str | None, target_model: str | None, all_collections: bool, skip: tuple[str, ...], keep_source: bool,
    force_delete: bool, token_cap: int | None, status_job: str | None, pause_job: str | None,
    resume_job: str | None, abort_job: str | None, wait: bool, dry_run: bool, yes: bool,
) -> None:
    """Re-embed a collection: in place with its own model, or into the profile's model with a job.

    \b
      nx collection re-embed NAME --to MODEL    in place when MODEL is NAME's model; else a job
      nx collection re-embed --all              plan every collection off the profile
      nx collection re-embed --all --skip NAME  the same, leaving NAME out (repeatable)
      nx collection re-embed --status [JOB]     jobs; --pause/--resume/--abort JOB act on one

    nexus-bw65: rebuild embeddings for every chunk in NAME that has a
    live catalog document, using --to MODEL. Chunk ids, document text,
    and metadata are preserved; only the vector changes. A chunk with no
    live document (no owner, or owned only by a deleted document) is left
    on its old vector, and the summary says how many.

    Use case: an embedding-model upgrade on a sourceless collection
    (store_put-only / MCP-promoted notes). For source-backed
    collections prefer ``nx collection reindex`` so the indexer
    re-derives chunk boundaries with the new chunker contract.

    Also the repair for stored vectors that no longer match their text:
    re-embedding in place with the collection's own model recomputes every
    vector server-side (nexus-tysei: voyage-context-3 chunks the retired
    client path embedded with document context).

    RDR-210 Phase 6: a different MODEL, or --all, runs a job on the local
    engine. It moves each live document, one transaction apiece, into the
    sibling collection that carries the profile's model, then supersedes and
    deletes the source (--keep-source keeps it). The plan states the token
    estimate and the cap (10000000 for a Voyage target, none for the on-device
    model) and what the source's delete would remove, and a plan above the cap
    is refused before anything is written. Dry run is the default.

    The source is deleted only when it holds no document row and the engine's
    checks pass. It is kept, superseded, when a tombstoned document still
    holds it (kept_tombstoned: --all re-enters the job once the trash is
    purged or a document restored), with --keep-source, or when rows naming
    it match no document the job moved (kept_residue; --force-delete deletes
    them with it). A source the parity check refuses (failed, parity_mismatch)
    is deleted by nothing: --resume re-runs the check, --abort leaves it
    superseded and undeleted. --wait follows the jobs to done,
    done_with_skips, aborted, failed or paused and runs taxonomy discover for
    the target of the first two.

    Limitations:
      - The in-place form does NOT rename the collection; run
        ``nx collection rename`` if the name needs to track the new model.
    """
    if skip and not all_collections:
        raise click.UsageError("--skip goes with --all.")
    controls = [c for c in (status_job, pause_job, resume_job, abort_job) if c is not None]
    if controls:
        if len(controls) > 1 or name or all_collections or target_model or keep_source or force_delete:
            raise click.UsageError(
                "--status, --pause, --resume and --abort each stand alone (--token-cap goes with --resume)."
            )
        if token_cap is not None and resume_job is None:
            raise click.UsageError("--token-cap with a job control goes with --resume.")
        if wait and resume_job is None:
            raise click.UsageError("--wait goes with a start (--all, NAME --to MODEL) or --resume JOB.")
        _reembed_controls(
            _t3(), status_job=status_job, pause_job=pause_job, resume_job=resume_job,
            abort_job=abort_job, token_cap=token_cap, wait=wait,
        )
        return
    job_flags = keep_source or force_delete or token_cap is not None
    if all_collections:
        if name or target_model:
            raise click.UsageError(
                "--all moves every collection off the profile to the profile's model; it takes no NAME or --to."
            )
        _reembed_all(
            _t3(), dry_run=dry_run, yes=yes, keep_source=keep_source, force_delete=force_delete,
            token_cap=token_cap, skip=skip, wait=wait,
        )
        return
    if not name or not target_model:
        raise click.UsageError("give NAME and --to MODEL, or --all, or a job control (--status ...).")

    db = _t3()

    # nexus-u37lw (critique Critical): in service mode, upsert_chunks_with_
    # embeddings forwards TEXT and the server re-embeds by the model the
    # collection's catalog row records (EmbedderRouter routes by the row, the
    # name's model only when no row backs it; nexus-dxifx) —
    # client-computed vectors are discarded. A --to that differs from the
    # encoded model would therefore silently no-op with the OLD model while
    # stamping the NEW model into metadata (corrupting staleness checks)
    # and printing false success. Fail loud instead. RDR-210 Phase 6: on the
    # local engine a different model starts the re-embed job, which moves the
    # documents into the sibling collection that carries the model.
    from nexus.db.http_vector_client import is_service_backed  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    if is_service_backed(db):
        encoded = _reembed_source_model(db, name)
        if encoded != target_model:
            from nexus.config import is_local_mode  # noqa: PLC0415 — deferred import; patched in tests at the module attribute

            if is_local_mode():
                _reembed_cross_model(
                    db, name, target_model, dry_run=dry_run, yes=yes, keep_source=keep_source,
                    force_delete=force_delete, token_cap=token_cap, wait=wait,
                )
                return
            raise click.ClickException(
                f"service mode re-embeds server-side using the model the "
                f"collection's catalog row records — or, when no row backs the "
                f"name, the model its name encodes ({encoded!r}); "
                f"--to {target_model!r} "
                f"cannot take effect on {name!r}. Cross-model moves "
                f"re-index from source into a {target_model}-named "
                f"collection (nx index repo / nx index pdf — the RDR-162 "
                f"vector-ETL pipeline retired at RDR-155 P4b). Plain "
                f"'nx collection rename' does NOT re-embed and would "
                f"corrupt the name-encoded model contract."
            )
    if job_flags:
        raise click.UsageError(
            "--keep-source, --force-delete and --token-cap belong to the re-embed job (a different --to MODEL, "
            "or --all); an in-place re-embed has none of them."
        )
    if wait:
        raise click.UsageError(
            "--wait belongs to the re-embed job (a different --to MODEL, or --all); an in-place re-embed has no job."
        )

    if not dry_run and not yes:
        click.confirm(
            f"Re-embed {name!r} with {target_model!r}? This rewrites the vector "
            f"of every chunk that has a live catalog document, in place; chunks "
            f"with none keep their old vector.",
            abort=True,
        )

    if dry_run:
        try:
            col = db.get_collection(name)
            n = col.count()
        except Exception as exc:  # noqa: BLE001 — boundary catch re-raised as ClickException (tail review, nexus-c9xr2)
            raise click.ClickException(
                f"collection {name!r}: {type(exc).__name__}: {exc}"
            )
        click.echo(
            f"dry-run: would re-embed up to {n} stored chunk(s) in {name!r} with "
            f"{target_model!r} (chunks with no live catalog document are left as "
            f"they are). Pass --no-dry-run --yes to apply."
        )
        return

    try:
        processed, skipped = _reembed_collection(
            db, name, target_model, dry_run=False,
        )
    except Exception as exc:  # noqa: BLE001 — boundary catch re-raised as ClickException (file convention; reviewer M1, nexus-znwc2 — the upsert ack-mismatch RuntimeError surfaces here)
        raise click.ClickException(
            f"collection {name!r}: {type(exc).__name__}: {exc}"
        )
    click.echo(
        f"re-embedded {processed} chunk(s) in {name!r} with "
        f"{target_model!r}; skipped {skipped} row(s) (empty document, or no "
        f"longer owned by a live document when the write reached the engine)."
    )
    # nexus-wbfpw.35 fix round 3: the walk reads live rows, so a stored chunk with
    # no live catalog document is never visited. Say how many stayed on the old
    # model rather than let "re-embedded N" read as the whole collection.
    try:
        stored = db.get_collection(name).count()
    except Exception:  # noqa: BLE001 — the count only decorates a finished run
        stored = None
    if stored is not None and stored > processed:
        click.echo(
            f"{stored - processed} of {stored} stored chunk(s) were not "
            f"re-embedded and keep their old vectors: the skipped rows above, "
            f"and chunks with no live catalog document (`nx collection info "
            f"{name}` shows live and stored counts)."
        )


@collection.command("rewrite-metadata")
@click.argument("name", required=False, default=None)
@click.option("--all", "all_collections", is_flag=True,
              help="Rewrite metadata in every T3 collection.")
@click.option("--source-path", default=None,
              help="Only rewrite chunks whose source_path equals this value.")
@click.option("--dry-run", is_flag=True,
              help="Report counts without issuing any writes.")
def rewrite_metadata_cmd(
    name: str | None,
    all_collections: bool,
    source_path: str | None,
    dry_run: bool,
) -> None:
    """Rewrite each chunk's metadata to the canonical schema (nexus-2my).

    Operationalises the nexus-40t metadata schema rationalisation on
    already-indexed corpora. Chunks ingested before 4.3.1 keep their
    pre-canonical metadata (cargo keys, flat git_*, oversized records)
    until this command is run; ``nx index --force`` is a silent no-op
    when the pipeline-state DB still has the content_hash on file.

    \\b
    Examples:
      nx collection rewrite-metadata knowledge__delos
      nx collection rewrite-metadata knowledge__delos --source-path paper.pdf
      nx collection rewrite-metadata --all --dry-run
    """
    from nexus.db.t3 import _rewrite_collection_metadata  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    if not name and not all_collections:
        raise click.ClickException("specify a collection name or use --all")
    if name and all_collections:
        raise click.ClickException("--all is mutually exclusive with NAME")

    db = _t3()
    targets = (
        sorted(c["name"] for c in db.list_collections())
        if all_collections else [name]
    )

    grand_updated = 0
    grand_skipped = 0
    grand_total = 0
    for i, col_name in enumerate(targets, 1):
        try:
            updated, skipped, total = _rewrite_collection_metadata(
                db, col_name,
                source_path=source_path,
                dry_run=dry_run,
            )
        except Exception as exc:  # noqa: BLE001 — per-collection failure surfaced via click.echo, loop continues
            click.echo(
                f"  [{i}/{len(targets)}] {col_name}: "
                f"{type(exc).__name__}: {exc}",
                err=True,
            )
            continue

        grand_updated += updated
        grand_skipped += skipped
        grand_total += total
        verb = "would rewrite" if dry_run else "rewrote"
        click.echo(
            f"  [{i}/{len(targets)}] {col_name}: {verb} {updated}, "
            f"skipped {skipped} (already canonical), {total} scanned"
        )

    verb = "Would rewrite" if dry_run else "Rewrote"
    click.echo(
        f"Done: {verb} {grand_updated} chunks "
        f"({grand_skipped} already canonical, {grand_total} scanned) "
        f"across {len(targets)} collection(s)."
    )


@collection.command("health")
@click.option(
    "--sort",
    "sort_by",
    type=click.Choice([
        "name", "chunk_count", "last_indexed",
        "zero_hit_rate_30d", "median_query_distance_30d",
        "cross_projection_rank", "orphan_catalog_rows",
        "hub_domination_score",
    ]),
    default="name",
    show_default=True,
    help="Sort the health table by the named column.",
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["table", "json"]),
    default="table",
    show_default=True,
    help="Output format.",
)
def health_cmd(sort_by: str, fmt: str) -> None:
    """Composite per-collection health report (RDR-087 Phase 3.4).

    Folds catalog, T2 telemetry, and topic-assignment signals into one
    row per collection. Use ``--format=json`` for agents and dashboards.
    """
    from nexus.collection_health import run_collection_health  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    click.echo(run_collection_health(sort_by=sort_by, fmt=fmt))


@collection.command("audit")
@click.argument("name")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["table", "json"]),
    default="table",
    show_default=True,
    help="Output format.",
)
@click.option(
    "--live",
    is_flag=True,
    default=False,
    help=(
        "When the 30-day search_telemetry histogram is empty, sample N "
        "chunks from the T3 service store and derive the distance histogram from "
        "self-queries (budget ~10 s at N=25). Reuses stored embeddings "
        "— no Voyage API roundtrips."
    ),
)
@click.option(
    "--live-n",
    "live_n",
    type=int,
    default=25,
    show_default=True,
    help="Number of live-probe samples when --live fires.",
)
def audit_cmd(name: str, fmt: str, live: bool, live_n: int) -> None:
    """Deep-dive audit for a single collection (RDR-087 Phase 4.2).

    Four sections: distance histogram (30d telemetry, ``--live`` to
    probe T3 when telemetry is cold), top-5 cross-projections,
    orphan chunks (>30d, no incoming links), top-10 cross-collection
    hub topic assignments.
    """
    from nexus.collection_audit import (  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
        format_audit_human,
        format_audit_json,
        run_collection_audit,
    )

    report = run_collection_audit(name, live=live, live_n=live_n)
    if fmt == "json":
        click.echo(format_audit_json(report))
    else:
        click.echo(format_audit_human(report))


@collection.command("merge-candidates")
@click.option(
    "--min-shared", "min_shared", type=int, default=3, show_default=True,
    help="Minimum distinct shared topics between two collections "
         "to qualify as a candidate.",
)
@click.option(
    "--min-similarity", "min_similarity", type=float, default=0.5,
    show_default=True,
    help="Minimum mean ``similarity`` across shared topics.",
)
@click.option(
    "--exclude-hubs", "exclude_hubs", is_flag=True, default=False,
    help="Drop top-N cross-collection hub topics from the shared-topic "
         "count before thresholding (reduces false positives from "
         "generic hubs).",
)
@click.option(
    "--hub-top-n", "hub_top_n", type=int, default=10, show_default=True,
    help="Hub depth used by --exclude-hubs.",
)
@click.option(
    "--limit", "limit", type=int, default=50, show_default=True,
    help="Max number of candidate pairs returned.",
)
@click.option(
    "--format", "fmt",
    type=click.Choice(["table", "json"]), default="table", show_default=True,
    help="Output format.",
)
@click.option(
    "--create-link", "create_link", is_flag=True, default=False,
    help="(deferred) Write catalog `relates`/`bridges` edges for each "
         "surfaced pair. Currently reports a deferred-workflow advisory "
         "per RDR §bridge-link — use nx catalog link manually.",
)
def merge_candidates_cmd(
    min_shared: int, min_similarity: float,
    exclude_hubs: bool, hub_top_n: int,
    limit: int, fmt: str, create_link: bool,
) -> None:
    """(Unavailable) Pair-wise cross-collection overlap ranking (RDR-087 Phase 4.3).

    Surfaces (a, b) pairs where collection *a* projects into topics in
    collection *b* with high similarity — hints at merge or bridge-
    link opportunities for a human / agent to decide on. NEVER writes
    catalog edges automatically; --create-link is advisory and deferred.
    """
    if create_link:
        raise click.ClickException(
            "--create-link is deferred per RDR-087 §bridge-link workflow. "
            "Use `nx catalog link` manually after reviewing the candidates."
        )
    from nexus.merge_candidates import run_merge_candidates  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    # nexus-sis0m.4: the analysis is unavailable; exiting 0 with prose
    # (even under --format json) read as an empty result.
    raise click.ClickException(
        run_merge_candidates(
            min_shared=min_shared,
            min_similarity=min_similarity,
            exclude_hubs=exclude_hubs,
            hub_top_n=hub_top_n,
            limit=limit,
            fmt=fmt,
        )
    )
