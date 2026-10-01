# SPDX-License-Identifier: AGPL-3.0-or-later
import shlex
import sys
from pathlib import Path

import click
import structlog

_log = structlog.get_logger(__name__)

from nexus.corpus import EmbeddingProfileMismatchError, LocalVoyageCredentialMissingError, t3_collection_name
from nexus.db import make_t3
from nexus.db.t3 import T3Database
from nexus.errors import PutOversizedError
from nexus.ttl import parse_ttl


def _t3() -> T3Database:
    # No credential pre-flight (nexus-c7aj3): make_t3() constructs the
    # service-backed client unconditionally (RDR-155 P4a.2) — no call site
    # here can reach a direct-Chroma client, so a Chroma/Voyage cred check
    # at this boundary only ever produced false failures on migrated
    # installs. Legacy creds are migration-source config; the ETL that
    # reads them does its own checks. Real construction failures surface
    # as make_t3()'s own honest errors.
    try:
        return make_t3()
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc


@click.group()
def store() -> None:
    """Permanent semantic knowledge store (served by the native nexus-service: bge-768 locally, Voyage AI in cloud mode)."""


@store.command("put")
@click.argument("source")
@click.option("--collection", "-c", required=True,
              help="Collection: the bare subject this note belongs to, such as "
                   "distributed-systems. Required: the placeholders default/knowledge/"
                   "notes/tmp/test are refused. Never a model token or version; see "
                   "docs/collections.md.")
@click.option("--title", "-t", default="", help="Document title (required when SOURCE is -)")
@click.option("--tags", default="", help="Comma-separated tags")
@click.option("--category", default="", help="Category label")
@click.option("--ttl", default="permanent", show_default=True,
              help="TTL: Nd, Nw, or permanent")
@click.option("--session-id", default="", hidden=True)
@click.option("--agent", default="", hidden=True, help="Source agent name")
def put_cmd(
    source: str,
    collection: str,
    title: str,
    tags: str,
    category: str,
    ttl: str,
    session_id: str,
    agent: str,
) -> None:
    """Store SOURCE (file path or '-' for stdin) in the T3 knowledge store.

    SOURCE may be a file path or '-' to read from stdin.  When reading from
    stdin, --title is required.

    \b
    Examples:
      nx store put ./notes.md --collection distributed-systems --tags "arch,decision"
      echo "key insight" | nx store put - --title "finding-01" --collection vector-search
      nx store put ./doc.md --ttl 30d --title "sprint-notes"
    """
    if source == "-":
        if not title:
            raise click.ClickException("--title is required when reading from stdin (-)")
        content = sys.stdin.read()
    else:
        path = Path(source)
        if not path.exists():
            raise click.ClickException(f"File not found: {source}")
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise click.ClickException(f"File {source!r} is not valid UTF-8.")
        if not title:
            title = path.name

    # MCP store_put refuses empty content up front ("content is required"); so does the CLI, with
    # the same clean error rather than a ValueError traceback from deep in the writer.
    if not content:
        raise click.ClickException(
            f"nothing to store: {'stdin' if source == '-' else repr(source)} is empty."
        )

    try:
        days = parse_ttl(ttl)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    # nexus-tk070.p6b fix-pass (nexus-24rof, RDR-194 D5): pass None through
    # verbatim rather than coercing to 0 — parse_ttl already rejects "0d"/
    # "0w" input (ValueError, caught above), so `days` is either None
    # (permanent) or a positive int here; db.put now rejects an explicit 0
    # itself as defense-in-depth, but this CLI path can never reach it.
    ttl_days = days

    db = _t3()
    # nexus-hmxi: pass t3 so the resolver grandfathers an existing
    # legacy 2-segment collection ahead of the auto-promoted
    # conformant shape, keeping store/list/search aligned.
    # for_write=True (nexus-35ok4): this command WRITES new content —
    # a genuinely new corpus mints strictly (raises loud if
    # local.embed_model is voyage-shaped with no key configured).
    # nexus-0fw11: a placeholder subject raises PlaceholderCollectionError,
    # itself a ClickException, so the refusal prints cleanly here and on
    # every other CLI writer without a per-command catch.
    col_name = t3_collection_name(collection, t3=db, for_write=True)

    # RDR-223 P2.6 (nexus-z0o2p.16): the note is written by the note writer, the
    # same sequence MCP store_put runs (nexus-z0o2p.12). put_note owns the whole
    # caller protocol: split the note to the collection model's token window
    # (nexus-spujb), refuse an over-quota note before minting anything (nexus-xzyr3),
    # register the catalog document, begin the index-run fence, send the pieces
    # and the owner rows as ONE write_manifest_many request (the completion stamp
    # follows the post-store chains, stamp_note), and settle the outcome (fail the
    # fence, remove the row this call minted or put back the identity stamp it
    # changed). A chunk of the note
    # can therefore never land without its owner, and a failed request leaves the
    # previous manifest as it was. This command only words the result.
    from nexus.catalog.note_write import failure_message, fire_note_chains, put_note, stamp_note  # noqa: PLC0415 — deferred: heavy catalog import, rare/branch-local for CLI startup cost

    # nexus-s71lr, deliverable 3 (named literally: "nx store put"): a single
    # document is still ONE embed call, and a large document's embed can run
    # a minute+ with zero progress signal at all -- worse than the per-file
    # loops (not even a start/end line). Same _PhaseHeartbeat mechanism as
    # `nx index rdr`/`nx index pdf --dir`/`nx store import`: ticks every 5s
    # for as long as the write is in flight. arm() sits immediately before the
    # try/finally that guards it (code-review-expert finding d), nothing
    # risky in between.
    from nexus.commands.index import _PhaseHeartbeat  # noqa: PLC0415 — deferred cross-module import; avoids a hard import-time coupling between two independently-loadable command modules
    file_heartbeat = _PhaseHeartbeat(
        is_tty=sys.stdout.isatty(),
        echo=lambda msg, nl: click.echo(msg, nl=nl, err=True),
        interval=5.0,
        prefix="embed",
    )
    file_heartbeat.arm(f"storing {title or source}")
    try:
        outcome = put_note(
            content=content, collection=col_name, title=title, tags=tags,
            category=category, session_id=session_id, source_agent=agent,
            ttl_days=ttl_days,
        )
    except PutOversizedError as exc:
        # put_note refuses an over-quota note before it mints a catalog row.
        raise click.ClickException(str(exc)) from exc
    finally:
        file_heartbeat.disarm()

    # One wording of every outcome that did not store (note_write.failure_message): the same table
    # MCP store_put, nx memory promote and the recovery import read, so a client-side refusal is told
    # to fix its key here exactly as it is there, and an outcome no command knows is never "Stored".
    message = failure_message(outcome, subject=repr(title), check="'nx store list'")
    if message is not None:
        raise click.ClickException(message)
    # nexus-9099: fire the three post-store hook chains so the chash index, taxonomy assignment and
    # aspect-extraction queue see CLI store-put events (RDR-095 symmetric-fire). fire_note_chains is
    # MCP store_put's shape (nexus-spujb): the single and batch chains see every piece, the document
    # chain sees the note once, whole, and carries the CATALOG tumbler (nexus-w8lg1). The manifest was
    # written by the one request above (the completion stamp follows the chains), so the batch chain
    # skips the manifest hook. doc_id is the source identity here: catalog identity for a note is
    # (collection, title) uniformly (nexus-sdp0u), whether SOURCE was a file or stdin: the file's
    # on-disk path is deliberately never passed through as catalog file_path, since that leg is
    # collection-blind and could match/clobber an unrelated `nx index md` document.
    fire_note_chains(outcome, content)
    # The completion stamp, LAST (RDR-223, nexus-z0o2p.34): a kill in a chain above leaves the fence
    # 'indexing', so the next put of the note redoes the write and fires the chains again.
    outcome = stamp_note(outcome)
    message = failure_message(outcome, subject=repr(title), check="'nx store list'")
    if message is not None:
        raise click.ClickException(message)
    pieces = outcome.pieces
    split_note = f"  ({len(pieces)} chunks, split to the embedding model's token window)" if len(pieces) > 1 else ""
    click.echo(f"Stored: {outcome.doc_id}  →  {col_name}{split_note}")


