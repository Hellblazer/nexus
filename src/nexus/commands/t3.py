# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx t3`` command group — T3 vector-store maintenance.

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

import contextlib
import json
import os
import re
import signal
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NoReturn

import click
import structlog

from nexus import config as _config

_log = structlog.get_logger(__name__)

#: How many chunk ids the ``t3_gc_chunks_deleted`` log event and the
#: gc_audit row's ``details.chunk_ids_sample`` carry verbatim (nexus-fduai);
#: the rest is a count. The row's ``chashes`` list is NOT sampled here —
#: the engine caps it itself and keeps ``chash_count`` exact.
_GC_AUDIT_ID_SAMPLE = 50

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
# Doc-only literal (nexus-pfuns): `--resume`'s click help= string is built
# once at decorator-evaluation (module-import) time, so it can never
# reflect a per-invocation NEXUS_CONFIG_DIR override regardless -- this is
# purely informational text, NOT the runtime default (see
# `_backfill_state_path` below for that).
_BACKFILL_STATE_DEFAULT_DOC = "~/.config/nexus/backfill_state.json"
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
    Absent that, falls back to ``nexus_config_dir()/backfill_state.json``
    -- NOT a hardcoded ``~/.config/nexus`` (nexus-pfuns: the old fallback
    was a module-level ``os.path.expanduser(...)`` constant, frozen at
    import and blind to ``NEXUS_CONFIG_DIR`` entirely, same import-time-
    default class already fixed once in ``gc_purge_marker.py`` -- T2
    nexus/gc-purge-marker-xdist-leak-2026-08-20). ``_config.nexus_config_dir``
    is read via module-attribute access (not a by-value ``from nexus.config
    import nexus_config_dir``) so a patch applied after this module is
    already imported still takes effect, and the function itself re-reads
    ``os.environ`` on every call -- both resolved at CALL time, never
    frozen.
    """
    override = os.environ.get(_BACKFILL_STATE_FILE_ENV)
    if override:
        return Path(override)
    return _config.nexus_config_dir() / "backfill_state.json"


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
            "Catalog is empty. Index or store documents before 'nx t3 gc' (nx index repo / nx store put)."
        )
    return cat


def _make_catalog_writer():
    """Open the write-only catalog proxy ``nx t3 gc`` reports its audit row
    through (nexus-fduai). Patched in tests for isolation."""
    from nexus.catalog.factory import make_catalog_writer  # noqa: PLC0415 — command-local import deferred to avoid CLI startup cost (nexus.catalog.factory)

    return make_catalog_writer()


def _make_t3_for_backfill():
    """Construct the default T3Database for the backfill command.

    Patched in tests for isolation.
    """
    from nexus.db import make_t3  # noqa: PLC0415 — command-local import deferred to avoid CLI startup cost (nexus.db)
    return make_t3()


@click.group()
def t3() -> None:
    """T3 vector-store maintenance commands."""


@t3.command("prune-stale", hidden=True)
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
    TOMBSTONE PROTECTION (nexus-dkymw, Sam's second 2026-09-07 ruling,
    superseding nexus-mqd6t's original immediate-exclusion filter): the
    "referenced by any manifest entry" set above is read from the
    catalog's ``chashesForCollection`` alive-set, which now includes a
    tombstoned-but-not-yet-purged document's chashes, not just live
    documents'. Deleting a document with ``nx catalog delete`` does NOT
    make its chunks orphan-eligible here — only ``nx catalog purge-trash``
    physically reclaiming the row does. This keeps ``nx catalog restore``
    honest: without it, this command's own ``--orphan-window`` clock
    (independent of purge-trash's ``--older-than-days``) could reap a
    just-tombstoned document's chunks inside the restore window, and
    restore would resurrect an empty shell.

    \b
    Every run (dry-run and ``--no-dry-run --yes`` alike, nexus-zewg3)
    reports "Protected by pending tombstones: N chunk(s)" — the count of
    chunks in ``--collection`` kept alive ONLY by a tombstone, distinct
    from chunks a live document still references. The engine computes
    this count itself (``CatalogRepository.tombstoneProtectedChunkCount``,
    the same anti-join ``nexus.purge_trash``'s own chunk sweep uses); an
    engine that predates the field reports the line as unavailable rather
    than a confident zero. This is a distinct signal from the note-shaped
    and RUNFENCE protections below: only ``nx catalog purge-trash``
    reclaims this class, never another ``nx t3 gc`` run.

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

    # nexus-sis0m.3: refuse a name no collection carries, here, rather than
    # surface the engine's "register it first via POST
    # /v1/catalog/collections/upsert" 422 (shakeout 7.64.1 Surface E F10) --
    # advice that is wrong for a typo.
    # A registered collection with no chunks is absent from the chunk
    # listing but is a real target, so a catalog registration counts too.
    from nexus.catalog.membership import collection_is_known as _collection_is_known  # noqa: PLC0415 — command-local import (nexus.catalog.membership)

    in_t3 = collection in {c["name"] for c in t3_db.list_collections(strict=True)}
    if not in_t3 and not _collection_is_known(cat, collection):
        raise click.ClickException(
            f"no collection named {collection!r}; 'nx collection list' shows "
            "the names t3 gc takes."
        )

    try:
        # Manifest path (RDR-108 nexus-e5aw): the catalog
        # document_chunks table is the authoritative source of truth
        # for which chashes belong to which collection. A chunk is
        # orphan when its content hash is not referenced by any
        # manifest row for this collection's documents. Same SQL the
        # indexer's _prune_deleted_files uses.
        #
        # nexus-dkymw (Sam's second 2026-09-07 ruling, superseding
        # nexus-mqd6t's original DELETED_AT.isNull() filter for this one
        # read): "referenced" here includes a tombstoned-but-not-yet-
        # purged document's chashes, not just live documents' -- the
        # alive-set protects them until nx catalog purge-trash physically
        # reclaims the row, so this --orphan-window clock cannot reap a
        # just-tombstoned document's chunks inside nx catalog restore's
        # recovery window.
        #
        # nexus-zewg3: of `referenced`, how many chashes are held alive
        # ONLY by a pending tombstone (never by a live document) -- the
        # engine computes this with the SAME anti-join
        # nexus.purge_trash's own chunk sweep uses
        # (CatalogRepository.tombstoneProtectedChunkCount), in the SAME
        # round trip as `referenced` itself. `None` means an engine older
        # than the one that shipped this field -- see the report line
        # below, which never prints a confident 0 for that case.
        referenced, tombstone_protected_count = (
            cat.chashes_for_collection_with_tombstone_protected(collection)
        )
    except Exception as exc:  # noqa: BLE001 — boundary catch; logged then re-raised as a domain error
        click.echo(f"Failed to read catalog manifest: {exc}")
        raise click.exceptions.Exit(1)

    # nexus-39upx hazard 2 (RDR-145) + nexus-g6k6b (RUNFENCE precondition):
    # chashes_for_collection only sees chashes with a manifest row. A
    # legacy store_put / nx store put NOTE (stored before nexus-b6enc) may
    # have none — reads hide such a chunk since RDR-192 Step 5, but this
    # deleting sweep keeps the notes guard until RDR-192 Step 11 — so
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
    # nexus-sis0m.3: count only the note chunks the manifest does not
    # already reference; live_note_chashes returns every note-shaped
    # document's chunk, and the label said "manifest-less" for all of them
    # (shakeout 7.64.1 F11: census-manifest-less read 0 while this read N).
    manifest_less_notes = note_chashes - referenced
    if manifest_less_notes:
        click.echo(
            f"  protecting {len(manifest_less_notes)} manifest-less note "
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

    # nexus-zewg3: always printed, dry-run and --no-dry-run --yes alike --
    # a chash referenced ONLY by a pending tombstone is in `referenced`
    # exactly like a live-referenced one (nexus-dkymw dropped that
    # distinction from chashes_for_collection), so t3 gc will never
    # reclaim it on its own no matter how many times it runs; only
    # nx catalog purge-trash does, once the tombstone ages past its own
    # --older-than-days window. An engine that cannot answer the question
    # (tombstone_protected_count is None) says so honestly instead of
    # printing a confident zero.
    if tombstone_protected_count is None:
        click.echo("  Protected by pending tombstones: unavailable on this engine")
    else:
        click.echo(
            f"  Protected by pending tombstones: {tombstone_protected_count} "
            f"chunk(s) (reclaimed by 'nx catalog purge-trash' once past its "
            f"--older-than-days window, never by t3 gc)"
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

    # nexus-v1zdu (audit residual #1, HIGH): catalog_documents_for_collection
    # does NOT distinguish "collection known, zero documents" from
    # "collection unknown" -- an unregistered/typo'd COLLECTION reads as
    # zero documents, which reads as "nothing to protect" for the
    # manifest-less-note (RDR-145) and RUNFENCE (nexus-g6k6b) guards above,
    # exactly backwards for this operator-driven --yes path. Checked HERE
    # (last line of defence before deletion, same placement as nexus-jqrtp
    # below) rather than unconditionally early, so a --dry-run / report-only
    # invocation (which never deletes anything) is not refused over a
    # collection that merely lacks a `collections` registry row -- the same
    # un-backfilled-but-real state nexus-jqrtp's own guard already tolerates
    # via --allow-empty-manifest-set, which is why this check shares that
    # SAME override rather than minting a second flag for the same risk
    # category (an unverifiable/empty catalog state proceeding to delete).
    # INDEPENDENT of the nexus-jqrtp guard's own condition otherwise: this
    # one fires off `collection_documents`, not `referenced`, so a divergent
    # or stale referenced-chash read that happens to be non-empty does not
    # mask an unknown collection here.
    if not allow_empty_manifest_set:
        from nexus.catalog.membership import refuse_if_collection_unknown  # noqa: PLC0415 — command-local import (nexus.catalog.membership)

        refuse_if_collection_unknown(cat, collection, collection_documents)
    elif not collection_documents:
        # The override is the operator asserting "this collection really is
        # fully orphaned", which is also the only state in which an unknown
        # name is a legitimate gc target. Said out loud, because with the
        # override nothing else stands between this name and deletion
        # (review of 6129b9d35).
        from nexus.catalog.membership import collection_is_known  # noqa: PLC0415 — command-local import (nexus.catalog.membership)

        if not collection_is_known(cat, collection):
            click.echo(
                f"WARNING: the catalog does not know a collection named "
                f"'{collection}'. --allow-empty-manifest-set overrides that "
                f"refusal: every chunk T3 holds under this exact name is an "
                f"orphan candidate. Check the name against `nx collection list`."
            )

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
    # AUDIT TRAIL (nexus-fduai; closes the nexus-i711w item-20 window, Hal
    # ruling 2026-07-30): the local ChunkOrphaned EventLog died with the
    # local catalog. The engine's gc_audit table that replaced it is fed
    # server-side by the background reaps (sweepChunks / purge_trash /
    # gc_quarantine_orphans, actor="engine"), but the delete THIS verb
    # performs happens client-side, so the engine cannot see it — which is
    # exactly why it ships a client-facing producer, POST /gc_audit/record
    # (nexus-jqvzk, "the rows recordGcAudit writes for nx t3 gc"). This
    # verb reports its delete there in the same breath, then mirrors it as
    # the structured ``t3_gc_chunks_deleted`` event. A dry run records
    # nothing (report-only runs would swamp the trail); an audit write that
    # fails after the delete succeeded is WARNED and fails the exit code —
    # a delete with no forensic row is the blind spot nexus-sybbh named.
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
        audit_id: int | None = None
        audit_error: str | None = None
        writer = None
        try:
            writer = _make_catalog_writer()
            audit_id = writer.record_gc_audit(
                operation="t3_gc",
                collection=collection,
                actor="nx t3 gc",
                dry_run=False,
                chashes=[chash for _chunk_id, chash in candidates],
                details={
                    "deleted": deleted_total,
                    "requested": len(pending_chunk_ids),
                    "chunk_ids_sample": pending_chunk_ids[:_GC_AUDIT_ID_SAMPLE],
                    "chunk_ids_truncated": max(
                        len(pending_chunk_ids) - _GC_AUDIT_ID_SAMPLE, 0,
                    ),
                    # nexus-zewg3: how many OTHER chunks in this collection
                    # (not part of this run's deletes) are held alive only
                    # by a pending tombstone, not by any live document.
                    # None (never 0) when the engine could not answer.
                    "tombstone_protected": tombstone_protected_count,
                },
            )
        except Exception as exc:  # noqa: BLE001 — the delete already happened; surface the missing audit row loudly, never a traceback
            audit_error = f"{type(exc).__name__}: {exc}"
        # Closing the proxy is housekeeping: a close() that raises after the
        # row was written must not report the row as unwritten.
        close = getattr(writer, "close", None)
        if callable(close):
            with contextlib.suppress(Exception):
                close()
        _log.info(
            "t3_gc_chunks_deleted",
            collection=collection,
            deleted=deleted_total,
            requested=len(pending_chunk_ids),
            chunk_ids_sample=pending_chunk_ids[:_GC_AUDIT_ID_SAMPLE],
            chunk_ids_truncated=max(len(pending_chunk_ids) - _GC_AUDIT_ID_SAMPLE, 0),
            tombstone_protected=tombstone_protected_count,
            gc_audit_id=audit_id,
            gc_audit_error=audit_error,
        )
        if audit_error is not None:
            click.echo(
                f"  WARNING: the {deleted_total} chunk(s) above were deleted "
                f"but the gc_audit row recording it could NOT be written "
                f"({audit_error}) — the deletion has no forensic record in "
                f"nexus.gc_audit. Keep this output; see the "
                f"t3_gc_chunks_deleted log event for the ids.",
                err=True,
            )
            raise click.exceptions.Exit(1)


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
        f"$NEXUS_BACKFILL_STATE_FILE (default: {_BACKFILL_STATE_DEFAULT_DOC}). "
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
    total_reverse_discovered = 0
    total_cross_collection_forward_owner_skipped = 0
    total_reverse_multi_piece_skipped = 0
    total_chunk_count_mismatch_skipped = 0
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
        # nexus-wbfpw.7: reverse-note discoveries -- reported separately
        # from forward discovery, never folded into docs_processed alone.
        reverse_part = (
            f" ({result.docs_reverse_discovered} via reverse notes-guard)"
            if result.docs_reverse_discovered
            else ""
        )
        # nexus-wbfpw.7 fix-round-1: cross-collection forward-owner and
        # multi-piece reverse skips -- never silent, same discipline as
        # every other skip class above.
        cross_collection_part = (
            f" ({result.docs_cross_collection_forward_owner_skipped} skipped: "
            f"cross_collection_forward_owner)"
            if result.docs_cross_collection_forward_owner_skipped
            else ""
        )
        reverse_multi_piece_part = (
            f" ({result.docs_reverse_multi_piece_skipped} skipped: "
            f"reverse_multi_piece)"
            if result.docs_reverse_multi_piece_skipped
            else ""
        )
        # nexus-wbfpw.41: forward-path documents whose matched chunks cannot
        # be one manifest (duplicate positions, or a count that differs from
        # the registered chunk_count under --only-gapped) -- never silent.
        chunk_count_mismatch_part = (
            f" ({result.docs_skipped_chunk_count_mismatch} skipped: "
            f"chunk_count_mismatch)"
            if result.docs_skipped_chunk_count_mismatch
            else ""
        )
        print(
            f"[{idx}/{total}] {coll_name}: processed {result.docs_processed} "
            f"doc(s), {verb} {result.chunks_would_write if dry_run else result.chunks_written} chunk manifest row(s)"
            f"{skipped_part}{zero_chunks_part}{phase3_no_index_part}"
            f"{has_manifest_part}{chash_divergent_part}{fk_409_part}{reverse_part}"
            f"{cross_collection_part}{reverse_multi_piece_part}{chunk_count_mismatch_part}",
            file=sys.stderr,
        )

        # Emit to stdout as well for the summary output.
        click.echo(
            f"  {coll_name}: processed {result.docs_processed} doc(s), "
            f"{verb} {result.chunks_would_write if dry_run else result.chunks_written} chunk manifest row(s)"
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
            + (
                f" ({result.docs_reverse_discovered} via reverse notes-guard)"
                if result.docs_reverse_discovered
                else ""
            )
            + (
                f" ({result.docs_cross_collection_forward_owner_skipped} skipped: "
                f"cross-collection forward owner)"
                if result.docs_cross_collection_forward_owner_skipped
                else ""
            )
            + (
                f" ({result.docs_reverse_multi_piece_skipped} skipped: "
                f"reverse multi-piece note)"
                if result.docs_reverse_multi_piece_skipped
                else ""
            )
            + (
                f" ({result.docs_skipped_chunk_count_mismatch} skipped: "
                f"matched chunks disagree with the document's chunk count)"
                if result.docs_skipped_chunk_count_mismatch
                else ""
            )
        )

        total_docs += result.docs_processed
        total_chunks += result.chunks_would_write if dry_run else result.chunks_written
        total_skipped_no_t3 += result.docs_skipped_no_t3
        total_skipped_zero_chunks += result.docs_skipped_zero_chunks
        total_skipped_phase3_no_index += result.docs_skipped_phase3_no_index
        total_skipped_has_manifest += result.docs_skipped_has_manifest
        total_skipped_chash_divergent += result.docs_skipped_chash_divergent
        total_skipped_fk_409 += result.docs_skipped_fk_409
        total_reverse_discovered += result.docs_reverse_discovered
        total_cross_collection_forward_owner_skipped += (
            result.docs_cross_collection_forward_owner_skipped
        )
        total_reverse_multi_piece_skipped += result.docs_reverse_multi_piece_skipped
        total_chunk_count_mismatch_skipped += result.docs_skipped_chunk_count_mismatch
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
            # nexus-wbfpw.7 fix-round-1: cross-collection forward-owner and
            # reverse multi-piece skips are unhealed gaps of the same kind
            # as the three below -- a future fix (widened discovery, a
            # re-put that re-splits the note) can make them recoverable, so
            # a collection whose only gaps are these must also stay
            # revisitable by --resume, not be marked done.
            residual = (
                result.docs_skipped_fk_409
                + result.docs_skipped_chash_divergent
                + result.docs_skipped_zero_chunks
                + result.docs_cross_collection_forward_owner_skipped
                + result.docs_reverse_multi_piece_skipped
                + result.docs_skipped_chunk_count_mismatch
            )
            if residual > 0:
                state[coll_name] = [
                    "__partial__",
                    f"fk_409={result.docs_skipped_fk_409}",
                    f"chash_divergent={result.docs_skipped_chash_divergent}",
                    f"zero_chunks={result.docs_skipped_zero_chunks}",
                    f"cross_collection_forward_owner={result.docs_cross_collection_forward_owner_skipped}",
                    f"reverse_multi_piece={result.docs_reverse_multi_piece_skipped}",
                    f"chunk_count_mismatch={result.docs_skipped_chunk_count_mismatch}",
                ]
                click.echo(
                    f"  {coll_name}: NOT marked done -- {residual} doc(s) "
                    f"skipped (fk_409={result.docs_skipped_fk_409}, "
                    f"chash_divergent={result.docs_skipped_chash_divergent}, "
                    f"zero_chunks={result.docs_skipped_zero_chunks}, "
                    f"cross_collection_forward_owner="
                    f"{result.docs_cross_collection_forward_owner_skipped}, "
                    f"reverse_multi_piece={result.docs_reverse_multi_piece_skipped}, "
                    f"chunk_count_mismatch={result.docs_skipped_chunk_count_mismatch}); "
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
    # nexus-wbfpw.7: reverse notes-guard discovery total -- reported
    # separately from forward discovery in every summary, dry-run included.
    reverse_discovered_part = (
        f", {total_reverse_discovered} doc(s) manifested via reverse notes-guard"
        if total_reverse_discovered
        else ""
    )
    # nexus-wbfpw.7 fix-round-1: cross-collection forward-owner and reverse
    # multi-piece skip totals -- never silent, same discipline as every
    # other skip class above.
    cross_collection_forward_owner_part = (
        f", {total_cross_collection_forward_owner_skipped} chash(es) skipped "
        f"(cross-collection forward owner)"
        if total_cross_collection_forward_owner_skipped
        else ""
    )
    reverse_multi_piece_part = (
        f", {total_reverse_multi_piece_skipped} note(s) skipped "
        f"(reverse multi-piece)"
        if total_reverse_multi_piece_skipped
        else ""
    )
    chunk_count_mismatch_part = (
        f", {total_chunk_count_mismatch_skipped} doc(s) skipped "
        f"(chunk count mismatch)"
        if total_chunk_count_mismatch_skipped
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
        + reverse_discovered_part
        + cross_collection_forward_owner_part
        + reverse_multi_piece_part
        + chunk_count_mismatch_part
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
        service-side rate limits, not local CPU.
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
            res = reidentify_collection(
                t3_db, coll_name, dry_run=dry_run, known_to_exist=not collection,
            )
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


# ── RDR-192 Step 2, client half (bead nexus-wbfpw.5) ────────────────────────
#
# `nx t3 census-manifest-less` wraps the engine's read-only
# `POST /v1/vectors/manifest-less-census` route (bead nexus-wbfpw.4,
# `HttpVectorClient.manifest_less_census`). The route is first carried by
# engine-service-v0.1.133; against an older engine the verb exits 4 and the
# same census runs as direct SQL, `scripts/sql/manifest_less_census.sql`.

#: Bucket names the manifest-less-census route returns (RDR-192 S2, bead
#: nexus-wbfpw.4's response contract -- see that route's docstring and
#: `scripts/sql/manifest_less_census.sql`'s header for the full
#: definitions). The bead that requested this verb predates the route and
#: names the same five buckets; no reconciliation was needed. Order here
#: drives both text rendering and `--require-zero` validation.
_CENSUS_BUCKETS = (
    "superseded", "legacy-unmanifested", "dead-owner", "no-owner", "unclassified",
)

#: Page size `_census_one_collection` requests per call (AGENTS.md paging
#: convention, N <= 300; the engine clamps to this anyway --
#: `VectorHandler.MAX_CENSUS_LIMIT`). A module-level constant so a test can
#: force multi-page pagination without seeding 300+ real rows.
_CENSUS_PAGE_LIMIT = 300

#: Distinct exit code for "the connected engine predates the
#: manifest-less-census route" (RDR-192 S2, bead nexus-wbfpw.5's
#: EXECUTION note): a 404 here means the CONNECTED engine's build is
#: older than the one that shipped bead nexus-wbfpw.4's route -- an
#: EXPECTED, never-a-traceback outcome. This is a MECHANISM, not a dated
#: snapshot of which tag carries the route (review round 1 Significant-3
#: finding): which engine tags carry it changes over time (e.g.
#: engine-service-v0.1.133), so callers should check the connected
#: engine's own version / `REQUIRED_ENGINE_VERSION`
#: (`src/nexus/engine_version.py`), never a frozen "no tag carries it
#: yet" claim. Distinct from exit 1 (unclassified > 0), 2 (--require-zero
#: violated), 3 (--all found no collection), 5 (a real engine error).
_EXIT_NO_ROUTE = 4

#: Distinct exit code for a real engine error that is NOT "predates the
#: route" (review round 1 Important-1/Important-2 findings): a
#: `VectorServiceError` with any code other than 404 -- an explicitly
#: named `quarantine-*` --collection's 400
#: (`VectorHandler.requireNotQuarantineCollection`), a transient 5xx, or
#: a failed --all collection listing (`list_collections(strict=True)`).
#: Printed as one clear line naming the collection/listing and the
#: error, on stderr, never a traceback -- matching the rest of t3.py's
#: "except Exception: click.echo(..., err=True); exit non-zero"
#: convention. In --all, collections already censused before the
#: failure are still rendered (text, or a still-parseable --json document
#: carrying a "census_error" key) before this exit fires.
_EXIT_ENGINE_ERROR = 5

#: Human-readable message for the exit-4 (no-route) case. A module
#: constant so the wording lives in exactly one place (the runtime
#: message and the help text below both name the mechanism, not a date).
_NO_ROUTE_MESSAGE = (
    "This engine does not carry the manifest-less-census route "
    "(RDR-192 S2, bead nexus-wbfpw.4) -- the connected engine predates "
    "that route. Upgrade to an engine tag that carries it (compare the "
    "deployed engine's own version against REQUIRED_ENGINE_VERSION in "
    "src/nexus/engine_version.py)."
)


def _census_one_collection(client, collection: str) -> dict:
    """Page through :meth:`HttpVectorClient.manifest_less_census` for one
    collection, merging every page's ``chashes``/``owners`` (RDR-192 S2,
    bead nexus-wbfpw.5). ``totals``/``scope_chunk_total`` are read off the
    FIRST page only -- the route reports them collection-wide and
    identical on every page (see that method's docstring), so re-reading
    them per page would be redundant, never a correction.

    Raises whatever :meth:`~HttpVectorClient.manifest_less_census` raises
    (notably :class:`~nexus.db.http_vector_client.VectorServiceError`,
    ``code=404`` on a pre-route engine) -- the caller decides how to
    surface that.
    """
    offset = 0
    chashes: dict[str, list[str]] = {bucket: [] for bucket in _CENSUS_BUCKETS}
    owners: dict[str, dict] = {}
    totals: dict[str, int] = {}
    scope_chunk_total = 0
    first_page = True
    while True:
        page = client.manifest_less_census(
            collection, limit=_CENSUS_PAGE_LIMIT, offset=offset,
        )
        if first_page:
            totals = dict(page.get("totals") or {})
            scope_chunk_total = int(page.get("scope_chunk_total", 0))
            first_page = False
        for bucket, page_chashes in (page.get("chashes") or {}).items():
            chashes.setdefault(bucket, []).extend(page_chashes)
        owners.update(page.get("owners") or {})
        returned = int(page.get("returned", 0))
        if returned < _CENSUS_PAGE_LIMIT:
            break
        offset += _CENSUS_PAGE_LIMIT
    return {
        "collection": collection,
        "chashes": chashes,
        "owners": owners,
        "totals": totals,
        "scope_chunk_total": scope_chunk_total,
    }


def _render_census_text(result: dict) -> None:
    """Print one collection's census in text form, naming each item's
    owner tumbler and path (forward, reverse, or none) so an operator can
    see which document keeps a chunk live and whether the reverse
    tie-break chose it (nexus-wbfpw.5 acceptance criteria)."""
    click.echo(f"{result['collection']}:")
    owners = result["owners"]
    for bucket in _CENSUS_BUCKETS:
        bucket_chashes = result["chashes"].get(bucket, [])
        click.echo(f"  {bucket}: {result['totals'].get(bucket, 0)}")
        for chash in sorted(bucket_chashes):
            owner = owners.get(chash) or {}
            tumbler = owner.get("owner_tumbler") or "-"
            path = owner.get("owner_path") or "none"
            click.echo(f"    {chash}  owner={tumbler} ({path})")
    click.echo(f"  total: {result['scope_chunk_total']}")


def _finish_census(
    *,
    as_json: bool,
    results: list[dict],
    collections_discovered: int | None,
    census_error: dict[str, str | None] | None,
    require_zero: tuple[str, ...],
    exit_code: int | None = None,
) -> NoReturn:
    """Render the census result exactly once -- text, or one JSON
    document -- and exit. Called on EVERY exit path (review round 2
    CRITICAL: under --json, stdout must carry exactly one parseable
    document regardless of exit code; round 1's fix only covered the
    mid-loop-failure flavor of exit 5, leaving exit 3, exit 4, and the
    listing-failure flavor of exit 5 emitting a plain sentence to stdout,
    nothing at all, or nothing at all respectively).

    ``unclassified``/``require_zero_violations`` are always computed over
    whatever collections DID succeed, even when ``census_error`` is also
    set (review round 2 Significant): --require-zero and unclassified are
    gate conditions computed from the collections that succeeded;
    ``census_error`` reports that the census itself is incomplete. Both
    facts belong in the document together, so a violation already
    observed is never hidden behind a later 4/5.

    Exit-code precedence (mirrored in the command's own help text and
    docs/cli-reference.md): ``census_error`` (4 no-route, 5 any other
    engine error) always wins over 1 (unclassified) and 2
    (--require-zero) -- an incomplete census cannot pass a gate. Between
    4 and 5: 4 is a whole-engine condition (the CONNECTED ENGINE predates
    the route) and is checked first, on the very first collection
    attempted, before any per-collection 5 could fire. Exit 3 is passed
    explicitly by the caller -- it is not a violation (the --all listing
    SUCCEEDED and is genuinely empty), so it is never derived from
    unclassified/require_zero/census_error, which would otherwise
    trivially compute to a clean 0 over zero results.
    """
    any_unclassified = any(
        result["totals"].get("unclassified", 0) > 0 for result in results
    )
    zero_violations = [
        bucket for bucket in require_zero
        if sum(result["totals"].get(bucket, 0) for result in results) > 0
    ]
    if exit_code is None:
        if census_error is not None:
            exit_code = (
                _EXIT_NO_ROUTE if census_error["kind"] == "no_route"
                else _EXIT_ENGINE_ERROR
            )
        elif any_unclassified:
            exit_code = 1
        elif zero_violations:
            exit_code = 2
        else:
            exit_code = 0

    if as_json:
        click.echo(json.dumps({
            "collections": results,
            "collections_discovered": collections_discovered,
            "collections_censused": len(results),
            "census_error": census_error,
            "unclassified": any_unclassified,
            "require_zero_violations": zero_violations,
            "exit_code": exit_code,
        }, indent=2))
    else:
        for result in results:
            _render_census_text(result)

    if zero_violations:
        # Always stderr (review round 1 CRITICAL finding): this line used
        # to go to the SAME stdout stream as the --json payload above,
        # corrupting it for the one combination (--json + a violated
        # --require-zero) the exit-code contract exists to support.
        click.echo(f"--require-zero violated: {', '.join(zero_violations)}", err=True)

    sys.exit(exit_code)


@t3.command("census-manifest-less")
@click.option(
    "--collection", "-c", default=None,
    help="Collection to census. Exactly one of --collection/--all is required.",
)
@click.option(
    "--all", "all_collections", is_flag=True, default=False,
    help="Census every T3 collection except quarantine-* ones (live(c) "
    "applies to every collection, not only knowledge__*).",
)
@click.option(
    "--json", "as_json", is_flag=True, default=False,
    help="Emit JSON instead of text.",
)
@click.option(
    "--require-zero", "require_zero", multiple=True, metavar="BUCKET",
    help="Bucket that must be zero across every censused collection "
    f"(one of {', '.join(_CENSUS_BUCKETS)}); repeatable. Exit 2 if any "
    "named bucket's total is above zero.",
)
def census_manifest_less_cmd(
    collection: str | None,
    all_collections: bool,
    as_json: bool,
    require_zero: tuple[str, ...],
) -> None:
    """Census manifest-less T3 chunks via the engine's read-only census
    route (RDR-192 Step 2 MVV (a), bead nexus-wbfpw.5).

    \b
    Classifies every chunk carrying no own-collection manifest row into
    one of five buckets -- superseded, legacy-unmanifested, dead-owner,
    no-owner, unclassified -- and prints, per collection, the count in
    each bucket, the owning document (tumbler + how it was found: forward,
    reverse, or none) for each item, and a total. See
    ``scripts/sql/manifest_less_census.sql``'s header (the SAME text the
    engine route runs) for the full bucket definitions and the
    forward/reverse precedence rule.

    \b
    Exit codes:
      0  clean.
      1  unclassified > 0 -- a census that cannot classify a row has failed.
      2  --require-zero names a bucket whose count is above zero.
      3  --all finds no collection (excluding quarantine-*) -- the listing
         itself SUCCEEDED and is genuinely empty; a failed listing is exit 5.
      4  the connected engine predates the manifest-less-census route --
         upgrade the engine (compare its version against
         REQUIRED_ENGINE_VERSION in src/nexus/engine_version.py).
      5  a real engine error other than "predates the route": a
         quarantine-* --collection's 400, a transient 5xx, or a failed
         --all collection listing. In --all, collections already censused
         before the failure are still printed (or, under --json, still
         emitted as a parseable document naming the failed collection).

    \b
    Exit-code precedence: 4 and 5 (the census is INCOMPLETE) always win
    over 1 and 2 (a gate condition computed from what WAS censused) -- an
    incomplete census cannot pass a gate. Between 4 and 5: 4 is a
    whole-engine condition (the connected engine predates the route
    entirely) and is checked first, on the very first collection
    attempted, before a later per-collection 5 could ever fire. This
    never hides a finding: unclassified/--require-zero are always
    computed over whatever collections DID succeed, even when 4/5 also
    fires, and both are always present in the output (text and --json)
    alongside the incomplete-census report.

    \b
    Human-readable diagnostics (the --require-zero violation notice, an
    engine-error line) always go to stderr, never stdout -- with --json,
    stdout carries exactly one JSON document on every exit path listed
    above, parseable regardless of exit code. The document carries
    "collections" (per-collection results, possibly partial),
    "collections_discovered" (how many collections were found to census;
    null when discovery itself failed), "collections_censused" (how many
    actually completed -- compare against "collections_discovered" to
    tell "stopped after 1 of 50" from "stopped after 1 of 2"),
    "census_error" (null, or {"collection", "error", "kind"} naming which
    collection failed and why), "unclassified" (bool) and
    "require_zero_violations" (list of bucket names), and "exit_code".

    \b
    Needs engine-service-v0.1.133 or later; against an older engine it
    exits 4, and the same census runs as direct SQL
    (``scripts/sql/manifest_less_census.sql``).
    """
    if bool(collection) == bool(all_collections):
        raise click.UsageError(
            "Specify exactly one of --collection NAME or --all."
        )
    for bucket in require_zero:
        if bucket not in _CENSUS_BUCKETS:
            raise click.BadParameter(
                f"unknown bucket {bucket!r}; must be one of "
                f"{', '.join(_CENSUS_BUCKETS)}",
                param_hint="--require-zero",
            )

    from nexus.db import make_t3  # noqa: PLC0415 — command-local import deferred to avoid CLI startup cost (nexus.db)
    from nexus.db.http_vector_client import VectorServiceError  # noqa: PLC0415 — command-local import deferred to avoid CLI startup cost (nexus.db.http_vector_client)

    t3_db = make_t3()

    if all_collections:
        # RDR-204: exclude quarantine siblings via the catalog-authoritative
        # lifecycle_state field, never a raw "quarantine-" name-prefix parse
        # (tests/test_collection_name_parse_census.py's parse-site census
        # guards against exactly that regression). Absent means the engine
        # could not join a catalog row for this collection -- unregistered,
        # not quarantine, so it stays IN, matching
        # http_vector_client.is_live_collection_row's own "absent means
        # included" reading.
        try:
            # strict=True (nexus-wbfpw.5 review round 1, Significant-2):
            # list_collections's default swallows a non-404 failure and
            # returns [] -- the shared contract every OTHER caller relies
            # on, which this verb must not change. strict=True re-raises
            # instead, so exit 3 below can mean "listed zero collections",
            # never "the listing itself failed".
            names = [
                c["name"] for c in t3_db.list_collections(strict=True)
                if c.get("lifecycle_state") != "quarantine"
            ]
        except VectorServiceError as exc:
            click.echo(f"Failed to list T3 collections: {exc}", err=True)
            # Review round 2 CRITICAL: this used to sys.exit before the
            # JSON render block was ever reached, leaving stdout empty
            # under --json. collections_discovered is None -- the listing
            # itself failed, so there is no count to report.
            _finish_census(
                as_json=as_json, results=[], collections_discovered=None,
                census_error={
                    "collection": None, "error": str(exc), "kind": "listing_failed",
                },
                require_zero=require_zero,
            )
        if not names:
            # Review round 2 CRITICAL: this used to print a plain sentence
            # to stdout with no err=True, which is both a diagnostic-on-
            # stdout regression and, under --json, corrupts the "stdout is
            # always one parseable document" contract. Exit 3 is not a
            # violation (the listing SUCCEEDED and is genuinely empty), so
            # exit_code is passed explicitly rather than derived.
            click.echo(
                "No T3 collections found (excluding quarantine-*); nothing to census.",
                err=True,
            )
            _finish_census(
                as_json=as_json, results=[], collections_discovered=0,
                census_error=None, require_zero=require_zero, exit_code=3,
            )
    else:
        names = [collection]

    collections_discovered = len(names)

    # nexus.db.make_t3() with no injected _client (every production call,
    # local and cloud alike) returns the HttpVectorClient itself, not a
    # T3Database facade -- the facade wrap is test-injection-only (see
    # that function's docstring). manifest_less_census lives directly on
    # HttpVectorClient, so t3_db already IS the right object to call it on.
    client = t3_db

    results: list[dict] = []
    census_error: dict[str, str | None] | None = None
    for name in names:
        try:
            results.append(_census_one_collection(client, name))
        except VectorServiceError as exc:
            if exc.code == 404:
                click.echo(_NO_ROUTE_MESSAGE, err=True)
                census_error = {"collection": name, "error": str(exc), "kind": "no_route"}
            else:
                # Any other engine error (a quarantine-* collection's 400,
                # a transient 5xx, ...) -- review round 1 Important-1: this
                # used to fall through to a bare `raise` and surface as a
                # raw traceback. Stop censusing further collections but
                # keep what already succeeded (review round 1's --all
                # decision).
                click.echo(f"Census failed on collection {name!r}: {exc}", err=True)
                census_error = {"collection": name, "error": str(exc), "kind": "engine_error"}
            break

    _finish_census(
        as_json=as_json, results=results, collections_discovered=collections_discovered,
        census_error=census_error, require_zero=require_zero,
    )
