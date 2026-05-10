# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx t3`` command group — T3 (ChromaDB) maintenance.

``nx t3 prune-stale`` (RDR-090 P1.4 / nexus-u7r0) sweeps each T3
collection's source_path values, removes chunks whose on-disk source
file is missing.

``nx t3 gc`` (RDR-101 Phase 6 / nexus-r5eo) is the SOLE post-Phase-3
emitter of ``ChunkOrphaned`` events and the SOLE post-Phase-3 path that
deletes T3 chunks. It joins the catalog projection (alive doc_ids per
collection) with T3 chunk metadata and removes chunks whose ``doc_id``
is dead AND whose ``indexed_at`` predates the orphan window.

The collection mode iterates ``T3Database.list_unique_source_paths``
plus a ``Path(p).exists()`` check; the staleness predicate is
intentionally simple (file present / absent) — broken-symlink
handling and partial-content checks are out of scope.

Out of scope:
  - Catalog-side prune-stale (``nx catalog prune-stale``) is a
    separate bead (nexus-zg4c).
  - The ``nx collection audit --verify-chroma`` cross-check between
    catalog chunk_ids and chroma chunk_ids (GH #335) shares the
    drift-detection idea but is a different surface.
"""
from __future__ import annotations

import json
import os
import re
import signal
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import click
import structlog

_log = structlog.get_logger(__name__)

# SIG-6 (nexus-872w): resumable backfill state file.
# The state file is a JSON dict mapping collection name → list of doc_ids
# that have been processed, or ["__done__"] when a collection is complete.
# Atomic writes via .tmp + rename avoid partial-write corruption on crash.
_BACKFILL_STATE_FILE_ENV = "NEXUS_BACKFILL_STATE_FILE"
_BACKFILL_STATE_DEFAULT = os.path.expanduser(
    "~/.config/nexus/backfill_state.json"
)
_PROGRESS_INTERVAL = 10  # emit progress every N docs across all collections


_DEFAULT_ORPHAN_WINDOW = "30d"
_WINDOW_PATTERN = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$", re.IGNORECASE)
_WINDOW_UNIT_SECONDS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
}


def _parse_orphan_window(spec: str) -> timedelta:
    """Parse ``"30d"`` / ``"24h"`` / ``"2w"`` into a :class:`timedelta`.

    Supports s/m/h/d/w suffixes. A bare integer is rejected: operators
    must be explicit about the unit so a typo cannot silently mean
    ``30 seconds`` instead of ``30 days``. Zero (``"0d"``) is rejected:
    a zero window means every chunk older than "now" is eligible,
    which is rarely intentional and is dangerous when paired with
    ``--no-dry-run --yes``.
    """
    match = _WINDOW_PATTERN.match(spec)
    if not match:
        raise click.BadParameter(
            f"--orphan-window must be e.g. '30d' / '12h' / '2w', got {spec!r}"
        )
    n = int(match.group(1))
    if n <= 0:
        raise click.BadParameter(
            f"--orphan-window must be positive, got {spec!r}. "
            f"A zero or negative window would treat every orphaned chunk "
            f"as immediately eligible for deletion."
        )
    unit = match.group(2).lower()
    return timedelta(seconds=n * _WINDOW_UNIT_SECONDS[unit])


def _backfill_state_path() -> Path:
    """Return the path to the backfill state file.

    Respects ``NEXUS_BACKFILL_STATE_FILE`` env override so tests can
    redirect the file to a tmp directory without touching the real config.
    """
    return Path(os.environ.get(_BACKFILL_STATE_FILE_ENV, _BACKFILL_STATE_DEFAULT))


def _load_backfill_state(path: Path) -> dict[str, list[str]]:
    """Load the backfill state file, returning an empty dict on miss/error."""
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_backfill_state(path: Path, state: dict[str, list[str]]) -> None:
    """Atomically write the backfill state file (tmp + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(path)


def _make_catalog():
    """Construct the default Catalog with the same init-gate that
    ``commands/catalog.py:_get_catalog`` enforces.

    Without the init gate, running ``nx t3 gc`` on a fresh install
    either crashes with an opaque traceback inside ``Catalog.__init__``
    or, worse, silently produces an empty alive-set so every chunk is
    treated as orphan (catastrophic when paired with --no-dry-run --yes).

    Patched in tests for isolation.
    """
    from nexus.catalog.catalog import Catalog  # noqa: PLC0415
    from nexus.config import catalog_path  # noqa: PLC0415

    path = catalog_path()
    if not Catalog.is_initialized(path):
        raise click.ClickException(
            "Catalog not initialized. Run 'nx catalog setup' before 'nx t3 gc'."
        )
    return Catalog(path, path / ".catalog.db")


def _make_t3_for_backfill():
    """Construct the default T3Database for the backfill command.

    Patched in tests for isolation.
    """
    from nexus.db import make_t3  # noqa: PLC0415
    return make_t3()


@click.group()
def t3() -> None:
    """T3 (ChromaDB) maintenance commands."""


@t3.command("prune-stale")
@click.option(
    "--collection",
    "-c",
    default="",
    help="Limit to one collection. Omit to scan every T3 collection.",
)
@click.option(
    "--dry-run/--no-dry-run",
    default=True,
    help="Report-only (default). Use --no-dry-run to perform deletions.",
)
@click.option(
    "--confirm",
    is_flag=True,
    default=False,
    help="Required alongside --no-dry-run to actually delete chunks "
    "(the explicit-affirmation belt-and-suspenders pattern).",
)
def prune_stale_cmd(collection: str, dry_run: bool, confirm: bool) -> None:
    """Delete T3 chunks whose ``source_path`` is missing from disk.

    \b
    Reports per-collection summary lines: stale source_paths and
    chunk counts. By default this is read-only (--dry-run is on).

    \b
    To actually delete, pass BOTH --no-dry-run AND --confirm. The
    two-flag dance is deliberate: --no-dry-run flips the intent,
    --confirm verifies the operator typed it on purpose. Either flag
    alone runs the report without deleting.

    \b
    Examples:
      nx t3 prune-stale                              # report all collections
      nx t3 prune-stale -c rdr__nexus-571b8edd       # one collection
      nx t3 prune-stale --no-dry-run --confirm       # actually delete
    """
    from nexus.db import make_t3  # noqa: PLC0415

    will_delete = (not dry_run) and confirm
    if (not dry_run) and not confirm:
        click.echo(
            "--no-dry-run alone is treated as report-only. "
            "Add --confirm to actually delete chunks."
        )
        will_delete = False

    t3_db = make_t3()

    if collection:
        target_collections = [collection]
    else:
        try:
            all_colls = t3_db.list_collections()
        except Exception as exc:
            click.echo(f"Failed to list collections: {exc}")
            raise click.exceptions.Exit(1)
        target_collections = [c["name"] for c in all_colls]

    if not target_collections:
        click.echo("No collections to scan.")
        return

    total_stale_paths = 0
    total_stale_chunks = 0
    affected_collections = 0

    # nexus-6ims: relative source_paths must resolve against the
    # owning catalog document's owner.repo_root, not against the
    # running process's cwd. Open the catalog so we can join.
    from nexus.catalog.catalog import Catalog
    from nexus.config import catalog_path as _catalog_path
    cat_dir = _catalog_path()
    cat: Catalog | None = None
    owner_roots: dict[str, str] = {}
    if (cat_dir / "documents.jsonl").exists():
        try:
            cat = Catalog(cat_dir, cat_dir / ".catalog.db")
            owner_roots = dict(cat._db.execute(
                "SELECT tumbler_prefix, repo_root FROM owners "
                "WHERE repo_root != ''"
            ))
        except Exception as exc:
            click.echo(f"WARN: catalog not available for owner lookup: {exc}")

    def _resolve(path_str: str) -> Path | None:
        """Resolve a chunk source_path. Absolute → as-is. Relative → look
        up the owning document's owner.repo_root and prepend. Returns
        None if no owner.repo_root anchor available."""
        if path_str.startswith("/"):
            return Path(path_str)
        if not cat:
            return None
        # Find any catalog document with this file_path; take its owner.
        row = cat._db.execute(
            "SELECT tumbler FROM documents WHERE file_path = ? LIMIT 1",
            (path_str,),
        ).fetchone()
        if not row:
            return None
        parts = row[0].split(".")
        if len(parts) < 2:
            return None
        owner_id = ".".join(parts[:2])
        root = owner_roots.get(owner_id)
        if not root:
            return None
        return Path(root) / path_str

    skipped_unverifiable = 0

    for coll_name in target_collections:
        try:
            unique_paths = t3_db.list_unique_source_paths(coll_name)
        except Exception as exc:
            click.echo(f"  {coll_name}: SKIP (list failed: {exc})")
            continue

        stale_paths: list[str] = []
        for p in unique_paths:
            resolved = _resolve(p)
            if resolved is None:
                # Relative path with no owner anchor — refuse to
                # classify (would have falsely deleted under the
                # nexus-6ims pre-fix logic).
                skipped_unverifiable += 1
                continue
            if not resolved.exists():
                stale_paths.append(p)
        if not stale_paths:
            continue

        affected_collections += 1
        click.echo(f"\n{coll_name}: {len(stale_paths)} stale source_path(s)")
        coll_chunks = 0
        for p in stale_paths:
            try:
                ids = t3_db.ids_for_source(coll_name, p)
            except Exception as exc:
                click.echo(f"  {p}: SKIP (ids_for_source failed: {exc})")
                continue
            click.echo(f"  {p}  ->  {len(ids)} chunk(s)")
            coll_chunks += len(ids)
            if will_delete:
                try:
                    deleted = t3_db.delete_by_source(coll_name, p)
                    if deleted != len(ids):
                        click.echo(
                            f"    WARN: deleted {deleted}, expected {len(ids)}"
                        )
                except Exception as exc:
                    click.echo(f"    delete failed: {exc}")
        total_stale_paths += len(stale_paths)
        total_stale_chunks += coll_chunks

    verb = "deleted" if will_delete else "would delete"
    click.echo(
        f"\nSummary: {verb} {total_stale_chunks} chunk(s) "
        f"across {total_stale_paths} stale path(s) "
        f"in {affected_collections} collection(s)."
    )
    if skipped_unverifiable:
        click.echo(
            f"  Skipped {skipped_unverifiable} relative-path entries "
            f"whose owning document has no owner.repo_root — cannot "
            f"verify presence (nexus-6ims fail-safe)."
        )


@t3.command("gc")
@click.option(
    "--collection",
    "-c",
    required=True,
    help="Collection to GC. Required (orphan diff is per-collection).",
)
@click.option(
    "--orphan-window",
    default=_DEFAULT_ORPHAN_WINDOW,
    show_default=True,
    help="Grace period before an orphaned chunk becomes eligible for "
    "deletion. Format: e.g. '30d', '12h', '2w'. The default protects "
    "against transient orphans during a re-index.",
)
@click.option(
    "--dry-run/--no-dry-run",
    default=True,
    help="Report-only (default). Use --no-dry-run to actually delete.",
)
@click.option(
    "--yes",
    is_flag=True,
    default=False,
    help="Required alongside --no-dry-run to actually delete chunks. "
    "Without --yes, the command falls back to report-only.",
)
def gc_cmd(
    collection: str,
    orphan_window: str,
    dry_run: bool,
    yes: bool,
) -> None:
    """Garbage-collect orphaned T3 chunks (RDR-101 Phase 6).

    \b
    A chunk is an orphan when:
      - its ``doc_id`` metadata is not in the catalog projection's
        alive set for ``--collection``, AND
      - its ``indexed_at`` predates ``--orphan-window`` (default 30d).

    \b
    Per RF-101-3, ``nx t3 gc`` is the SOLE post-Phase-3 emitter of
    ``ChunkOrphaned`` events and the SOLE path that physically deletes
    T3 chunks. The strict order on each candidate is:

        1. Append ``ChunkOrphaned(chunk_id, reason)`` to the event log.
        2. Call ``T3Database.delete_by_chunk_ids`` for that chunk.

    \b
    A crash between (1) and (2) leaves the log consistent with T3 (event
    present + delete failed): the next ``nx t3 gc`` run idempotently
    retries the delete. The opposite ordering would leave T3 ahead of
    the log (delete succeeded + crash before event), violating
    replay-equality.

    \b
    Chunks missing ``doc_id`` (legacy pre-Phase-2 backfill) are
    UNDECIDABLE here and skipped: operators must run a maintenance
    backfill verb, not GC, to address them.

    \b
    NOTE (RDR-108 Phase 4): post-Phase-3 chunks have no ``doc_id`` in
    metadata and so are skipped here. The manifest-based GC inside
    ``nx index`` (``indexer._prune_deleted_files``) handles them.
    Reconciliation of the two paths is tracked in nexus-e5aw.

    \b
    Examples:
      nx t3 gc -c knowledge__delos --dry-run                # report only
      nx t3 gc -c rdr__nexus-571b8edd --no-dry-run --yes    # actually GC
      nx t3 gc -c code__nexus --orphan-window 7d --dry-run  # tighter window
    """
    from nexus.catalog.event_log import EventLog  # noqa: PLC0415
    from nexus.catalog.events import (  # noqa: PLC0415
        ChunkOrphanedPayload,
        make_event,
    )
    from nexus.db import make_t3  # noqa: PLC0415

    window = _parse_orphan_window(orphan_window)
    cutoff = datetime.now(UTC) - window

    will_delete = (not dry_run) and yes
    if (not dry_run) and not yes:
        click.echo(
            "--no-dry-run alone is treated as report-only. "
            "Add --yes to actually delete chunks."
        )
        will_delete = False

    t3_db = make_t3()
    cat = _make_catalog()

    try:
        alive = {e.tumbler for e in cat.list_by_collection(collection)}
    except Exception as exc:
        click.echo(f"Failed to read catalog: {exc}")
        raise click.exceptions.Exit(1)
    alive_str = {str(t) for t in alive}

    candidates: list[tuple[str, str]] = []
    skipped_no_doc_id = 0
    skipped_no_indexed_at = 0
    skipped_within_window = 0

    try:
        chunks = list(t3_db.list_chunks_with_metadata(collection))
    except Exception as exc:
        click.echo(f"Failed to list chunks for {collection}: {exc}")
        raise click.exceptions.Exit(1)

    for chunk_id, meta in chunks:
        doc_id = meta.get("doc_id", "")
        if not doc_id:
            skipped_no_doc_id += 1
            continue
        if doc_id in alive_str:
            continue
        indexed_at = meta.get("indexed_at", "")
        if not indexed_at:
            skipped_no_indexed_at += 1
            continue
        try:
            indexed_dt = datetime.fromisoformat(indexed_at)
        except ValueError:
            skipped_no_indexed_at += 1
            continue
        if indexed_dt > cutoff:
            skipped_within_window += 1
            continue
        candidates.append((chunk_id, doc_id))

    click.echo(
        f"{collection}: {len(candidates)} orphan chunk(s) eligible "
        f"(window={orphan_window})"
    )
    if skipped_no_doc_id:
        click.echo(f"  skipped {skipped_no_doc_id} chunk(s) with no doc_id")
    if skipped_no_indexed_at:
        click.echo(
            f"  skipped {skipped_no_indexed_at} chunk(s) with no/bad indexed_at"
        )
    if skipped_within_window:
        click.echo(
            f"  skipped {skipped_within_window} chunk(s) inside the orphan window"
        )

    for chunk_id, doc_id in candidates:
        click.echo(f"  {chunk_id}  ->  doc_id={doc_id}")

    if not candidates:
        click.echo("\nSummary: 0 orphan(s); nothing to do.")
        return

    if not will_delete:
        click.echo(
            f"\nSummary: would delete {len(candidates)} chunk(s) from {collection}."
        )
        return

    # Strict order (RF-101-3): event first, then delete. The event-emit
    # loop runs per-chunk so the log records each ChunkOrphaned BEFORE
    # any chunk is deleted; the actual delete is BATCHED into one call
    # afterward (delete_by_chunk_ids paginates internally at 300). A
    # crash between event-emit and batch-delete leaves the log
    # consistent with T3 (events present, all chunks still in T3); the
    # next gc run idempotently re-emits and retries the batch.
    #
    # NOTE on the alive snapshot: ``alive_str`` was sampled at the top
    # of this command. A doc registered concurrently between snapshot
    # and execution would not appear in the alive set, so its chunks
    # could be GC'd despite the doc being live again. Operators
    # SHOULD NOT run gc concurrently with active indexing. The window
    # is single-operator-driven and acceptably small in practice.
    event_log = EventLog(cat._dir)
    pending_chunk_ids: list[str] = []
    for chunk_id, doc_id in candidates:
        event = make_event(
            ChunkOrphanedPayload(
                chunk_id=chunk_id,
                reason=f"doc_id {doc_id} no longer alive in {collection}",
            )
        )
        event_log.append(event)
        pending_chunk_ids.append(chunk_id)

    deleted_total = 0
    delete_failed = 0
    try:
        deleted_total = t3_db.delete_by_chunk_ids(collection, pending_chunk_ids)
    except Exception as exc:
        delete_failed = len(pending_chunk_ids)
        click.echo(
            f"  batch delete failed ({len(pending_chunk_ids)} chunk(s)): "
            f"{exc}. Events were emitted; next 'nx t3 gc' run will retry.",
            err=True,
        )

    if delete_failed:
        click.echo(
            f"\nSummary: emitted {len(pending_chunk_ids)} ChunkOrphaned "
            f"event(s); batch delete FAILED for {delete_failed} chunk(s)."
        )
    else:
        click.echo(
            f"\nSummary: deleted {deleted_total} chunk(s) from {collection}."
        )


@t3.command("backfill-manifest")
@click.option(
    "--collection",
    "-c",
    default="",
    help=(
        "Limit to one collection. Omit to backfill all collections "
        "registered in the catalog."
    ),
)
@click.option(
    "--dry-run/--no-dry-run",
    default=True,
    help="Report-only (default). Use --no-dry-run to write manifest rows.",
)
@click.option(
    "--limit",
    "-n",
    default=0,
    type=int,
    help="If > 0, process at most N documents per collection.",
)
@click.option(
    "--resume/--no-resume",
    default=False,
    help=(
        "Resume a previous interrupted backfill. Reads state from "
        f"$NEXUS_BACKFILL_STATE_FILE (default: {_BACKFILL_STATE_DEFAULT}). "
        "Collections marked done are skipped; others are re-processed "
        "from scratch (per-doc idempotency comes from write_manifest)."
    ),
)
def backfill_manifest_cmd(
    collection: str,
    dry_run: bool,
    limit: int,
    resume: bool,
) -> None:
    """Backfill document_chunks manifest from T3 chunk metadata (RDR-108 D2).

    \\b
    Reads T3 chunk metadata (doc_id, chunk_index, chunk_text_hash, span
    coordinates) per catalog document and writes one row per chunk into
    the ``document_chunks`` manifest table. After this runs the catalog
    can answer "what chunks compose a Document and in what order?" without
    consulting T3 metadata.

    \\b
    The backfill is idempotent: re-running overwrites the manifest with
    the same content (DELETE + INSERT in one transaction per document).

    \\b
    Carve-outs:
      - taxonomy__* collections are skipped (centroids have no chunk_text_hash).
      - Pre-RDR-053 chunks missing chunk_text_hash raise an error; re-index
        that collection before running backfill.

    \\b
    Progress is written to stderr. Use --resume to continue after Ctrl-C.
    On SIGINT the state file is flushed before exit so --resume can pick
    up where it left off.

    \\b
    Examples:
      nx t3 backfill-manifest --dry-run                         # report only
      nx t3 backfill-manifest -c code__nexus --no-dry-run       # one collection
      nx t3 backfill-manifest --no-dry-run                      # all collections
      nx t3 backfill-manifest --no-dry-run -n 100               # first 100 docs
      nx t3 backfill-manifest --no-dry-run --resume             # continue after Ctrl-C
    """
    from nexus.catalog.manifest_backfill import (  # noqa: PLC0415
        MissingChunkHashError,
        backfill_manifest_for_collection,
    )

    cat = _make_catalog()
    t3_db = _make_t3_for_backfill()

    if dry_run:
        click.echo("(dry-run: no manifest rows will be written)")

    if collection:
        collections_to_process = [collection]
    else:
        # All collections registered in the catalog.
        collections_to_process = [
            c["name"] for c in cat.list_collections()
        ]
        if not collections_to_process:
            click.echo("No collections registered in catalog; nothing to do.")
            return

    total = len(collections_to_process)

    # SIG-6: load resume state and skip already-done collections.
    state_path = _backfill_state_path()
    state: dict[str, list[str]] = {}
    if resume:
        state = _load_backfill_state(state_path)
        done_before = sum(1 for v in state.values() if v == ["__done__"])
        if done_before:
            print(
                f"Resuming: {done_before} collection(s) already done, skipping.",
                file=sys.stderr,
            )

    # SIG-6: SIGINT handler — flush state then exit 130.
    def _on_sigint(signum: int, frame: object) -> None:  # noqa: ARG001
        if state:
            _save_backfill_state(state_path, state)
            print(
                f"\nInterrupted. Progress saved to {state_path}. "
                f"Re-run with --resume to continue.",
                file=sys.stderr,
            )
        sys.exit(130)

    try:
        signal.signal(signal.SIGINT, _on_sigint)
    except (ValueError, OSError):
        # Non-main thread (e.g. test runner) — skip signal registration.
        pass

    total_docs = 0
    total_chunks = 0
    total_skipped_no_t3 = 0
    skipped_taxonomy = 0
    errors: list[str] = []
    docs_processed_overall = 0

    for idx, coll_name in enumerate(collections_to_process, start=1):
        # SIG-6: skip collections already complete in resume state.
        if resume and state.get(coll_name) == ["__done__"]:
            print(
                f"[{idx}/{total}] {coll_name}: skipped (already done)",
                file=sys.stderr,
            )
            continue

        print(
            f"[{idx}/{total}] {coll_name}: processing ...",
            file=sys.stderr,
        )

        try:
            result = backfill_manifest_for_collection(
                cat, t3_db, coll_name, dry_run=dry_run, limit=limit
            )
        except MissingChunkHashError as exc:
            click.echo(
                f"ERROR: {exc}",
                err=True,
            )
            errors.append(str(exc))
            continue
        except Exception as exc:
            click.echo(
                f"ERROR in {coll_name}: {exc}",
                err=True,
            )
            errors.append(f"{coll_name}: {exc}")
            continue

        if result.skipped_taxonomy:
            click.echo(f"  {coll_name}: skipped (taxonomy carve-out)")
            skipped_taxonomy += 1
            continue

        # SIG-6: per-collection stderr progress including skipped-no-t3.
        verb = "would write" if dry_run else "wrote"
        skipped_part = (
            f" ({result.docs_skipped_no_t3} skipped: no_t3)"
            if result.docs_skipped_no_t3
            else ""
        )
        print(
            f"[{idx}/{total}] {coll_name}: processed {result.docs_processed} "
            f"doc(s), {verb} {result.chunks_written} chunk manifest row(s)"
            f"{skipped_part}",
            file=sys.stderr,
        )

        # Emit to stdout as well for the summary output.
        click.echo(
            f"  {coll_name}: processed {result.docs_processed} doc(s), "
            f"{verb} {result.chunks_written} chunk manifest row(s)"
            + (
                f" ({result.docs_skipped_no_t3} skipped: no T3 collection)"
                if result.docs_skipped_no_t3
                else ""
            )
        )

        total_docs += result.docs_processed
        total_chunks += result.chunks_written
        total_skipped_no_t3 += result.docs_skipped_no_t3
        docs_processed_overall += result.docs_processed

        # SIG-6: periodic progress every _PROGRESS_INTERVAL docs.
        if docs_processed_overall % _PROGRESS_INTERVAL == 0 and docs_processed_overall > 0:
            print(
                f"  ... {docs_processed_overall} docs processed so far",
                file=sys.stderr,
            )

        # SIG-6: mark collection done in state file (atomic write).
        if not dry_run:
            state[coll_name] = ["__done__"]
            _save_backfill_state(state_path, state)

    verb = "would write" if dry_run else "wrote"
    skipped_no_t3_part = (
        f", {total_skipped_no_t3} doc(s) skipped (no T3 collection)"
        if total_skipped_no_t3
        else ""
    )
    click.echo(
        f"\nSummary: processed {total_docs} doc(s), "
        f"{verb} {total_chunks} manifest row(s)"
        + skipped_no_t3_part
        + (f", skipped {skipped_taxonomy} taxonomy collection(s)" if skipped_taxonomy else "")
        + (f", {len(errors)} error(s)" if errors else "")
    )

    if errors:
        raise SystemExit(1)


@t3.command("reidentify")
@click.option(
    "--collection",
    "-c",
    default="",
    help="Limit to one collection. Mutually exclusive with --all-collections.",
)
@click.option(
    "--all-collections",
    "all_collections",
    is_flag=True,
    default=False,
    help="Re-identify every T3 collection. Mutually exclusive with --collection.",
)
@click.option(
    "--dry-run/--no-dry-run",
    default=True,
    help="Report-only (default). Use --no-dry-run to perform the migration.",
)
@click.option(
    "--max-workers",
    type=int,
    default=4,
    show_default=True,
    help=(
        "Number of collections to process in parallel under "
        "--all-collections. Each collection has an independent ID "
        "namespace so concurrent execution is safe; ChromaDB Cloud "
        "rate limits are the practical ceiling. Set to 1 for "
        "deterministic serial output."
    ),
)
def reidentify_cmd(
    collection: str,
    all_collections: bool,
    dry_run: bool,
    max_workers: int,
) -> None:
    """Re-upsert T3 chunks under content-derived natural IDs (RDR-108 D1).

    \b
    Per collection, paginates T3 chunks (300/op), computes a new natural
    ID from chunk_text_hash[:32], and re-upserts each chunk under the new
    ID using the existing embedding (no Voyage call). Document-level
    metadata fields (doc_id, chunk_index, chunk_count) are stripped at
    re-upsert; the catalog manifest table is now authoritative for those.
    Old chunk IDs are batch-deleted after the get-loop completes.

    \b
    The command is idempotent: re-running on a fully-migrated collection
    is a zero-write no-op. It is also crash-resumable: re-invoking after
    an interrupted run safely sweeps the un-deleted old IDs.

    \b
    Carve-outs:
      - taxonomy__* collections are skipped (centroids use centroid_hash).
      - Pre-RDR-053 chunks missing chunk_text_hash raise an error;
        re-index that collection from source before running.

    \b
    Performance (RDR-108 nexus-qlm2):
      - --all-collections processes collections in parallel via a
        ThreadPoolExecutor (--max-workers, default 4). Each collection
        has an independent ID namespace so concurrent execution is
        correctness-preserving; the practical ceiling is the operator's
        ChromaDB Cloud rate limits, not local CPU.
      - Per-collection completion order is non-deterministic under
        max_workers > 1. Pass --max-workers 1 for serial dispatch and
        operator-readable output.

    \b
    Examples:
      nx t3 reidentify --collection code__nexus            # dry-run report
      nx t3 reidentify -c code__nexus --no-dry-run         # one collection
      nx t3 reidentify --all-collections --no-dry-run      # full corpus, 4 workers
      nx t3 reidentify --all-collections --max-workers 8   # higher concurrency
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from nexus.db.t3_reidentify import (  # noqa: PLC0415
        MissingChunkHashError,
        reidentify_collection,
    )

    # XOR: exactly one of --collection / --all-collections must be set.
    if bool(collection) == bool(all_collections):
        raise click.UsageError(
            "Specify exactly one of --collection NAME or --all-collections."
        )

    if max_workers < 1:
        raise click.UsageError("--max-workers must be >= 1.")

    t3_db = _make_t3_for_backfill()

    if dry_run:
        click.echo("(dry-run: no T3 writes or deletes will be performed)")

    if collection:
        collections_to_process = [collection]
    else:
        collections_to_process = [
            c["name"] for c in t3_db.list_collections()
        ]
        if not collections_to_process:
            click.echo("No T3 collections found; nothing to do.")
            return

    total = len(collections_to_process)
    # Single-collection invocations are inherently serial; skip the
    # executor overhead and keep the per-collection progress line shape
    # the operator already knows from --collection mode.
    workers = min(max_workers, total) if total > 1 else 1

    def _process_one(idx: int, coll_name: str) -> tuple[
        int, str, "object | None", str | None
    ]:
        """Run reidentify_collection in a worker. Returns (idx, name,
        result_or_None, error_or_None) so the main thread can render
        output deterministically by index."""
        print(
            f"[{idx}/{total}] {coll_name}: processing ...",
            file=sys.stderr,
        )
        try:
            res = reidentify_collection(t3_db, coll_name, dry_run=dry_run)
        except MissingChunkHashError as exc:
            return idx, coll_name, None, str(exc)
        except Exception as exc:
            return idx, coll_name, None, f"{coll_name}: {exc}"
        return idx, coll_name, res, None

    total_examined = 0
    total_migrated = 0
    total_already = 0
    total_deleted = 0
    skipped_taxonomy = 0
    errors: list[str] = []

    if workers == 1:
        # Deterministic serial path: dispatch + render in input order.
        results_iter = (
            _process_one(i, n)
            for i, n in enumerate(collections_to_process, start=1)
        )
    else:
        # Parallel dispatch; collect as completed (out-of-order render).
        executor = ThreadPoolExecutor(max_workers=workers)
        try:
            futures = [
                executor.submit(_process_one, i, n)
                for i, n in enumerate(collections_to_process, start=1)
            ]
            results_iter = (f.result() for f in as_completed(futures))
        finally:
            executor.shutdown(wait=False)

    for _idx, coll_name, result, error in results_iter:
        if error is not None:
            click.echo(f"ERROR: {error}", err=True)
            errors.append(error)
            continue

        if result.skipped_taxonomy:
            click.echo(f"  {coll_name}: skipped (taxonomy carve-out)")
            skipped_taxonomy += 1
            continue

        verb = "would migrate" if dry_run else "migrated"
        delete_part = (
            f", {result.chunks_deleted} old id(s) deleted"
            if not dry_run and result.chunks_deleted
            else ""
        )
        click.echo(
            f"  {coll_name}: examined {result.chunks_examined} chunk(s), "
            f"{verb} {result.chunks_migrated}, "
            f"{result.chunks_already_migrated} already migrated"
            + delete_part
        )

        total_examined += result.chunks_examined
        total_migrated += result.chunks_migrated
        total_already += result.chunks_already_migrated
        total_deleted += result.chunks_deleted

    verb = "would migrate" if dry_run else "migrated"
    click.echo(
        f"\nSummary: examined {total_examined} chunk(s) across "
        f"{total} collection(s); {verb} {total_migrated}, "
        f"{total_already} already migrated, "
        f"{total_deleted} old id(s) deleted"
        + (f", skipped {skipped_taxonomy} taxonomy" if skipped_taxonomy else "")
        + (f", {len(errors)} error(s)" if errors else "")
    )

    if errors:
        raise SystemExit(1)