# nexus-8g79.10 (V1): catalog_store_hook moved to
# ``nexus.catalog.store_hook`` (lower layer) so MCP infra can invoke
# without the MCP layer reaching up into this CLI module. Re-exported
# here under the legacy private name for back-compat.
from nexus.catalog.store_hook import catalog_store_hook as _catalog_store_hook  # noqa: E402
# nexus-spujb: ``get_cmd`` reads a split note back whole.
from nexus.catalog.store_hook import split_note_text as _split_note_text  # noqa: E402
# RDR-223 P2.6 (nexus-z0o2p.16): ``put_cmd`` writes through
# ``nexus.catalog.note_write.put_note``, so the split-write helpers it used to
# import here (tracked registration, rollbacks, the direct manifest write, the
# note splitters, the oversize check) are no longer re-exported from this module.


@store.command("list")
@click.option("--collection", "-c", default="knowledge", show_default=True,
              help="Collection: a bare subject such as distributed-systems (default: "
                   "knowledge). Never a model token or version; see "
                   "docs/collections.md.")
@click.option("--limit", "-n", default=200, show_default=True,
              help="Maximum entries to show")
@click.option("--offset", default=0, show_default=True,
              help="Skip this many entries (for pagination)")
@click.option("--docs", is_flag=True, default=False,
              help="Show unique documents instead of individual chunks")
@click.option("--reapable", is_flag=True, default=False,
              help="List the chunks the engine's reapable predicate selects in --collection "
                   "(read-only; the set 'nx t3 gc' would quarantine). Requires --collection. "
                   "--limit bounds the rows shown.")
