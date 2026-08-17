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
# nexus-69c94 critique S1+C2: a THIRD marker, ["__partial__", "fk_409=N",
# "chash_divergent=M", "zero_chunks=K"], means the collection finished this
# run WITHOUT error but left N+M+K docs un-healed (all three classes are
# caught per-doc and never raise -- zero_chunks is the dominant live gap
# class and can become recoverable after a Phase-5 remediation pass) --
# --resume treats it as pending, NOT done, and reprocesses the whole
# collection.
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
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — command-local import deferred to avoid CLI startup cost (nexus.catalog.factory)

    cat = make_catalog_reader()
    if cat is None:
        raise click.ClickException(
            "Catalog not initialized. Run 'nx catalog setup' before 'nx t3 gc'."
        )
    return cat


def _make_t3_for_backfill():
    """Construct the default T3Database for the backfill command.

    Patched in tests for isolation.
    """
    from nexus.db import make_t3  # noqa: PLC0415 — command-local import deferred to avoid CLI startup cost (nexus.db)
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
    """RETIRED — reported a false all-clear; superseded by an existing pair.

    \b
    It swept chunks by their ``source_path`` metadata. RDR-102 D2 hard-removed
    that key from the chunk schema, so the sweep matched nothing and every run
    printed a clean "0 stale" no matter how many indexed files had been deleted
    from disk. It also opened the LOCAL catalog by probing for
    ``documents.jsonl``, a file that has not existed since nexus-i711w — two
    independent reasons it could not have worked.

    \b
    It does not need rebuilding: the catalog-native pipeline already exists and
    covers the work, though NOT strictly — the retired verb swept every
    collection and ``nx t3 gc`` has no such mode, so the GC half must be looped
    (nexus-iitif). ``nx catalog prune-stale`` drops documents whose
    file_path is missing (resolving relative paths against the owner's
    repo_root, and refusing to classify what it cannot verify), and ``nx t3 gc``
    then collects the chunks those documents no longer reference. Doing it in
    that order also avoids the failure mode a chunk-only sweep creates: chunks
    deleted out from under a live manifest are exactly the dangling-manifest
    state ``nx doctor`` now flags (nexus-5xn3k). nexus-bm8dd.
    """
    _ = (collection, dry_run, confirm)
    raise click.ClickException(
        "nx t3 prune-stale is RETIRED (nexus-bm8dd).\n"
        "\n"
        "It swept T3 chunks by source_path metadata. RDR-102 D2 removed that "
        "key from the chunk schema, so the sweep has matched nothing since — "
        "and reported a clean '0 stale' on every collection rather than saying "
        "it could not look. A verb that cannot find anything must not be "
        "mistaken for one that found nothing.\n"
        "\n"
        "Use the catalog-native pipeline:\n"
        "  nx catalog prune-stale [--collection COLLECTION] --no-dry-run --confirm\n"
        "  nx t3 gc -c COLLECTION --no-dry-run --yes\n"
        "\n"
        "Prune first, GC second: deleting chunks while their document still "
        "references them leaves a dangling manifest (nexus-5xn3k).\n"
        "\n"
        "NOTE: `nx t3 gc` REQUIRES -c — the orphan diff is per-collection, so "
        "there is no sweep-every-collection mode to replace the one this verb "
        "advertised. `nx catalog prune-stale` does take all collections. To GC "
        "every collection, enumerate and loop:\n"
        "  nx collection list | awk '{print $1}' | xargs -I{} "
        "nx t3 gc -c {} --no-dry-run --yes\n"
        "(nexus-iitif tracks whether gc should gain an all-collections mode.)"
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
@click.option(
    "--allow-empty-manifest-set",
    is_flag=True,
    default=False,
    help="Override the empty-alive-set refusal (nexus-jqrtp). DANGEROUS: "
    "only pass this once you've confirmed the collection really is fully "
    "orphaned, not a fresh/mis-scoped tenant or an unbackfilled manifest.",
)
@click.option(
    "--allow-incomplete-index-state",
    is_flag=True,
    default=False,
    help="Override the RUNFENCE index-state refusal (nexus-g6k6b). "
    "DANGEROUS: only pass this once you've confirmed no reindex is "
    "concurrently running against this collection (an in-flight or "
    "fence-failed document's chunks are not garbage — they are a run "
    "in progress or a documented-damaged state with its own remedy).",
)
def gc_cmd(
    collection: str,
    orphan_window: str,
    dry_run: bool,
    yes: bool,
    allow_empty_manifest_set: bool,
    allow_incomplete_index_state: bool,
) -> None:
    """Garbage-collect orphaned T3 chunks via the catalog manifest (RDR-108 Phase 4).

    \b
    A chunk is an orphan when:
      - its full ``meta.chunk_text_hash`` is NOT referenced by any
        manifest entry in the catalog ``document_chunks`` table for
        ``--collection``, AND
      - its ``indexed_at`` predates ``--orphan-window`` (default 30d).

    \b
    The manifest path matches what ``indexer._prune_deleted_files``
    (run at the end of ``nx index``) does. Both paths are now
    semantically equivalent; this CLI is the operator-driven one with
    explicit dry-run + --yes confirmation, plus ``ChunkOrphaned``
    event emission for audit trail.

    \b
    Per RF-101-3, ``nx t3 gc`` is the SOLE emitter of ``ChunkOrphaned``
    events. The strict order on each candidate is:

        1. Append ``ChunkOrphaned(chunk_id, reason)`` to the event log.
        2. Call ``T3Database.delete_by_chunk_ids`` for that chunk.

    \b
    A crash between (1) and (2) leaves the log consistent with T3 (event
    present + delete failed): the next ``nx t3 gc`` run idempotently
    retries the delete. The opposite ordering would leave T3 ahead of
    the log (delete succeeded + crash before event), violating
    replay-equality.

    \b
    Chunks missing ``chunk_text_hash`` (pre-RDR-053 relics) are
    UNDECIDABLE here and skipped with a warning: re-index the source
    or run ``nx t3 reidentify`` to populate the field. Same carve-out
    as the indexer's manifest GC.

    \b
    NOTE (RDR-108 Phase 4): post-Phase-3 chunks have no ``doc_id`` in
    metadata and so are skipped here. The manifest-based GC inside
    ``nx index`` (``indexer._prune_deleted_files``) handles them.
    Reconciliation of the two paths is tracked in nexus-e5aw.

    \b
    RUNFENCE precondition (nexus-g6k6b): if any document registered
    under ``--collection`` is not ``index_state='complete'`` (actively
    ``'indexing'``, fenced ``'failed'``, or explicitly unresolved/NULL —
    excluding manifest-less notes, which are separately and always
    protected), a ``--no-dry-run --yes`` run REFUSES rather than risk
    deleting chunks an in-flight or fence-damaged reindex has not yet
    re-manifested. Pass ``--allow-incomplete-index-state`` once you have
    confirmed no reindex is concurrently running.

    \b
    Examples:
      nx t3 gc -c knowledge__delos --dry-run                # report only
      nx t3 gc -c rdr__nexus-571b8edd --no-dry-run --yes    # actually GC
      nx t3 gc -c code__nexus --orphan-window 7d --dry-run  # tighter window
    """
    from nexus.db import make_t3  # noqa: PLC0415 — command-local import deferred to avoid CLI startup cost (nexus.db)

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
        # Manifest path (RDR-108 nexus-e5aw): the catalog
        # document_chunks table is the authoritative source of truth
        # for which chashes belong to which collection. A chunk is
        # orphan when its content hash is not referenced by any
        # manifest row for this collection's documents. Same SQL the
        # indexer's _prune_deleted_files uses.
        referenced = cat.chashes_for_collection(collection)
    except Exception as exc:  # noqa: BLE001 — boundary catch; logged then re-raised as a domain error
        click.echo(f"Failed to read catalog manifest: {exc}")
        raise click.exceptions.Exit(1)

    # nexus-39upx hazard 2 (RDR-145) + nexus-g6k6b (RUNFENCE precondition):
    # chashes_for_collection only sees chashes with a manifest row. A
    # store_put / nx store put NOTE never gets one — RDR-145 defers
    # manifest-backed identity for notes, and catalog-003-soft-delete.xml's
    # live_chunks view treats a manifest-less chunk as live BY DESIGN — so
    # a note's chash is indistinguishable from a chash that fell out of a
    # live document's manifest via re-index; both simply read "not
    # referenced" above. Separately, Hal's 2026-08-02 comment on this bead
    # is a BINDING requirement: the corpus-wide sweep must filter on
    # index_state='complete', since sweeping a document that is mid-index
    # (or fence-failed) could delete chunks an in-flight run has already
    # written but has not yet manifested.
    #
    # ONE fetch (catalog_documents_for_collection) serves BOTH guards.
    # Deliberately FAILS LOUD on lookup failure, not fail-open: this is
    # the operator-driven --yes path, and an unverifiable note/index-state
    # set must refuse the run rather than risk deleting live content.
    from nexus.indexer_utils import (  # noqa: PLC0415 — command-local import deferred to avoid CLI startup cost (nexus.indexer_utils)
        catalog_documents_for_collection,
        live_note_chashes,
        non_complete_documents,
    )

    try:
        collection_documents = catalog_documents_for_collection(cat, collection)
    except Exception as exc:  # noqa: BLE001 — boundary catch; refuses the run rather than risk deleting live content
        click.echo(
            f"Failed to verify manifest-less note protection / index-run "
            f"state for {collection!r}: {exc}. Refusing to compute orphan "
            f"candidates without it — a store_put/nx store put note's "
            f"chash is indistinguishable from a genuine re-index orphan "
            f"without this check (nexus-39upx hazard 2 / nexus-g6k6b)."
        )
        raise click.exceptions.Exit(1)

    note_chashes = live_note_chashes(collection_documents)
    if note_chashes:
        click.echo(
            f"  protecting {len(note_chashes)} manifest-less note "
            f"chunk(s) from orphan classification (RDR-145)"
        )
    referenced = referenced | note_chashes

    incomplete_docs = non_complete_documents(collection_documents)
    if incomplete_docs:
        _states = ", ".join(sorted({
            f"{d.title!r}={d.index_state!r}" for d in incomplete_docs
        }))
        click.echo(
            f"  {len(incomplete_docs)} document(s) in {collection!r} are "
            f"not index_state='complete' ({_states}) — a --no-dry-run "
            f"--yes run will REFUSE until they resolve, or pass "
            f"--allow-incomplete-index-state (nexus-g6k6b)"
        )

    candidates: list[tuple[str, str]] = []  # (chunk_id, chash)
    skipped_no_chash = 0
    skipped_no_indexed_at = 0
    skipped_within_window = 0

    try:
        chunks = list(t3_db.list_chunks_with_metadata(
            collection, fields=("chunk_text_hash", "indexed_at"),
        ))
    except Exception as exc:  # noqa: BLE001 — boundary catch; logged then re-raised as a domain error
        click.echo(f"Failed to list chunks for {collection}: {exc}")
        raise click.exceptions.Exit(1)


    for chunk_id, meta in chunks:
        chash = meta.get("chunk_text_hash") or ""  # RDR-180: full digest — the truncation was the quarantine-class bug
        if not chash:
            # Pre-RDR-053 relic: no content hash to compare against
            # the manifest. Skip with operator-visible count. Same
            # carve-out semantics as indexer._prune_deleted_files.
            skipped_no_chash += 1
            continue
        if chash in referenced:
            continue  # live: manifest still references this chunk
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
        candidates.append((chunk_id, chash))

    click.echo(
        f"{collection}: {len(candidates)} orphan chunk(s) eligible "
        f"(window={orphan_window})"
    )
    if skipped_no_chash:
        click.echo(
            f"  skipped {skipped_no_chash} chunk(s) with no chunk_text_hash "
            f"(pre-RDR-053 relics; re-index source or run 'nx t3 reidentify')"
        )
    if skipped_no_indexed_at:
        click.echo(
            f"  skipped {skipped_no_indexed_at} chunk(s) with no/bad indexed_at"
        )
    if skipped_within_window:
        click.echo(
            f"  skipped {skipped_within_window} chunk(s) inside the orphan window"
        )

    for chunk_id, chash in candidates:
        click.echo(f"  {chunk_id}  ->  chash={chash}")

    if not candidates:
        click.echo("\nSummary: 0 orphan(s); nothing to do.")
        return

    if not will_delete:
        click.echo(
            f"\nSummary: would delete {len(candidates)} chunk(s) from {collection}."
        )
        return

    # nexus-jqrtp: the empty-alive-set guard. `cat is None` in _make_catalog()
    # was written to stop exactly this catastrophe but only ever fired for a
    # SQLite-only "catalog absent" condition; in service mode the factory
    # always returns a handle, so it never fired there — and "catalog
    # PRESENT but its manifest references NOTHING for this collection" was
    # never guarded on either substrate. An empty `referenced` set here is
    # reachable without anything client-visible being wrong (fresh/mis-scoped
    # tenant, an unbackfilled manifest, a collection with no catalog
    # projection) and makes EVERY chunk in the collection an orphan
    # candidate — the last line of defence before --no-dry-run --yes deletes
    # it all. Refuse unless the operator has confirmed the collection really
    # is fully orphaned and passed --allow-empty-manifest-set.
    if not referenced and not allow_empty_manifest_set:
        click.echo(
            f"\nREFUSING to delete: the catalog manifest for '{collection}' "
            f"references ZERO chashes, but {len(candidates)} chunk(s) are "
            f"about to be treated as orphans. This is indistinguishable from "
            f"a fresh/mis-scoped tenant or an unbackfilled manifest without "
            f"deciding the collection's disposition first. Investigate with "
            f"'nx t3 backfill-manifest -c {collection}' or "
            f"'nx catalog reconcile', or — if the collection really is "
            f"fully orphaned — re-run with --allow-empty-manifest-set."
        )
        raise click.exceptions.Exit(1)

    # nexus-g6k6b (RUNFENCE precondition, nexus-39upx round 2 CRITICAL):
    # Hal's 2026-08-02 comment, binding, verbatim: "The corpus-wide sweep
    # (b) MUST filter on index_state = 'complete'. Sweeping a document
    # that is mid-index would delete chunks an in-flight run has already
    # written but has not yet manifested." A T3 chunk carries no doc_id
    # (post-RDR-108), so a candidate cannot be attributed to the specific
    # document that most recently owned it — the conservative,
    # structurally-honest response when candidates cannot be attributed
    # to individual documents is a collection-level circuit breaker: ANY
    # non-complete document in this collection means no candidate can be
    # PROVEN safe, so refuse rather than delete anything.
    if incomplete_docs and not allow_incomplete_index_state:
        _names = ", ".join(sorted({
            f"{d.title!r} ({d.index_state!r})" for d in incomplete_docs
        }))
        click.echo(
            f"\nREFUSING to delete: {len(incomplete_docs)} document(s) in "
            f"'{collection}' are not index_state='complete': {_names}. "
            f"An in-flight or fence-failed document's chunks are not "
            f"garbage — they are a run in progress or a documented-damaged "
            f"state with its own remedy (finish the reindex, or re-index "
            f"with --force). If you have confirmed no reindex is "
            f"concurrently running against this collection, re-run with "
            f"--allow-incomplete-index-state."
        )
        raise click.exceptions.Exit(1)

    # NOTE on the manifest snapshot: ``referenced`` was sampled at the
    # top of this command. A doc registered concurrently between
    # snapshot and execution would not appear in the referenced set,
    # so its chunks could be GC'd despite the doc being live again. The
    # index_state check above closes the common case (a NEW re-index
    # begun after this command started would stamp 'indexing' before its
    # first chunk upsert — nexus-5xn3k.4 fence-begin ordering — and would
    # be caught by the NEXT invocation, though not retroactively by this
    # already-in-flight one); the residual snapshot-to-execution race is
    # single-operator-driven and acceptably small in practice.
    #
    # AUDIT-TRAIL WINDOW (nexus-i711w item 20, Hal ruling 2026-07-30):
    # the local ChunkOrphaned EventLog emission died with the local
    # git-backed catalog. Until the engine's gc-audit surface ships
    # (next engine tag), destructive T3 GC has no audit record — an
    # explicitly accepted window; do not re-litigate here.
    pending_chunk_ids: list[str] = [chunk_id for chunk_id, _chash in candidates]

    deleted_total = 0
    delete_failed = 0
    try:
        deleted_total = t3_db.delete_by_chunk_ids(collection, pending_chunk_ids)
    except Exception as exc:  # noqa: BLE001 — best-effort path; failure logged, must not crash caller
        delete_failed = len(pending_chunk_ids)
        click.echo(
            f"  batch delete failed ({len(pending_chunk_ids)} chunk(s)): "
            f"{exc}. Next 'nx t3 gc' run will retry.",
            err=True,
        )

    if delete_failed:
        click.echo(
            f"\nSummary: batch delete FAILED for {delete_failed} chunk(s)."
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
@click.option(
    "--only-gapped/--no-only-gapped",
    default=False,
    help=(
        "Touch ONLY documents with zero manifest rows (nexus-3n7pr). "
        "Skips any document that already has >=1 manifest row via a "
        "batched pre-pass, before any T3 read or write -- use this for a "
        "targeted repair pass so healthy manifests are never rewritten. "
        "Default off (unchanged pre-existing behavior: every document in "
        "the collection is processed)."
    ),
)
def backfill_manifest_cmd(
    collection: str,
    dry_run: bool,
    limit: int,
    resume: bool,
    only_gapped: bool,
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
      nx t3 backfill-manifest --no-dry-run --only-gapped        # repair pass: zero-manifest docs only
    """
    from nexus.catalog.manifest_backfill import (  # noqa: PLC0415 — command-local import deferred to avoid CLI startup cost (nexus.catalog.manifest_backfill)
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
        # nexus-69c94 critique S1: collections left __partial__ by a prior
        # run (nonzero fk_409/chash_divergent residual) are NOT skipped --
        # they will be reprocessed below like any other pending collection.
        # Surface the count so the operator knows this run's job includes
        # retrying them.
        partial_before = sum(
            1 for v in state.values() if v and v[0] == "__partial__"
        )
        if partial_before:
            print(
                f"Resuming: {partial_before} collection(s) left partial by "
                f"a prior run (fk_409/chash_divergent residual) will be "
                f"reprocessed.",
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
    total_skipped_zero_chunks = 0
    total_skipped_phase3_no_index = 0
    total_skipped_has_manifest = 0
    total_skipped_chash_divergent = 0
    total_skipped_fk_409 = 0
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
                cat, t3_db, coll_name, dry_run=dry_run, limit=limit,
                only_gapped=only_gapped,
            )
        except MissingChunkHashError as exc:
            click.echo(
                f"ERROR: {exc}",
                err=True,
            )
            errors.append(str(exc))
            continue
        except Exception as exc:  # noqa: BLE001 — best-effort path; failure logged, must not crash caller
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
        # nexus-gvmbo: zero-chunk-match skips must be COUNTED and visible in
        # the command's own output, never silent -- this is what turns the
        # "never write empty" fix into an observable non-vacuity guarantee.
        zero_chunks_part = (
            f" ({result.docs_skipped_zero_chunks} skipped: zero_chunks)"
            if result.docs_skipped_zero_chunks
            else ""
        )
        # nexus-3n7pr G2: docs_skipped_phase3_no_index was counted but never
        # printed -- the dry run IS the sizing instrument for a remediation
        # pass, so an unhealable class that isn't printed under-reports it.
        phase3_no_index_part = (
            f" ({result.docs_skipped_phase3_no_index} skipped: phase3_no_index)"
            if result.docs_skipped_phase3_no_index
            else ""
        )
        # nexus-3n7pr G1: --only-gapped skips surfaced the same way as the
        # other skip classes -- never silent.
        has_manifest_part = (
            f" ({result.docs_skipped_has_manifest} skipped: has_manifest)"
            if result.docs_skipped_has_manifest
            else ""
        )
        # nexus-dmf7r: chash-id/metadata divergence skips -- never silent.
        chash_divergent_part = (
            f" ({result.docs_skipped_chash_divergent} skipped: chash_divergent)"
            if result.docs_skipped_chash_divergent
            else ""
        )
        # nexus-r7g3i: FK-409 skips -- never silent.
        fk_409_part = (
            f" ({result.docs_skipped_fk_409} skipped: fk_409)"
            if result.docs_skipped_fk_409
            else ""
        )
        print(
            f"[{idx}/{total}] {coll_name}: processed {result.docs_processed} "
            f"doc(s), {verb} {result.chunks_written} chunk manifest row(s)"
            f"{skipped_part}{zero_chunks_part}{phase3_no_index_part}"
            f"{has_manifest_part}{chash_divergent_part}{fk_409_part}",
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
            + (
                f" ({result.docs_skipped_zero_chunks} skipped: zero chunk matches)"
                if result.docs_skipped_zero_chunks
                else ""
            )
            + (
                f" ({result.docs_skipped_phase3_no_index} skipped: phase3 no chunk_index)"
                if result.docs_skipped_phase3_no_index
                else ""
            )
            + (
                f" ({result.docs_skipped_has_manifest} skipped: already has manifest)"
                if result.docs_skipped_has_manifest
                else ""
            )
            + (
                f" ({result.docs_skipped_chash_divergent} skipped: chash id/metadata divergent)"
                if result.docs_skipped_chash_divergent
                else ""
            )
            + (
                f" ({result.docs_skipped_fk_409} skipped: FK conflict)"
                if result.docs_skipped_fk_409
                else ""
            )
        )

        total_docs += result.docs_processed
        total_chunks += result.chunks_written
        total_skipped_no_t3 += result.docs_skipped_no_t3
        total_skipped_zero_chunks += result.docs_skipped_zero_chunks
        total_skipped_phase3_no_index += result.docs_skipped_phase3_no_index
        total_skipped_has_manifest += result.docs_skipped_has_manifest
        total_skipped_chash_divergent += result.docs_skipped_chash_divergent
        total_skipped_fk_409 += result.docs_skipped_fk_409
        docs_processed_overall += result.docs_processed

        # SIG-6: periodic progress every _PROGRESS_INTERVAL docs.
        if docs_processed_overall % _PROGRESS_INTERVAL == 0 and docs_processed_overall > 0:
            print(
                f"  ... {docs_processed_overall} docs processed so far",
                file=sys.stderr,
            )

        # SIG-6: mark collection done in state file (atomic write).
        #
        # nexus-69c94 critique S1+C2 (substantive-critic, T2 scratch
        # 79007753 / T2 [22640]): docs_skipped_fk_409 and
        # docs_skipped_chash_divergent are caught PER DOC inside
        # backfill_manifest_for_collection and never raise -- a collection
        # containing them still returns normally, so marking it __done__
        # unconditionally would make --resume PERMANENTLY skip those
        # un-healed docs (the plan's stated safety property, "a collection
        # that errored is NOT marked done", would silently stop holding for
        # this sub-case). docs_skipped_zero_chunks joins the residual too
        # (critique C2): it is the DOMINANT live gap class (894/895 in the
        # nexus-3n7pr population) and a future remediation pass (re-index /
        # re-put) can make those exact docs recoverable -- a collection
        # whose only gaps are zero_chunks must stay revisitable by
        # --resume, not be permanently marked done. Mark __partial__
        # instead, carrying the residual counts for operator visibility --
        # --resume then reprocesses the WHOLE collection (per-doc
        # idempotency makes a full re-pass safe and cheap for docs that
        # already healed) rather than trusting a bare --resume to have
        # picked the residual up.
        if not dry_run:
            residual = (
                result.docs_skipped_fk_409
                + result.docs_skipped_chash_divergent
                + result.docs_skipped_zero_chunks
            )
            if residual > 0:
                state[coll_name] = [
                    "__partial__",
                    f"fk_409={result.docs_skipped_fk_409}",
                    f"chash_divergent={result.docs_skipped_chash_divergent}",
                    f"zero_chunks={result.docs_skipped_zero_chunks}",
                ]
                click.echo(
                    f"  {coll_name}: NOT marked done -- {residual} doc(s) "
                    f"skipped (fk_409={result.docs_skipped_fk_409}, "
                    f"chash_divergent={result.docs_skipped_chash_divergent}, "
                    f"zero_chunks={result.docs_skipped_zero_chunks}); "
                    f"a future --resume will reprocess this collection"
                )
            else:
                state[coll_name] = ["__done__"]
            _save_backfill_state(state_path, state)

    verb = "would write" if dry_run else "wrote"
    skipped_no_t3_part = (
        f", {total_skipped_no_t3} doc(s) skipped (no T3 collection)"
        if total_skipped_no_t3
        else ""
    )
    skipped_zero_chunks_part = (
        f", {total_skipped_zero_chunks} doc(s) skipped (zero chunk matches)"
        if total_skipped_zero_chunks
        else ""
    )
    # nexus-3n7pr G2: print in the summary too -- was counted, never surfaced.
    skipped_phase3_no_index_part = (
        f", {total_skipped_phase3_no_index} doc(s) skipped (phase3 no chunk_index)"
        if total_skipped_phase3_no_index
        else ""
    )
    # nexus-3n7pr G1: --only-gapped skip total.
    skipped_has_manifest_part = (
        f", {total_skipped_has_manifest} doc(s) skipped (already has manifest)"
        if total_skipped_has_manifest
        else ""
    )
    # nexus-dmf7r: chash id/metadata divergence skip total.
    skipped_chash_divergent_part = (
        f", {total_skipped_chash_divergent} doc(s) skipped "
        f"(chash id/metadata divergent)"
        if total_skipped_chash_divergent
        else ""
    )
    # nexus-r7g3i: FK-409 skip total.
    skipped_fk_409_part = (
        f", {total_skipped_fk_409} doc(s) skipped (FK conflict)"
        if total_skipped_fk_409
        else ""
    )
    click.echo(
        f"\nSummary: processed {total_docs} doc(s), "
        f"{verb} {total_chunks} manifest row(s)"
        + skipped_no_t3_part
        + skipped_zero_chunks_part
        + skipped_phase3_no_index_part
        + skipped_has_manifest_part
        + skipped_chash_divergent_part
        + skipped_fk_409_part
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
    ID from the full chunk_text_hash (RDR-180), and re-upserts each chunk under the new
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
    from concurrent.futures import ThreadPoolExecutor, as_completed  # noqa: PLC0415 — deliberate deferred import: branch-local / startup-cost avoidance
    from nexus.db.t3_reidentify import (  # noqa: PLC0415 — command-local import deferred to avoid CLI startup cost (nexus.db.t3_reidentify)
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
        except Exception as exc:  # noqa: BLE001 — per-collection worker; error returned in result tuple, not raised
            return idx, coll_name, None, f"{coll_name}: {exc}"
        return idx, coll_name, res, None

    total_examined = 0
    total_migrated = 0
    total_already = 0
    total_deleted = 0
    skipped_taxonomy = 0
    errors: list[str] = []

    def _render(results_iter):
        nonlocal total_examined, total_migrated, total_already, total_deleted
        nonlocal skipped_taxonomy
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

    if workers == 1:
        # Deterministic serial path: dispatch + render in input order.
        _render(
            _process_one(i, n)
            for i, n in enumerate(collections_to_process, start=1)
        )
    else:
        # Parallel dispatch; collect + render INSIDE the executor's
        # ``with`` block so __exit__ blocks until in-flight workers
        # finish (nexus-uv06: prior code called executor.shutdown(
        # wait=False) from a finally that fired BEFORE the lazy
        # results_iter generator was consumed, leaking worker threads
        # if the consumer raised or returned early).
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [
                executor.submit(_process_one, i, n)
                for i, n in enumerate(collections_to_process, start=1)
            ]
            _render(f.result() for f in as_completed(futures))

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