def list_cmd(collection: str, limit: int, offset: int, docs: bool, reapable: bool) -> None:
    """List entries in a T3 knowledge collection."""
    if reapable:
        # The operator must NAME the collection: the default ('knowledge') is a convenience for the
        # plain listing, but an audit of what garbage collection would take should never silently
        # target a collection the operator did not choose.
        from click.core import ParameterSource  # noqa: PLC0415 — deferred, branch-local

        ctx = click.get_current_context()
        if ctx.get_parameter_source("collection") in (ParameterSource.DEFAULT, ParameterSource.DEFAULT_MAP, None):
            raise click.UsageError("--reapable requires --collection (name the collection to inspect).")
        if docs or offset:
            raise click.UsageError("--reapable cannot be combined with --docs or --offset.")
    db = _t3()
    col_name = t3_collection_name(collection, t3=db)

    if reapable:
        _list_reapable(db, col_name, limit)
        return

    if docs:
        _list_documents(db, col_name)
        return

    entries = db.list_store(col_name, limit=limit, offset=offset)
    if not entries:
        click.echo(f"No entries in {col_name} at offset {offset}.")
        return

    # Get total count for page info
    try:
        total = db.collection_info(col_name)["count"]
    except Exception:  # noqa: BLE001 — best-effort total count for display; '?' on any backend failure (incl. KeyError)
        total = "?"

    shown_start = offset + 1
    shown_end = offset + len(entries)
    # nexus-sis0m.3: `total` is the collection's STORED chunk count (the
    # cheap count); the rows listed are live ones, so after a delete the
    # two differ (shakeout 7.64.1 F10). Name it rather than imply live.
    click.echo(f"{col_name}  (showing {shown_start}-{shown_end}; {total} stored)\n")
    from datetime import datetime, timedelta  # noqa: PLC0415  — stdlib deferred to call site (datetime)
    for e in entries:
        doc_id = e.get("id", "")  # RDR-180: full id — the list->get handle must round-trip
        title = (e.get("title") or "")[:40]
        tags = e.get("tags") or ""
        ttl_days = e.get("ttl_days", 0)
        indexed_at_full = e.get("indexed_at") or ""
        indexed_at = indexed_at_full[:10]
        # Derive expiry from indexed_at + ttl_days (expires_at no longer
        # stored — see metadata_schema.is_expired).
        if ttl_days and ttl_days > 0 and indexed_at_full:
            try:
                exp = (datetime.fromisoformat(indexed_at_full)
                       + timedelta(days=ttl_days)).date().isoformat()
                ttl_str = f"expires {exp}"
            except ValueError:
                ttl_str = f"ttl {ttl_days}d"
        else:
            ttl_str = "permanent"
        tag_str = f"  [{tags}]" if tags else ""
        click.echo(f"  {doc_id}  {title:<40}  {ttl_str:<24}  {indexed_at}{tag_str}")

    # A full page is the live signal that more rows may follow; the stored
    # total can exceed the live rows and point at an empty page (review of
    # 56bb2e88e).
    if len(entries) >= limit:
        click.echo(f"\n  Next page: --offset {shown_end}")


def _age_days(stamp: str) -> str:
    """*stamp* (an ISO-8601 timestamp) as whole days ('40d'), or '-' when it will not parse."""
    from datetime import UTC, datetime  # noqa: PLC0415  — stdlib deferred to call site (datetime)

    try:
        born = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return "-"
    if born.tzinfo is None:
        born = born.replace(tzinfo=UTC)
    return f"{max((datetime.now(UTC) - born).days, 0)}d"


def _reapable_known_collection(db: T3Database, col_name: str) -> bool:
    """True when *col_name* is a collection T3 holds chunks for or the catalog has registered, the
    same test ``nx t3 gc`` applies. The engine answers a reapable listing for ANY name with an empty
    200, so without this a typo reads "0 reapable chunks", the same words a clean collection gets."""
    if col_name in {c["name"] for c in db.list_collections(strict=True)}:
        return True
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred for startup cost (nexus.catalog.factory)

    cat = make_catalog_reader()
    return cat is not None and cat.get_collection(col_name) is not None


def _list_reapable(db: T3Database, col_name: str, limit: int) -> None:
    """``nx store list --reapable``: the chunks of *col_name* that the engine's reapable predicate
    selects right now, from ``POST /v1/vectors/reapable`` (RDR-192 Step 8/10). Read-only and
    advisory: a lock-free snapshot with the engine's default grace, i.e. what ``nx t3 gc`` would
    take at this instant. Paged by keyset; ``limit`` bounds the rows printed.

    The age shown is days since ``last_written_at``, the one clock column the route returns. The
    engine's grace runs from the later of that and the moment the chunk last lost an owner
    (``nexus.chunk_orphaned_at``, not returned), so the age here is at least the grace for every
    listed chunk and can overstate how long the chunk has been ownerless. ``created_at`` is shown
    beside it: it is write-once, so it is never the grace clock and is never younger than the age."""
    from nexus.db.http_vector_client import VectorServiceError  # noqa: PLC0415 — deferred for startup cost (nexus.db.http_vector_client)

    reapable_chunks = getattr(db, "reapable_chunks", None)
    if reapable_chunks is None:
        raise click.ClickException(
            "--reapable needs the engine-backed T3 handle (nexus.db.make_t3()); this one carries "
            "no reapable route."
        )
    if not _reapable_known_collection(db, col_name):
        raise click.ClickException(
            f"no collection named {col_name!r}; 'nx collection list' shows the names "
            f"'nx store list --reapable' takes."
        )
    shown = 0
    truncated = False
    try:
        for row in reapable_chunks(col_name):
            if shown >= limit:
                truncated = True
                break
            if shown == 0:
                click.echo(
                    f"{col_name}  (chash, last_written_at, days since last write, created_at, "
                    f"title, catalog_doc_id)\n"
                )
            written = row.get("last_written_at") or ""
            created = row.get("created_at") or ""
            title = (row.get("title") or "")[:40]
            doc = row.get("catalog_doc_id") or "-"
            click.echo(
                f"  {row.get('chash', '')}  {written}  {_age_days(written):>5}  {created}  "
                f"{title:<40}  {doc}"
            )
            shown += 1
    except VectorServiceError as exc:
        if exc.code == 404:
            raise click.ClickException(
                "The connected engine predates POST /v1/vectors/reapable (RDR-192 Step 8, bead "
                "nexus-wbfpw.17); upgrade to an engine tag that carries it (compare the deployed "
                "engine's version against REQUIRED_ENGINE_VERSION in src/nexus/engine_version.py)."
            ) from exc
        raise click.ClickException(f"Failed to list reapable chunks for {col_name!r}: {exc}") from exc
    if shown == 0:
        click.echo(f"0 reapable chunks in {col_name}")
        return
    if truncated:
        click.echo(f"\n  More reapable chunks exist than --limit {limit}; raise --limit to see them.")


def _list_documents(db: T3Database, col_name: str) -> None:
    """List documents in a collection, grouped by the catalog manifest.

    Grouping is :func:`nexus.catalog.store_hook.group_documents`, shared
    with the MCP ``store_list(docs=True)`` view, which carried an independent
    copy of the same mistake this replaces: both grouped by each chunk row's
    own ``content_hash``, on the premise that a ``store_put`` note is one
    chunk. Note splitting (nexus-spujb, nexus-b2tld) falsified it, and a
    split note then listed as one row per piece under a repeated title.

    Chunk count is likewise derived from the grouping. It used to read a
    ``chunk_count`` metadata field that only the PDF indexer ever sets, so
    every ``store_put`` note printed ``?`` — the MCP side fixed that half
    alone, and the two displays disagreed.
    """
    try:
        total_chunks = db.collection_info(col_name)["count"]
    except KeyError:
        # nexus-sis0m.1: only an absent or empty collection lands here
        # (collection_info cannot tell the two apart), and it is reported the
        # way plain `store list` reports it, at exit 0. Every other failure,
        # a stopped service among them, used to print "Collection not found"
        # too, which reads as data loss; those now propagate.
        click.echo(f"No documents in {col_name}.")
        return

    from nexus.catalog.store_hook import group_documents  # noqa: PLC0415 — deferred for startup cost (heavy nexus submodule)

    docs, degraded = group_documents(db, col_name, total_chunks)
    if not docs:
        click.echo(f"No documents in {col_name}.")
        return

    click.echo(f"{col_name}  ({len(docs)} documents, {total_chunks} stored chunks)\n")
    if degraded:
        click.echo(
            f"  NOTE: grouped by chunk, not by manifest — {degraded}. "
            "A split note appears as one row per piece.\n"
        )
    # page_count is not in ALLOWED_TOP_LEVEL — normalize() drops it so the
    # read always returned empty; removed in nexus-59j0. nexus-1oguj later
    # promoted extraction_method to canonical, but this compact list table
    # wasn't extended to show it (`nx store get` displays the per-chunk
    # value — see its display path).
    for i, doc in enumerate(docs, 1):
        title = (doc.title or "untitled")[:60]
        indexed = (doc.entry.get("indexed_at") or "")[:10]
        click.echo(f"  {i:3d}. {title:<60}  {doc.chunks:>4} chunks  {indexed}")



@store.command("get")
@click.argument("doc_id")
@click.option("--collection", "-c", default="knowledge", show_default=True,
              help="Collection: a bare subject such as distributed-systems (default: "
                   "knowledge). Never a model token or version; see "
                   "docs/collections.md.")
@click.option("--json", "json_out", is_flag=True, default=False,
              help="Output as JSON")
def get_cmd(doc_id: str, collection: str, json_out: bool) -> None:
    """Retrieve a T3 knowledge entry by its document ID.

    DOC_ID is the 64-char content-hash ID shown by 'nx store list'
    (the full sha256(chunk_text) hexdigest — RDR-180; pre-RDR-180
    stores used the [:32] prefix form).

    \b
    Examples:
      nx store get a1b2c3d4e5f6789012345678901234abcdef0123456789abcdef0123456789ab
      nx store get a1b2c3d4e5f6789012345678901234abcdef0123456789abcdef0123456789ab --collection code__myrepo --json
    """
    db = _t3()
    col_name = t3_collection_name(collection, t3=db)
    entry = db.get_by_id(col_name, doc_id)
    if entry is None:
        raise click.ClickException(f"Entry {doc_id!r} not found in {col_name}")
    # nexus-spujb: a note split to its model's token window reads back whole.
    split = _split_note_text(db, col_name, [entry["id"]])
    if split is not None:
        entry = {**entry, "content": split[1], "chunk_count": split[2]}

    if json_out:
        import json  # noqa: PLC0415 — stdlib import kept branch-local
        click.echo(json.dumps(entry, indent=2))
    else:
        title = entry.get("title", "")
        tags = entry.get("tags", "")
        indexed_at = (entry.get("indexed_at") or "")[:10]
        # nexus-1oguj: mirrors the MCP store_get display (mcp/core.py) —
        # extraction_method is canonical for PDF chunks now; absent for
        # non-PDF chunks and for chunks indexed before the fix shipped
        # (new-writes-only, no backfill; nexus-0qc4b).
        extraction_method = entry.get("extraction_method", "")
        click.echo(f"ID:         {entry['id']}")
        click.echo(f"Collection: {col_name}")
        if title:
            click.echo(f"Title:      {title}")
        if tags:
            click.echo(f"Tags:       {tags}")
        if indexed_at:
            click.echo(f"Indexed:    {indexed_at}")
        if extraction_method:
            click.echo(f"Extractor:  {extraction_method}")
        click.echo(f"\n{entry.get('content', '')}")


def _reap_catalog_for_doc_ids(doc_ids: list[str], *, expected_collection: str | None) -> None:
    """Best-effort: tombstone catalog entries for T3 docs about to be deleted.

    Why: ``nx store delete`` removed only the T3 doc, leaving the catalog
    entry visible to ``nx catalog list`` until the next ``nx catalog gc``.
    Eventual consistency surprised users who expected delete to be atomic.
    Skipped silently when the catalog is uninitialised.

    Thin wrapper around :func:`nexus.catalog.store_hook.reap_catalog_manifest_for_chashes`
    (nexus-o8dil.5: relocated to that lower layer so ``db/http_vector_client.py``'s
    ``expire()`` can share it too, without a db-layer-imports-commands-layer
    inversion). Kept here, at this name, because ``tests/test_5axey_chash_catalog_lookups.py``
    and ``tests/test_kmo9h_catalog_gate_census.py`` import and call it directly.

    CALL BEFORE deleting the T3 chunk(s), not after — see the relocated
    function's docstring for why the order is load-bearing (RDR-191 F10c's
    anti-join). CALL ONLY after confirming *doc_ids* actually exist in the
    target T3 collection (nexus-c53hy) — its chash resolution is scoped to
    *expected_collection* plus blank-collection ghosts (nexus-r3cdg), so
    calling it before knowing whether the T3 delete will find anything can
    still tombstone a ghost that happens to share the chash. See ``delete_cmd``'s ``--id``
    branch for the existence-check-first call shape.

    *doc_ids* are T3 chunk natural ids (chashes), not tumblers.
    *expected_collection* is REQUIRED, no default (nexus-h7nax — see
    :func:`reap_catalog_manifest_for_chashes`'s docstring for why a
    defaultable guard is the recurring failure mode here). Forwarded
    verbatim as a defense-in-depth scoping guard; pass ``None`` explicitly
    when there is genuinely no collection to scope to.
    """
    from nexus.catalog.store_hook import reap_catalog_manifest_for_chashes  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    reap_catalog_manifest_for_chashes(doc_ids, expected_collection=expected_collection)


@store.command("delete")
@click.option("--collection", "-c", required=True,
              help="Collection name (required)")
@click.option("--id", "doc_id", default=None,
              help="Exact 64-char content-hash document ID from 'nx store list'")
@click.option("--title", default=None,
              help="Title of the document(s) to delete; a chunk another document "
                   "still holds is kept and that document named")
@click.option("--yes", "-y", is_flag=True, default=False,
              help="Skip confirmation prompt")
def delete_cmd(collection: str, doc_id: str | None, title: str | None, yes: bool) -> None:
    """Delete an entry from a T3 knowledge collection.

    Use --id for a single known entry, --title to delete all chunks of a document.
    To remove an entire collection use: nx collection delete <name>

    Note (RDR-108 D1 / RDR-180): T3 chunk natural IDs are content-derived
    (the full sha256(text) hexdigest), so two documents with identical
    content share one T3 row. --title removes the named document and keeps
    any chunk another document still holds, naming that document
    (nexus-sis0m.5). --id names a chunk; one that two documents share is
    kept for both.
    """
    if not doc_id and not title:
        raise click.UsageError("provide --id or --title")
    if doc_id and title:
        raise click.UsageError("--id and --title are mutually exclusive")

    db = _t3()
    col_name = t3_collection_name(collection, t3=db)

    if doc_id:
        # nexus-c53hy (RDR-191 P2 round-2 fix): resolve-and-VERIFY before
        # reaping. A collection-scoped T3 existence check runs FIRST -- a
        # doc_id that is not actually in col_name (bogus/stale, or paired
        # with the wrong --collection) is caught here, before the catalog
        # reap ever fires. When this landed the reap resolved purely by
        # chash and could tombstone a live, unrelated document owning this
        # exact chash under a DIFFERENT collection -- the inverse of the
        # F10c bug class this whole area exists to fix. The reap is now
        # collection-scoped itself (nexus-r3cdg); this check stays as the
        # primary layer.
        if db.get_by_id(col_name, doc_id) is None:
            raise click.ClickException(f"Entry {doc_id!r} not found in {col_name}")

        # nexus-o8dil.5 (RDR-191 F10c follow-up): reap BEFORE delete, not
        # after. PgVectorRepository#delete's anti-join refuses to delete a
        # chash any LIVE manifest row still references -- including this
        # very document's own not-yet-tombstoned manifest row. Reaping
        # first tombstones the owning document so the anti-join sees no
        # live reference and the delete can actually succeed; reaping
        # after (the old order) left the manifest row live at delete
        # time, so the delete was silently refused and the reap ran too
        # late to matter. See store_hook.reap_catalog_manifest_for_chashes.
        # Now gated on the existence check above, and scoped to col_name
        # as a second defense-in-depth layer.
        _reap_catalog_for_doc_ids([doc_id], expected_collection=col_name)
        if not db.delete_by_id(col_name, doc_id):
            # Existence was confirmed a moment ago, so this is NOT a plain
            # "not found" -- either a genuine race (something else deleted
            # it concurrently) or the delete hit the anti-join because the
            # reap above declined to act (e.g. an ambiguous chash shared
            # with another live document). Distinguish from the true
            # not-found case above; the reap for this id has already run.
            raise click.ClickException(
                f"Entry {doc_id!r} existed in {col_name} moments ago but the "
                "delete did not remove it -- likely anti-join-protected by "
                "another live reference, or a concurrent modification. Any "
                "catalog cleanup for it has already run; check 'nx catalog "
                "list' / 'nx catalog reconcile'."
            )
        click.echo(f"Deleted: {doc_id}  from  {col_name}")
    else:
        _delete_by_title(db, col_name, title, yes)


def _delete_by_title(db: T3Database, col_name: str, title: str, yes: bool) -> None:
    """``--title`` names documents (nexus-sis0m.5).

    Catalog documents titled *title* are tombstoned with their own manifest
    rows retracted first; their chunks are then deleted, and any chunk
    another live document still holds is kept and that document named.
    Chunks whose row title matches but that no catalog document owns (a
    note stored before the catalog, or with the catalog down) go through
    the older chunk-title path alongside.
    """
    from nexus.catalog.store_hook import (  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
        live_holders_of_chashes,
        reap_catalog_documents_by_title,
        title_reap_candidates,
    )

    row_ids = db.find_ids_by_title(col_name, title)

    def present(chash: str) -> bool:
        return db.get_by_id(col_name, chash) is not None

    docs = reap_catalog_documents_by_title(col_name, title, row_ids, present) if yes else None
    if not yes:
        # Count before asking, act after: the reap tombstones, so it cannot
        # run ahead of the confirmation. The count is the reap's own
        # candidate set, so the prompt names what will be touched.
        from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

        reader = make_catalog_reader()
        n_docs = 0
        if reader is not None:
            try:
                n_docs = len(title_reap_candidates(reader, col_name, title, row_ids, present))
            finally:
                reader.close()
        if not n_docs and not row_ids:
            raise click.ClickException(f"No entries with title {title!r} in {col_name}")
        click.echo(
            f"Found {n_docs} document(s) and {len(row_ids)} chunk(s) titled "
            f"{title!r} in {col_name}."
        )
        click.confirm("Delete?", abort=True)
        docs = reap_catalog_documents_by_title(col_name, title, row_ids, present)

    reaped = docs.documents if docs else ()
    if not reaped and not row_ids and not (docs and docs.failures):
        raise click.ClickException(f"No entries with title {title!r} in {col_name}")

    # Chunks titled *title* that no reaped document named: the chunk-title
    # path, reap-before-delete, exactly as before.
    doc_chashes = list(docs.chashes) if docs else []
    # A failed document's chunks are held back from the chunk-title path
    # too: that path resolves a chunk to its sole owner and would reap the
    # very document the title reap just refused.
    skip = set(doc_chashes) | set(docs.held if docs else ())
    loose = [i for i in row_ids if i not in skip]
    if loose:
        _reap_catalog_for_doc_ids(loose, expected_collection=col_name)
    targets = list(dict.fromkeys(doc_chashes + loose))

    for tumbler, _t in reaped:
        click.echo(f"Deleted document {title!r} ({tumbler}) from {col_name}.")
    # nexus-o8dil.45: the server-reported count, never len(targets): the
    # engine's anti-join keeps a chunk another live document still holds.
    try:
        deleted = db.batch_delete(col_name, targets) if targets else 0
    except Exception as exc:
        # The documents above are already tombstoned; their chunks stay
        # until a retry or purge-trash sweeps them.
        raise click.ClickException(
            f"the chunk delete failed after {len(reaped)} document(s) were "
            f"tombstoned ({type(exc).__name__}: {exc}); re-run the delete to "
            "remove the chunks."
        ) from exc
    if deleted:
        click.echo(f"Deleted {deleted} {'entry' if deleted == 1 else 'entries'} with title {title!r} from {col_name}.")
    kept = len(targets) - deleted
    holders = live_holders_of_chashes(col_name, targets) if kept > 0 else {}
    if kept > 0:
        named = sorted({f"{t!r} ({tb})" for hs in holders.values() for tb, t in hs})
        click.echo(
            f"  {kept} of {len(targets)} chunk(s) kept: still held by "
            + (", ".join(named) if named else "another live document")
            + ".",
            err=True,
        )
    failures = docs.failures if docs else ()
    for tumbler, err in failures:
        click.echo(f"  document {tumbler} was NOT deleted: {err}", err=True)
    if failures or (not reaped and deleted == 0):
        # *title* was not removed as asked: a document left live, or only
        # uncataloged chunks, every one of them kept.
        raise click.ClickException(
            f"{title!r} was not fully deleted from {col_name}; see above."
        )


@store.command("expire")
def expire_cmd() -> None:
    """Remove T3 knowledge__ entries whose TTL has expired."""
    count = _t3().expire()
    click.echo(f"Expired {count} {'entry' if count == 1 else 'entries'}.")


def _echo_left_out(result: dict) -> None:
    """Say which documents the import left chunks out of, and why. A document another run left
    ``indexing`` or ``failed`` is kept by the same rule as a finished one, but it is not one "with a
    different chunk list": it is unfinished, and the message says that."""
    docs = result.get("unowned_documents") or []
    if not result.get("unowned_count") or not docs:
        return
    target = result.get("collection_name", "")
    unfinished = [d for d in docs if d.get("index_state") in ("indexing", "failed")]
    current = [d for d in docs if d.get("index_state") not in ("indexing", "failed")]
    restore = (
        "To restore one from this file instead, delete it (this discards its current version, and "
        "--title removes every document with that title in the collection), then import again:"
    )
    for group, lead in (
        (current, "their {n} document(s) already exist with a different chunk list, which an import "
                  "never replaces, and a chunk is never stored without its owner."),
        (unfinished, "their {n} document(s) were left unfinished by another run (another export, "
                     "or an index run, left them indexing or failed), and an import finishes "
                     "only the runs of its own file; a chunk is never stored without its owner."),
    ):
        if not group:
            continue
        left = sum(int(d.get("left_out") or 0) for d in group)
        click.echo(f"  {left} records were left out of the import: " + lead.format(n=len(group)) + " " + restore)
        for d in group[:5]:
            title = d.get("title")
            tumbler = d.get("tumbler")
            if title is None:
                click.echo(f"    (could not look up document {tumbler}'s title; see nx catalog show {tumbler})")
            elif not title:
                click.echo(f"    (document {tumbler} has no title; see nx catalog show {tumbler})")
            else:
                # Catalog titles are user and file data: quote them so a
                # pasted command cannot run anything else.
                safe = "".join(ch for ch in title if ch.isprintable())
                click.echo(f"    nx store delete -c {shlex.quote(target)} --title {shlex.quote(safe)}")
        if len(group) > 5:
            click.echo(f"    ... and {len(group) - 5} more")


def _resolve_bare_subject(collection: str, *, t3: object | None = None, for_write: bool = False) -> str:
    """Resolve a ``--collection`` argument for export and import the way
    every other store verb does (:func:`t3_collection_name` with *t3*).

    A bare subject resolved from nexus-8o7ae on; a legacy two-segment name
    still passed raw, reached the exporter's model gate unresolved, where
    the model is guessed from the prefix, and a bge install was refused as
    a Voyage target, while export looked for a collection by that literal
    name (nexus-sis0m.5). With *t3*, an existing legacy collection is
    grandfathered and a missing one promoted to the conformant name.

    A write naming a placeholder subject is refused by the resolver, except
    into a collection that already exists under exactly that name: that is
    a restore, the case ``allow_placeholder`` exists for, and a raw legacy
    import such as ``knowledge__knowledge`` worked before this resolved.
    """
    from nexus.corpus import split_candidate_collection_name  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    restore = bool(
        for_write and t3 is not None
        and split_candidate_collection_name(collection)[0]
        and t3.collection_exists(collection)  # type: ignore[attr-defined]
    )
    return t3_collection_name(
        collection, t3=t3, for_write=for_write, allow_placeholder=restore,
    )


@store.command("export")
@click.argument("collection", default="", required=False)
@click.option("--output", "-o", default=None,
              help="Output file path (.nxexp) or directory (when --all).")
@click.option("--include", "includes", multiple=True,
              help="Glob pattern matched against source_path. Repeat for OR logic.")
@click.option("--exclude", "excludes", multiple=True,
              help="Glob pattern matched against source_path. Repeat for OR logic.")
@click.option("--all", "export_all", is_flag=True, default=False,
              help="Export every collection to separate .nxexp files.")
def export_cmd(
    collection: str,
    output: str | None,
    includes: tuple[str, ...],
    excludes: tuple[str, ...],
    export_all: bool,
) -> None:
    """Export a T3 collection to a portable .nxexp backup file.

    The export preserves all documents, metadata, and embeddings, enabling
    later import without re-embedding (saves Voyage AI API costs).

    \b
    Examples:
      nx store export code__myrepo -o myrepo-backup.nxexp
      nx store export code__myrepo --include "*.py" -o python-only.nxexp
      nx store export --all
      nx store export --all -o /path/to/backup-dir/
    """
    from datetime import date  # noqa: PLC0415 — stdlib import kept branch-local

    from nexus.errors import EmbeddingModelMismatch, FormatVersionError  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
    from nexus.exporter import export_collection  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    if export_all and collection:
        raise click.UsageError("Cannot specify COLLECTION together with --all.")
    if not export_all and not collection:
        raise click.UsageError("Provide a COLLECTION name or use --all.")

    db = _t3()

    if export_all:
        # One .nxexp file per collection; output may be a directory.
        out_dir = Path(output) if output else Path.cwd()
        if output and not out_dir.exists():
            out_dir.mkdir(parents=True, exist_ok=True)
        collections_info = db.list_collections()
        if not collections_info:
            click.echo("No collections found.")
            return
        today = date.today().isoformat()
        total_exported = 0
        for info in collections_info:
            col_name: str = info["name"]
            fname = f"{col_name}-{today}.nxexp"
            out_path = out_dir / fname
            try:
                result = export_collection(
                    db=db,
                    collection_name=col_name,
                    output_path=out_path,
                    includes=includes,
                    excludes=excludes,
                )
                click.echo(
                    f"Exported {result['exported_count']:>6} records  "
                    f"{col_name}  ->  {out_path.name}"
                )
                total_exported += result["exported_count"]
            except Exception as exc:  # noqa: BLE001 — per-collection export failure surfaced via click.echo, loop continues
                click.echo(f"ERROR exporting {col_name}: {exc}", err=True)
        click.echo(f"\nTotal: {total_exported} records across {len(collections_info)} collections.")
    else:
        col_name = _resolve_bare_subject(collection, t3=db)
        out_path = Path(output) if output else Path(f"{col_name}.nxexp")
        try:
            result = export_collection(
                db=db,
                collection_name=col_name,
                output_path=out_path,
                includes=includes,
                excludes=excludes,
            )
        except (EmbeddingModelMismatch, FormatVersionError) as exc:
            raise click.ClickException(str(exc)) from exc
        except Exception as exc:
            raise click.ClickException(f"Export failed: {exc}") from exc

        size_kb = result["file_bytes"] / 1024
        click.echo(
            f"Exported {result['exported_count']} records from {col_name} "
            f"-> {out_path}  ({size_kb:.1f} KB, {result['elapsed_seconds']:.1f}s)"
        )


@store.command("import")
@click.argument("file", type=click.Path(exists=True))
@click.option("--collection", "-c", default=None,
              help="Override target collection name (default: from export header).")
@click.option("--remap", "remaps", multiple=True,
              help="Path substitution: /old/path:/new/path  (repeat for multiple remaps).")
@click.option("--assume-model", default=None,
              help="Override the export header's declared embedding model. "
                   "Pre-migration .nxexp files can carry a wrong label (GH #1370); "
                   "use this to supply the true model instead of trusting the header.")
@click.option("--skip-existing", is_flag=True, default=False,
              help="Do not send the text or vector of a record whose chunk the target collection "
                   "already holds: the stored chunk and vector stay, and the record still gets "
                   "its owner. Without it every record is written with the file's vector, which "
                   "replaces a stored one.")
def import_cmd(
    file: str,
    collection: str | None,
    remaps: tuple[str, ...],
    assume_model: str | None,
    skip_existing: bool,
) -> None:
    """Import a .nxexp export file into T3.

    Embedding model validation is enforced: importing a code__ export into a
    docs__ collection (or vice versa) is rejected to prevent silent corruption
    of the target collection's vector space. Non-conformant legacy chunk ids
    (pre-migration backups) are re-hashed to content-derived ids automatically.

    \b
    Examples:
      nx store import myrepo-backup.nxexp
      nx store import myrepo-backup.nxexp --remap "/old/path:/new/path"
      nx store import myrepo-backup.nxexp --collection code__newname
      nx store import old-backup.nxexp --assume-model bge-base-en-v15-768
      nx store import partial-backup.nxexp --skip-existing
    """
    from nexus.errors import (  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
        EmbeddingDimensionMismatch,
        EmbeddingModelMismatch,
        FormatVersionError,
    )
    from nexus.exporter import import_collection  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    # Parse --remap options (format: old:new).
    parsed_remaps: list[tuple[str, str]] = []
    for remap in remaps:
        if ":" not in remap:
            raise click.UsageError(
                f"--remap requires old:new format (e.g. /old/path:/new/path), "
                f"got: {remap!r}"
            )
        old, new = remap.split(":", 1)
        if not old:
            raise click.UsageError(
                f"--remap old prefix cannot be empty, got: {remap!r}"
            )
        parsed_remaps.append((old, new))

    db = _t3()
    input_path = Path(file)
    # nexus-8o7ae: passed raw, a bare subject reached the exporter's model
    # gate unresolved and was refused as a voyage-code-3 target on a bge
    # install. The resolve's two profile refusals name their remedy; exit
    # cleanly with it, as put does (7.38.0 shakeout), not with a traceback.
    if collection:
        try:
            collection = _resolve_bare_subject(collection, t3=db, for_write=True)
        except (EmbeddingProfileMismatchError, LocalVoyageCredentialMissingError) as exc:
            raise click.ClickException(str(exc)) from exc

    # nexus-s71lr: "nx store put"/import bulk writes had NO progress signal at
    # all -- import_collection is one opaque call with no per-record callback,
    # so a large .nxexp file is total silence until it returns (worse than the
    # per-file loops: not even a start/end line per record). Reuses the exact
    # `_PhaseHeartbeat` mechanism `nx index rdr` / `nx index pdf --dir` arm:
    # ticks every 5s for as long as the call is in flight, disarmed in
    # finally so a raise from import_collection itself never leaks the
    # background thread (code-review-expert finding d: arm() sits immediately
    # before the try/finally that guards it, nothing risky in between).
    from nexus.commands.index import _PhaseHeartbeat  # noqa: PLC0415 — deferred cross-module import; avoids a hard import-time coupling between two independently-loadable command modules
    file_heartbeat = _PhaseHeartbeat(
        is_tty=sys.stdout.isatty(),
        echo=lambda msg, nl: click.echo(msg, nl=nl, err=True),
        interval=5.0,
        prefix="import",
    )
    file_heartbeat.arm(f"importing {input_path.name}")
    try:
        try:
            result = import_collection(
                db=db,
                input_path=input_path,
                target_collection=collection,
                remaps=parsed_remaps,
                assume_model=assume_model,
                skip_existing=skip_existing,
            )
        except FormatVersionError as exc:
            raise click.ClickException(str(exc)) from exc
        except EmbeddingDimensionMismatch as exc:
            raise click.ClickException(str(exc)) from exc
        except EmbeddingModelMismatch as exc:
            raise click.ClickException(str(exc)) from exc
        except Exception as exc:
            raise click.ClickException(f"Import failed: {exc}") from exc
    finally:
        file_heartbeat.disarm()

    click.echo(
        f"Imported {result['imported_count']} records into "
        f"{result['collection_name']}  ({result['elapsed_seconds']:.1f}s)"
    )
    if result.get("skipped_count"):
        click.echo(
            f"  Skipped {result['skipped_count']} records: already stored (--skip-existing), or "
            "belonging to a document that keeps its current chunk list."
        )
    if result.get("owned_count"):
        click.echo(f"  {result['owned_count']} records are owned by a catalog document.")
    if result.get("vector_mismatches"):
        n = result["vector_mismatches"]
        click.echo(
            f"  {n} stored vector{'s' if n != 1 else ''} differed from the file's and "
            f"{'were' if n != 1 else 'was'} replaced by it."
        )
    if result.get("sweep_skipped"):
        n = result["sweep_skipped"]
        click.echo(
            f"  {n} document{'s' if n != 1 else ''} replaced an earlier chunk list whose old chunks "
            "could not be swept. The documents are complete; those chunks stay stored, owned by "
            "no document, until `nx t3 gc` removes them."
        )
    _echo_left_out(result)
    if result.get("rehashed_count"):
        click.echo(
            f"  Re-hashed {result['rehashed_count']} non-conformant legacy "
            "chunk ids to conformant content hashes."
        )
