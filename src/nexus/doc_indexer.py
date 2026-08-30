# SPDX-License-Identifier: AGPL-3.0-or-later
"""Document indexing pipeline: PDF and Markdown → T3 collections.

By default documents are stored in ``docs__`` collections.  Callers can
override the collection name for other prefixes (e.g. ``rdr__``).
"""
from __future__ import annotations

import hashlib

from nexus.chunk_identity import chunk_id_from_hash as _chunk_id_from_hash
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import structlog

if TYPE_CHECKING:
    from nexus.hook_registry import HookRegistry

_log = structlog.get_logger(__name__)

from nexus.checkpoint import (
    CHECKPOINT_DIR,
    CheckpointData,
    delete_checkpoint,
    read_checkpoint,
    write_checkpoint,
)
from nexus.corpus import index_model_for_collection
from nexus.db import make_t3
from nexus.retry import _vector_with_retry
from nexus.md_chunker import SemanticMarkdownChunker, parse_frontmatter
from nexus.pdf_chunker import PDFChunker
from nexus.pdf_extractor import PDFExtractor

# Type alias for the chunking callback used by _index_document.
# Receives (file_path, content_hash, target_model, now_iso, corpus) and returns
# a list of (chunk_id, document_text, metadata_dict) tuples, or an empty list.
ChunkFn = Callable[[Path, str, str, str, str], list[tuple[str, str, dict]]]

# Type alias for a local (dry-run) embedding function override.
# Receives (texts, model) and returns (embeddings, actual_model).
EmbedFn = Callable[[list[str], str], tuple[list[list[float]], str]]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def _lookup_existing_doc_id(
    cat: "CatalogReader | None", file_path: str, corpus: str,
) -> str:
    """Pre-flight catalog lookup for an already-indexed file (nexus-dcym).

    Returns the catalog ``doc_id`` if a registration exists under the
    corpus's owner; empty string otherwise (catalog absent, owner
    missing, or first-time registration). Best-effort: any failure
    silently returns "" so callers fall back to the legacy
    ``source_path``-keyed chunk lookup.

    Callers construct *cat* once and pass it in (RDR-120 DI substrate);
    ``None`` indicates the catalog has not been initialised at this
    location.
    """
    if cat is None:
        return ""
    try:
        from nexus.catalog.tumbler import Tumbler  # noqa: PLC0415 — circular-dep avoidance (nexus.catalog.tumbler)

        owner_name = corpus or "standalone-pdfs"
        # Curator-only lookup — see _register_or_lookup_doc_id for
        # rationale (repo and curator owners can share names; lookups
        # from the doc_indexer family must use the curator namespace).
        # nexus-qnp5s: curator_owner_tumbler_by_name() is implemented on
        # both SQLite Catalog and HttpCatalogClient — no raw _db access.
        owner = cat.curator_owner_tumbler_by_name(owner_name)
        if owner is None:
            return ""
        existing = cat.by_file_path(owner, file_path)
        if existing is None:
            return ""
        return str(existing.tumbler)
    except Exception:  # noqa: BLE001 — boundary catch; degrade to safe default on error
        return ""


#: nexus-y8qtj: fraction of a freshly-indexed document's OWN manifest
#: chashes that must already appear in another LIVE document before
#: ``_check_document_fork`` warns of a possible catalog-identity fork (two
#: Document rows for what is really one physical source — the failure mode
#: behind the Zoology defect, where a path-based re-index missed an
#: out-of-band source_uri and forked a second Document while leaving the
#: original's corrupted chunks live and searchable). 0.5 is a deliberately
#: conservative MAJORITY-overlap bar: legitimate near-duplicates (a preprint
#: vs. its camera-ready revision, two translations of the same paper) can
#: share a meaningful minority of chunks without being the same catalog
#: identity, and a bar much lower than half would make the warning noisy
#: enough to be ignored. This is a WARNING, not a refusal — unlike
#: --source-uri's fail-loud rules, near-duplicate content is a legitimate
#: ingest outcome; the operator decides whether to investigate or supersede.
_DOCUMENT_FORK_WARN_FRACTION = 0.25


def _check_document_fork(doc_id: str, collection_name: str) -> list[tuple[str, int]]:
    """Best-effort post-index fork check (nexus-y8qtj).

    Fetches *doc_id*'s own manifest chashes and does ONE batched
    ``docs_for_chashes`` round-trip to see whether another LIVE document
    already carries most of the same content — the signature of the
    y8qtj defect (a path-based re-index minting a second Document for a
    source originally registered under a different identity, e.g.
    DEVONthink's ``x-devonthink-item://<UUID>``). Logs a structlog
    WARNING (``index_possible_document_fork``) for every other document
    strictly above :data:`_DOCUMENT_FORK_WARN_FRACTION` overlap, and returns
    the same findings as ``[(other_doc_id, shared_chunk_count), ...]``
    sorted by shared count descending, so callers (the CLI) can fold a
    count into the run summary without a second round-trip.

    Self-contained (opens/closes its own catalog reader) so it can be
    called from every terminal-return site in ``index_pdf`` /
    ``index_markdown`` without threading a reader through. Best-effort:
    any failure here must never abort or affect the indexing result that
    already committed — chunks are already written by the time this runs.
    """
    if not doc_id:
        return []
    reader = None
    try:
        from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — circular-dep avoidance (nexus.catalog.factory)

        reader = make_catalog_reader()
        manifest = reader.get_manifest(doc_id)
        chashes = sorted({row.chash for row in manifest if getattr(row, "chash", "")})
        if not chashes:
            return []
        by_chash = reader.docs_for_chashes(chashes) or {}
    except Exception:  # noqa: BLE001 — best-effort observability; must not affect the committed index result
        _log.debug("document_fork_check_failed", doc_id=doc_id, exc_info=True)
        return []
    finally:
        if reader is not None:
            reader.close()  # nexus-qnp5s: HttpCatalogClient.close() is safe

    shared_counts: dict[str, int] = {}
    for owners in by_chash.values():
        for other in owners or []:
            if other and other != doc_id:
                shared_counts[other] = shared_counts.get(other, 0) + 1

    total = len(chashes)
    forks = [
        (other, count) for other, count in shared_counts.items()
        if count / total > _DOCUMENT_FORK_WARN_FRACTION
    ]
    forks.sort(key=lambda pair: pair[1], reverse=True)
    for other, count in forks:
        _log.warning(
            "index_possible_document_fork",
            new=doc_id, existing=other, shared_chunks=count, total_chunks=total,
            collection=collection_name,
        )
    return forks


def _identity_where(file_path: str, corpus: str, *, content_hash: str = "") -> dict:
    """nexus-dcym: chunk-identity where-filter for incremental sync sites.

    RDR-108 Phase 3 retired ``doc_id`` from chunk metadata (catalog
    manifest is authoritative); ``content_hash`` is now the canonical
    staleness key. Pass ``content_hash`` for staleness checks — pruning
    by content_hash is incorrect because matching chunks are by
    definition not stale.

    nexus-tbkk1: the no-``content_hash`` (``source_path``) branch is DEAD
    for every stale-chunk *prune* purpose it was written for, in THIS
    module and pipeline_stages.py specifically. RDR-102 D2 (2026-05-02,
    commit 83ac62c7) hard-removed ``source_path`` from
    ``make_chunk_metadata`` — the sole factory every doc_indexer.py /
    pipeline_stages.py chunk-metadata build routes through — so no chunk
    WRITTEN since then carries this key, and
    ``{"source_path": file_path}`` matches zero rows for any such chunk.
    Empirically corroborated, not just inferred from the writer-side
    proof: a read-only ``field>=!`` boundary-value existence probe
    (validated against a present-field control before use) against six
    representative live collections — code__1-1 (32172 chunks, this
    repo, continuously reindexed), docs__1-1 (4666, this repo's own
    docs), knowledge__knowledge (1339), and three external-corpus
    collections indexed once and rarely re-touched (knowledge__dt-papers
    7253, knowledge__augur-oracle-papers 1602, knowledge__interpretability
    299) — found ZERO rows carrying a ``source_path`` key anywhere
    (nexus-tbkk1 fix round, 2026-08-05). This is a representative sample,
    not an exhaustive one; it does not prove every collection in the
    store is clean, only that the population is not the large, easily-
    found kind RDR-102 D2's "defense-in-depth" framing (rdr-102-phase4-
    completion.md:159) might suggest.

    RDR-102 §D2 named FOUR sites for this same dead-code class, deferred
    to "Phase 5b": ``doc_indexer.py:109`` (this function), plus THREE
    SIBLING sites in ``indexer.py``/``indexer_utils.py`` that this bead
    did NOT touch and does NOT claim closed —
    ``indexer.py``'s ``_prune_misclassified_in_collection`` legacy
    ``source_path`` fallback (serves ``nx index repo``'s code/prose
    paths) and ``indexer_utils.check_staleness``'s source_path fallback
    were unaudited by this bead and subsequently audited + deleted by
    nexus-afudo (2026-08-05) — Phase 5b is now fully closed.
    Only the doc_indexer.py/pipeline_stages.py HALF of Phase 5b closes
    here: the four prune call sites that used to take this branch in
    THOSE two files (``_index_document``, ``_index_pdf_incremental``,
    ``index_pdf``'s small-doc branch, and pipeline_stages.
    ``_prune_stale_chunks``) are deleted.

    What replaces the deleted prunes' automatic (fires-on-every-reindex)
    protection, honestly: ``mcp_infra._sweep_superseded_vectors``
    (manifest-diff based — compares a re-indexed document's OWN
    before/after manifest chash sets) covers the common case and is
    proven end-to-end at tests/integration/test_tp8yk_manifest_never_
    outruns_chunks.py::test_union_guard_keeps_shared_chunk_at_the_
    production_wiring — but it structurally CANNOT see a legacy row
    whose owning document's manifest doesn't reference it (no "before"
    set to diff against). The comprehensive backstop for THAT population
    is ``nx t3 gc`` (RDR-108 Phase 4, chash-vs-manifest orphan sweep,
    ``src/nexus/commands/t3.py:219``) — broader than source_path-keyed
    pruning (catches any orphan cause), but manual/operator-triggered
    with a 30-day orphan-window default, not automatic. No one-time
    ``nx t3 gc`` sweep was run as part of closing this bead (production
    mutation, Hal-gated); confirm one has happened since 2026-08-05, or
    schedule it, before treating this deletion's edge risk as resolved.

    The ONE surviving caller of this branch — ``index_pdf``'s streaming-
    path ``return_metadata=True`` metadata read (fetching page/title/
    author for the return value, not a prune) — was independently broken
    by the identical gap (it also always saw zero rows). nexus-w6wp0
    (2026-08-05) fixed that call site to pass ``content_hash`` like every
    other caller; as of that fix, the no-``content_hash`` branch below has
    ZERO surviving production callers in this codebase. It is kept rather
    than deleted because removing a public-shaped helper's dead branch is
    out of scope for a bug-fix bead — nexus-tbkk1's scope was the
    stale-chunk *prune* sites in doc_indexer.py/pipeline_stages.py only,
    and nexus-w6wp0's was this one read site.
    """
    if content_hash:
        return {"content_hash": content_hash}
    return {"source_path": file_path}


def _metadata_for_doc_id(col: Any, doc_id: str) -> list[dict]:
    """nexus-w6wp0: fetch *doc_id*'s own chunk metadatas via the catalog
    manifest (doc_id -> document_chunks -> chash), not a content-derived
    where-filter.

    The manifest is the per-document chunk binding post RDR-108/180: it
    scopes the read to exactly the chash rows THIS document's manifest
    currently claims, unlike a collection-wide content_hash-keyed query
    which can also pick up a disjoint or partially-overlapping decoy --
    e.g. a stale/orphaned leftover batch from an earlier superseded write
    that happens to share this content_hash but isn't part of this
    document's live manifest. Self-contained (opens/closes its own
    catalog reader), mirroring ``_check_document_fork``'s manifest fetch.

    NOT a fix for TRUE byte-identical duplicate documents (round-2 review
    correction, substantive-critic, 2026-08-05): identical content_hash
    implies identical per-chunk text, hence identical chash SETS, so two
    documents' manifests resolve to the SAME shared, content-deduplicated
    T3 rows either way (RDR-108's chunk-level dedup -- see t3.py's
    store_put docstring and test_within_collection_identical_chunks_
    collapse). Those shared rows carry whichever registration's title/
    source_author was written last (last-write-wins), regardless of which
    document's manifest scopes the read. Acceptable for a summary display
    (chunk content is correct either way; only title/author attribution
    on a genuine duplicate reflects the most recent indexer), not
    something this function disambiguates further.

    Deliberately NOT best-effort (unlike ``_check_document_fork``, whose
    fork signal is optional): this result feeds the ``return_metadata``
    dict callers depend on, so a genuine catalog-read failure propagates
    as-is rather than being swallowed to an empty list -- FAIL LOUD, never
    a silent empty standing in for a real error. An empty MANIFEST
    (doc_id resolves but has zero chash rows) is not itself an error and
    returns ``[]`` normally; the caller's own count>0-but-empty guard is
    what turns a genuinely inconsistent zero-metadata result into a loud
    failure.
    """
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — circular-dep avoidance (nexus.catalog.factory)

    reader = make_catalog_reader()
    try:
        manifest = reader.get_manifest(doc_id)
        chashes = sorted({row.chash for row in manifest if getattr(row, "chash", "")})
    finally:
        reader.close()  # nexus-qnp5s: HttpCatalogClient.close() is safe
    if not chashes:
        return []
    batch = _vector_with_retry(col.get, ids=chashes, include=["metadatas"])
    return batch.get("metadatas", [])


def _doc_id_for_path(file_path: Path) -> str:
    """Best-effort READ-ONLY document identity for a source path, or "".

    nexus-5xn3k AC2, prose path. The PDF gate has a doc_id in scope already
    (``_register_or_lookup_doc_id`` runs before it); ``_index_document`` does
    not. Resolving one must NOT register: this sits on the staleness path,
    which by definition runs for documents that may be untouched, and minting
    catalog rows as a side effect of *deciding whether to skip* would be a
    far worse bug than the one being fixed.

    ``by_source_uri`` is the read-only lookup — no owner resolution, no
    create. The catalog auto-derives ``file://<abspath>`` when a document is
    registered without an explicit URI (catalog register, RDR-096 P3.1), so
    that is the key tried here.

    Returns "" when the document cannot be identified, which
    ``_manifest_is_fully_present`` treats as "no evidence of damage" — the
    pre-fix behaviour. A miss is therefore SAFE, never a spurious re-embed.
    """
    try:
        from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred to avoid circular import

        cat = make_catalog_reader()
        if cat is None:
            return ""
        entry = cat.by_source_uri(f"file://{file_path.resolve()}")
        return str(entry.tumbler) if entry is not None else ""
    except Exception as exc:  # noqa: BLE001 — fail-open: identity is best-effort here
        _log.debug("index_doc_identity_lookup_failed", path=str(file_path), error=str(exc))
        return ""


def _manifest_is_fully_present(col: Any, doc_id: str) -> bool:  # noqa: ARG001 — col/doc_id kept for signature stability, see below
    """Always ``True`` — RDR-191 Phase 6 (nexus-o8dil.33), 2026-08-15.

    HISTORY: nexus-5xn3k AC2 (original), reworked for RUNFENCE (nexus-5xn3k.3,
    design memo §3.4) to call the engine's per-document ``manifest/verify``
    (a single SQL anti-join: referenced/present/missing chash counts for one
    document) instead of an O(manifest size / 300) client-side existence
    loop. That route (and the ``nexus.manifest_verify(text)`` call it made
    through :meth:`HttpCatalogClient.manifest_verify`) is retired in THIS
    bead — but not because the check became unreachable through disuse: the
    manifest-chunk FK (``catalog-029-manifest-chunk-fk.xml``, VALIDATEd and
    deployed) makes ``missing`` — the ONLY value this function ever branched
    on — PROVABLY ALWAYS 0 for any manifest row that exists at all. The FK
    guarantees every ``catalog_document_chunks`` row references a matching
    ``nexus.chunks`` row at write time (a dangling INSERT is rejected, a
    DELETE of a still-referenced chunk is rejected); ``manifest_verify``'s
    SQL computed exactly that same existence check via anti-join. So this
    function's ``missing`` branch (the ONLY path that ever returned
    ``False``) became dead code the moment the FK was validated on THIS
    checkout's engine (Phase 5, engine-service-v0.1.76) — independent of
    whether the route/client method survived Phase 6's subtraction. Removing
    the now-provably-vacuous call, rather than leaving it to raise
    ``AttributeError`` and silently fail open through the broad ``except``
    below, keeps this function honest about what it actually decides now.

    NOTE this is a DIFFERENT question from write-path COMPLETENESS (did
    every expected chunk for THIS run get a manifest row at all) — that
    remains a live, load-bearing check, just not this one:
    ``CatalogRepository.completeIndexRun``'s fail-closed verify-then-stamp
    (``referenced == the caller's claimed chunk_count``) still runs
    server-side on every index completion via the SAME underlying
    ``nexus.manifest_verify(text)`` SQL function, which is DELIBERATELY KEPT
    (not dropped) for exactly that reason. This function's remaining
    fallback role — deciding whether a document with no reliable RUNFENCE
    fence signal (a legacy pre-fence row, an unresolvable doc_id, or a fence
    read failure) is safe to skip re-indexing — is answered by the SAME FK
    guarantee: any manifest row visible here already passed completeIndexRun
    (or an older writer) at write time, so there is no evidence of damage to
    find. ``col``/``doc_id`` are kept unused in the signature (not renamed to
    ``_col``/``_doc_id``) so :func:`_index_run_fresh` and every existing
    caller/test need no further change.
    """
    return True


def _index_fence_state(doc_id: str) -> tuple[str | None, str]:
    """Read-only lookup of the RUNFENCE fields for *doc_id* (nexus-5xn3k.3,
    design memo §3.4). Returns ``(index_state, index_content_hash)``.

    ``index_state`` comes back ``None`` for an empty *doc_id*, an
    unresolvable entry, a legacy pre-fence row (NULL column, or a field
    absent entirely on a pre-fence engine), OR a read failure — every one of
    these collapses to the same "the fence has nothing to say" bucket that
    :func:`_index_run_fresh` falls through to :func:`_manifest_is_fully_present`
    for. A read failure is logged at WARNING, never DEBUG: a fence that
    silently stops working recreates the ac4id bug one layer up.
    """
    if not doc_id:
        return None, ""
    try:
        from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred to avoid circular import

        cat = make_catalog_reader()
        if cat is None:
            return None, ""
        entry = cat.by_doc_id(str(doc_id))
        if entry is None:
            return None, ""
        return entry.index_state, entry.index_content_hash
    except Exception as exc:  # noqa: BLE001 — fail-open, but LOUD (ac4id lesson)
        _log.warning("index_fence_read_failed", doc_id=str(doc_id), error=str(exc))
        return None, ""


def _index_run_fresh(col: Any, doc_id: str, content_hash: str) -> bool:
    """The staleness three-way (RUNFENCE, nexus-5xn3k.3, design memo §3.4).

    Call ONLY after the existing chunk-level match (content_hash +
    embedding_model against ONE surviving chunk, ``limit=1``) already holds —
    this layers the fence's document-level intent/completion record on top
    of that per-chunk signal, closing the AC2 blind spot the memo's §1
    describes (a consistent T3/manifest truncation that no comparison
    between two artifacts written by the SAME broken run can ever see).

    * ``index_state == 'complete'`` AND the fence's own
      ``index_content_hash`` agrees with *content_hash* -> definitely fresh,
      no probe.
    * ``'indexing'`` / ``'failed'`` -> definitely stale, no probe. A partial
      or errored run must never read as done, regardless of what T3 happens
      to hold right now (nexus-lcmbp non-goal: this is NOT a lock — it never
      means "someone else is running, skip").
    * anything else (``None`` — a NULL column, a field absent on a pre-fence
      engine, an unresolvable doc_id, or a fence read failure) -> the fence
      has nothing to say; fall through to :func:`_manifest_is_fully_present`,
      which is unconditionally ``True`` as of RDR-191 Phase 6 (see its own
      docstring for why the FK makes that provably correct now, not merely
      a simplification).
    """
    state, fence_hash = _index_fence_state(doc_id)
    if state == "complete":
        return fence_hash == content_hash
    if state in ("indexing", "failed"):
        return False
    return _manifest_is_fully_present(col, doc_id)


def _fence_begin(doc_id: str, content_hash: str, collection: str) -> None:
    """Advisory: stamp ``index_state='indexing'`` BEFORE the first chunk
    upsert (memo §3.5 T0, nexus-5xn3k.4). Never raises — the fence is a
    diagnostic record, not a lock (nexus-lcmbp non-goal); indexing must
    proceed even when the catalog write itself fails."""
    from nexus.catalog.factory import make_catalog_writer  # noqa: PLC0415 — deferred import; test patch target
    from uuid import uuid4  # noqa: PLC0415 — deferred import: branch-local

    w = None
    try:
        w = make_catalog_writer()
        w.begin_index_run(doc_id, content_hash, uuid4().hex, collection)
    except Exception:  # noqa: BLE001 — boundary catch: begin is an advisory write; indexing must proceed (memo §3.4 fail-open contract)
        _log.warning("index_run_begin_failed", doc_id=doc_id, collection=collection)
    finally:
        close = getattr(w, "close", None)
        if close is not None:
            close()


def _fence_begin_many(pairs: list[tuple[str, str]], collection: str) -> None:
    """Advisory: batch-stamp ``index_state='indexing'`` for every doc in one
    upload FLUSH, ONE round trip (nexus-vw594 F1) instead of paying
    :func:`_fence_begin`'s one-call-per-file cost across an entire
    ``ChunkBatcher`` flush. *pairs* is ``[(doc_id, content_hash), ...]``;
    callers are expected to have already dropped entries with an empty
    ``doc_id`` (no catalog handle for that file). A single ``run_id`` is
    shared across every entry so the whole flush correlates as one run in
    the logs — mirrors :func:`_fence_begin`'s per-call ``uuid4()``, just
    minted once instead of once per doc.

    Same fail-open contract as :func:`_fence_begin`: never raises —
    indexing must proceed even when the catalog write itself fails."""
    if not pairs:
        return
    from nexus.catalog.factory import make_catalog_writer  # noqa: PLC0415 — deferred import; test patch target
    from uuid import uuid4  # noqa: PLC0415 — deferred import: branch-local

    run_id = uuid4().hex
    w = None
    try:
        w = make_catalog_writer()
        w.begin_index_run_many(
            docs=[
                {"doc_id": doc_id, "content_hash": content_hash, "run_id": run_id}
                for doc_id, content_hash in pairs
            ],
            collection=collection,
        )
    except Exception:  # noqa: BLE001 — boundary catch: begin is an advisory write; indexing must proceed (memo §3.4 fail-open contract)
        _log.warning("index_run_begin_many_failed", collection=collection, doc_count=len(pairs))
    finally:
        close = getattr(w, "close", None)
        if close is not None:
            close()


def _fence_fail(doc_id: str, error: str) -> None:
    """Advisory: stamp ``index_state='failed'``. Never raises — the caller's
    own exception (the reason this is being called) must always propagate
    unmasked."""
    from nexus.catalog.factory import make_catalog_writer  # noqa: PLC0415 — deferred import; test patch target

    w = None
    try:
        w = make_catalog_writer()
        w.fail_index_run(doc_id, error)
    except Exception:  # noqa: BLE001 — boundary catch: fail is an advisory write; must never mask the original failure
        _log.warning("index_run_fail_write_failed", doc_id=doc_id)
    finally:
        close = getattr(w, "close", None)
        if close is not None:
            close()


def _fence_complete(doc_id: str, content_hash: str, chunk_count: int) -> None:
    """The load-bearing fail-closed stamp (memo §3.3). The engine verifies
    the manifest inside the SAME transaction as the stamp; a refusal comes
    back as :class:`~nexus.errors.IndexRunVerifyRefused`, which PROPAGATES
    — it is the signal this whole arc exists to surface, never swallowed
    into a green summary.

    Any other exception is advisory-only (WARNING, then return): the fence
    stays ``'indexing'``, which means over-work (a future re-index) rather
    than silent under-work. A ``None`` return (the client's pre-fence-engine
    sentinel, http_catalog_client.py's ``complete_index_run`` docstring) is
    NOT success and must never be read as a stamp having landed.

    nexus-5xn3k.6 code-review-expert IMPORTANT (2026-08-02): this is the
    multi-batch/incremental completion path (streaming PDF, prose past
    ``_INCREMENTAL_THRESHOLD``) — distinct from ``mcp_infra``'s
    ``_manifest_write_loop`` / ``_stamp_index_run_complete`` (the flush-grain
    manifest-hook path, nexus-dcv2k). Both are "the completion stamp was
    refused" and both feed the SAME ``.6`` summary consumer
    (``get_complete_refusals()``), so both must record to the same
    collector. Before this fix, a refusal HERE propagated (correct,
    fail-loud) but never reached the collector — the record-level summary
    consumer added in .6 could not see it; only a caller catching
    ``IndexRunVerifyRefused`` itself would know. Recording here closes that
    gap without changing the propagation contract at all: the exception
    still raises unmasked immediately after the record.
    """
    from nexus.catalog.factory import make_catalog_writer  # noqa: PLC0415 — deferred import; test patch target
    from nexus.errors import IndexRunVerifyRefused  # noqa: PLC0415 — deferred import: avoids import cycle at module load

    w = None
    try:
        w = make_catalog_writer()
        result = w.complete_index_run(doc_id, content_hash, chunk_count)
    except IndexRunVerifyRefused:
        from nexus.mcp_infra import _record_complete_refusal  # noqa: PLC0415 — deferred import: avoids import cycle at module load
        _record_complete_refusal(doc_id)
        raise
    except Exception:  # noqa: BLE001 — boundary catch: transport failure leaves the fence 'indexing' (over-work, never data loss); only the typed refusal propagates
        _log.warning("index_run_complete_write_failed", doc_id=doc_id)
        return
    finally:
        close = getattr(w, "close", None)
        if close is not None:
            close()
    if result is None:
        _log.debug("index_run_complete_pre_fence_engine", doc_id=doc_id)


def _repo_owner_document_for(reader, abs_path):
    """An existing catalog Document for *abs_path* under its REPO owner, if any.

    nexus-tqudo. The doc_indexer family (``nx index rdr``, ``nx index md``,
    ``nx collection reindex``) resolves a CURATOR owner; ``nx index repo``
    registers under a REPO owner. ``by_file_path`` is owner-scoped, so a
    curator-side lookup can NEVER see a repo-owner row — structurally, not
    occasionally. A prior ``nx index repo`` therefore leaves a row a later
    doc_indexer run cannot find, and it mints a SECOND Document for one
    physical file.

    THE UNSTATED ASSUMPTION THIS CLOSES. ``_catalog_markdown_hook``'s
    nexus-3lswy docstring argues no double-registration exists because those
    commands "never also run ``_catalog_hook``'s batched pass". That is true
    WITHIN one invocation and silent about a PREVIOUS one. Measured
    2026-08-27: two live forks on this install (rdr-167, rdr-182), each a
    complete repo-owner row shadowed by a complete curator-owner row, four
    days without self-healing.

    NOT reachable by the overlap-based ``_check_document_fork``: that runs
    AFTER the mint, is advisory, and compares manifests — so it is blind to a
    fork against a document whose manifest is EMPTY (the rdr-195 case, which
    forked against a chunk_count=0 row). Detection after the fact cannot
    close this; the lookup has to.

    BEST-EFFORT BY CONSTRUCTION: every failure returns ``None`` and the caller
    proceeds exactly as before. A cross-owner probe that raises must never be
    able to break indexing — this is a lookup widening, not a new gate.
    """
    try:
        from pathlib import Path as _Path  # noqa: PLC0415 — stdlib, deferred
        from nexus.repo_identity import _repo_identity_with_main  # noqa: PLC0415 — circular-dep avoidance

        p = _Path(abs_path)
        probe = p.parent if p.parent != p else p
        _name, repo_hash, main_repo = _repo_identity_with_main(probe)
        owner = reader.owner_for_repo(repo_hash)
        if owner is None:
            return None
        # The repo owner keys file_path relative to the REPO ROOT, which is
        # not necessarily the curator side's base_path. Re-derive rather than
        # reusing the caller's ``fp``.
        try:
            rel = str(p.resolve().relative_to(_Path(main_repo).resolve()))
        except ValueError:
            return None
        return reader.by_file_path(owner, rel)
    except Exception:  # noqa: BLE001 — boundary catch: a failed probe degrades to "no repo-owner row", never to a broken index
        return None


def _register_or_lookup_doc_id(
    file_path: Path,
    corpus: str,
    *,
    content_type: str,
    physical_collection: str,
    title: str = "",
    author: str = "",
    year: int = 0,
    base_path: Path | None = None,
    source_uri: str = "",
    with_created: bool = False,
) -> str | tuple[str, bool]:
    """RDR-102 D1 pre-flight catalog registration for the doc_indexer family.

    Pass *with_created=True* (nexus-uxg4u task 2) to additionally receive
    whether THIS call minted a brand-new Document — returns ``(doc_id,
    created)`` instead of a bare ``doc_id`` string. ``created`` is True
    only for the genuine ``writer.register`` mint path at the tail of
    this function; every lookup/dedup/skip/error path returns
    ``created=False``. Callers use this to decide whether a downstream
    failure should roll back (delete) the fresh registration via
    :func:`nexus.catalog.store_hook.rollback_minted_catalog_entry` — a
    pre-existing document (``created=False``) is never a rollback
    candidate. Default ``False`` preserves the historical bare-``str``
    return for every existing call site.

    Mirrors the ``indexer.py:run`` upfront pattern: open the catalog,
    resolve (or create) the curator owner for *corpus*, look up the
    Document by ``(owner, file_path)`` and either return the existing
    tumbler or register a fresh one. The returned tumbler string is the
    ``doc_id`` that ``_pdf_chunks`` / ``_markdown_chunks`` thread to
    ``make_chunk_metadata`` so chunks land in T3 with ``doc_id``
    populated at write time — closing the gap that ChromaDB's
    undocumented upsert metadata-merge was masking pre-RDR-102.

    Auto-initializes the catalog if absent (nexus-fq3b). Pre-fix, this
    function silently returned ``""`` for users without a catalog,
    chunks landed without ``doc_id``, and the post-Phase-5c prune
    fallback matched zero stale chunks because ``source_path`` was
    dropped from ALLOWED_TOP_LEVEL. Auto-init means every PDF/markdown
    indexing call results in a registered ``doc_id``, and subsequent
    re-indexes find prior chunks via the doc_id-keyed where filter.
    The catalog directory follows ``catalog_path()`` (env or XDG
    default), so users without an explicit ``nx catalog init`` get one
    on first index.

    Returns ``""`` only when an unexpected error occurs (best-effort:
    the caller falls back to the legacy identity path, which on
    Phase 5c collections will not match by source_path; the surrounding
    ``except Exception`` at the bottom of this function logs the
    failure for diagnosis) — this best-effort contract does NOT apply
    to the two ``source_uri`` fail-loud rules below, which propagate.

    Re-registration is event-idempotent via ``Catalog.register``'s
    ``by_file_path`` early-return at ``catalog.py:1218-1234``: a second
    call for the same ``(owner, file_path)`` returns the existing
    tumbler without writing a new ``DocumentRegistered`` event. RDR-102
    R1 + the ``test_preflight_registration_idempotent_on_staleness_skip``
    test pin this invariant.

    nexus-y8qtj: when *source_uri* is provided, identity resolution goes
    through :meth:`CatalogReader.by_source_uri` FIRST instead of
    ``by_file_path``. A path-based re-index of a document whose real
    identity is an out-of-band URI (e.g. DEVONthink's
    ``x-devonthink-item://<UUID>``, stamped onto the entry AFTER the
    auto-derived ``file://`` registration by ``nx dt index``) misses the
    file_path lookup and used to silently register a SECOND Document —
    leaving the original's chunks live, searchable, and un-swept (they
    remain part of the original document's own current manifest, so no
    orphan sweep would ever remove them). Two invariants are fail-loud
    and are NOT swallowed by the broad ``except Exception`` below (both
    propagate to the CLI as a hard error, never a silent fallback):

    - *source_uri* resolves to no LIVE document -> raises
      :class:`~nexus.errors.SourceUriNotFoundError`. Falling back to
      registering a brand-new Document here is exactly the y8qtj defect.
    - the resolved document's ``physical_collection`` differs from
      *physical_collection* -> raises
      :class:`~nexus.errors.SourceUriCollectionMismatchError`. Naming a
      ``--source-uri`` and a ``--collection`` that disagree with the
      document's current home is a MOVE, not a re-index.
    """
    from nexus.errors import (  # noqa: PLC0415 — circular-dep avoidance (nexus.errors)
        SourceUriCollectionMismatchError,
        SourceUriNotFoundError,
    )

    reader = None
    writer = None
    try:
        from nexus.catalog.types import make_relative  # noqa: PLC0415 — circular-dep avoidance (nexus.catalog.types)
        from nexus.catalog.factory import (  # noqa: PLC0415 — circular-dep avoidance (nexus.catalog.factory)
            make_catalog_reader,
            make_catalog_writer,
        )
        from nexus.catalog.tumbler import Tumbler  # noqa: PLC0415 — circular-dep avoidance (nexus.catalog.tumbler)

        # RDR-146 P1.2 strict split: reads via reader, writes via the
        # write-only daemon proxy.
        # nexus-i711w: the nexus-fq3b local auto-init leg died with the local
        # catalog — the service owns the catalog in every mode, and
        # make_catalog_reader always returns a service-backed reader.
        reader = make_catalog_reader()
        writer = make_catalog_writer()

        # Owner resolution mirrors _catalog_pdf_hook / _catalog_markdown_hook
        # so a re-index after the post-hook ran for the same file lands on
        # the SAME owner row (otherwise by_file_path would miss and we'd
        # double-register). PDFs default to ``standalone-pdfs`` when corpus
        # is empty; markdown defaults to ``standalone-docs``. content_type
        # ``paper`` selects the PDF default; everything else (``prose``,
        # ``rdr``) picks the docs default.
        if corpus:
            owner_name = corpus
        elif content_type == "paper":
            owner_name = "standalone-pdfs"
        else:
            owner_name = "standalone-docs"
        # Curator-only lookup. A REPO owner can share the same name
        # (e.g. "scheme-evolution-research" exists as both a repo
        # owner created by ``nx index repo`` AND as a target for
        # ``nx index pdf --corpus scheme-evolution-research``). Picking
        # up the repo owner here triggers the cross-project guard at
        # ``catalog.py:_check_source_uri_in_repo_root`` when the file
        # lives outside the repo's tree (e.g. a DEVONthink-sourced
        # PDF), the resulting ValueError gets caught broadly below
        # and returns "" — silently breaking Phase A pre-flight
        # registration for cross-source ingest. Filtering on
        # owner_type='curator' keeps the namespaces separate; repo
        # owners are reachable only via owner_for_repo(repo_hash)
        # from the repo indexer, never via a corpus-name lookup here.
        # nexus-qnp5s: curator_owner_tumbler_by_name() is implemented on
        # both SQLite Catalog and HttpCatalogClient — no raw _db access.
        owner_t = reader.curator_owner_tumbler_by_name(owner_name)
        if owner_t is not None:
            owner = owner_t
        else:
            owner = writer.register_owner(owner_name, "curator")

        fp = make_relative(file_path, base_path) if base_path else str(file_path)

        # nexus-y8qtj: source_uri-keyed resolution takes priority over the
        # file_path lookup below, and is fail-loud rather than falling
        # through to registration on a miss.
        if source_uri:
            existing_by_uri = reader.by_source_uri(source_uri)
            if existing_by_uri is None:
                raise SourceUriNotFoundError(
                    f"--source-uri {source_uri!r} resolves to no live "
                    f"catalog document. Refusing to register a new one "
                    f"under {fp!r} — that fallback is the nexus-y8qtj "
                    f"defect (a path-based re-index silently forking a "
                    f"second Document for a source registered under a "
                    f"different identity). If this source was never "
                    f"indexed, index it without --source-uri first; if "
                    f"the URI is wrong, correct it."
                )
            if existing_by_uri.physical_collection != physical_collection:
                raise SourceUriCollectionMismatchError(
                    f"--source-uri {source_uri!r} resolves to document "
                    f"{existing_by_uri.tumbler} in collection "
                    f"{existing_by_uri.physical_collection!r}, but this "
                    f"index run targets {physical_collection!r}. Refusing "
                    f"to name both — re-indexing into a different "
                    f"collection is a move, not a re-index. Use "
                    f"'nx catalog update' to move the document "
                    f"deliberately, or drop --collection to target its "
                    f"current home."
                )
            return (str(existing_by_uri.tumbler), False) if with_created else str(existing_by_uri.tumbler)

        existing = reader.by_file_path(owner, fp)
        if existing is None:
            # nexus-tqudo: the curator namespace cannot see a row a
            # prior `nx index repo` registered under the REPO owner.
            # Look there before minting, or one physical file ends up
            # with two catalog Documents.
            existing = _repo_owner_document_for(reader, file_path)
        if existing is not None:
            # nexus-2t63u: reconcile a stale ``physical_collection`` EARLY,
            # before the tumbler is returned and threaded into this run's
            # chunk/manifest writes. Without this, a path-based re-index
            # into a DIFFERENT --collection lands chunks correctly in the
            # NEW collection, but the engine's ``writeManifestRows`` /
            # ``appendManifestChunks`` stamp EVERY manifest row's
            # ``collection`` from ``catalog_documents.physical_collection``
            # (read unconditionally at manifest-write time — see
            # ``CatalogRepository.writeManifestRows``/``appendManifestChunks``
            # on the engine side) — so the manifest keeps pointing at the
            # OLD collection. ``manifest_verify`` then joins against the
            # WRONG collection and reports live, present chunks as
            # "missing", tripping the RUNFENCE completion refusal
            # (``IndexRunVerifyRefused``) BEFORE ``_catalog_pdf_hook`` — the
            # only other writer of this field — ever runs (it sits at the
            # tail of ``index_pdf``, reached only on a non-refused
            # completion). Every subsequent run reproduces the identical
            # counts: a self-perpetuating wedge (T2
            # nexus/nexus-2t63u-debug-2026-08-06).
            #
            # This mirrors what ``_catalog_pdf_hook`` already does at the
            # tail (``writer.update(existing.tumbler,
            # physical_collection=collection_name, ...)``); it only moves
            # that reconciliation ahead of the manifest write, which is the
            # ordering the engine's stamp requires. Contrast the
            # ``source_uri`` branch above, which RAISES
            # ``SourceUriCollectionMismatchError`` on this same divergence —
            # a ``--source-uri`` + ``--collection`` pair that disagrees with
            # the document's home is an explicit, deliberate move. A plain
            # ``by_file_path`` re-index under a new ``--collection`` is the
            # common, intentional retarget case instead, so this branch
            # reconciles rather than refusing (nexus-2t63u DESIGN CHOICE,
            # Option A — orchestrator comment on the bead, per the
            # debugger's A/B handoff in T2 [21558]).
            if existing.physical_collection != physical_collection:
                # nexus-ir68m (code-review Important #1 / substantive-critic
                # CRITICAL, round 2, empirically proven by the critic's
                # probe): this write is ADVISORY, isolated in its own narrow
                # try/except — never the outer function's broad ``except
                # Exception: return ""`` below. A tumbler this branch already
                # RESOLVED (``existing`` is a live document) must never be
                # discarded because of a transient reconcile-write failure;
                # doing so hands ``index_pdf`` doc_id="", which skips BOTH
                # ``_fence_begin`` and ``_fence_complete`` (both gated on
                # ``if _catalog_doc_id_for_batch:``) and drops the WHOLE run
                # out of RUNFENCE — reintroducing the exact un-fenced-
                # completion gap RDR-102/nexus-5xn3k/nexus-tp8yk closed, in
                # service of fixing a DIFFERENT bug. Mirrors ``_fence_begin``'s
                # established fail-open advisory-write contract: log and
                # proceed with the tumbler under its CURRENT (old) stamp —
                # the run then either completes honestly against the old
                # collection or refuses with the now-honest mismatch message
                # (``_index_run_refused_message``'s ``target_collection``
                # lookup) — both outcomes honest, neither unfenced.
                try:
                    writer.update(existing.tumbler, physical_collection=physical_collection)
                except Exception:  # noqa: BLE001 — boundary catch: advisory write, must never discard an already-resolved tumbler (nexus-ir68m fail-open contract)
                    _log.warning(
                        "doc_physical_collection_reconcile_write_failed",
                        tumbler=str(existing.tumbler),
                        file_path=fp,
                        old_collection=existing.physical_collection,
                        new_collection=physical_collection,
                    )
                else:
                    _log.warning(
                        "doc_physical_collection_reconciled",
                        tumbler=str(existing.tumbler),
                        file_path=fp,
                        old_collection=existing.physical_collection,
                        new_collection=physical_collection,
                    )
                    # nexus-2t63u round 2 (substantive-critic observation
                    # 4): count successful reconciliations so a batch run's
                    # own summary line can surface "N collection
                    # reconciliation(s)" instead of requiring the operator
                    # to notice a mass mistaken --collection retarget by
                    # scrolling back through WARNING-level structlog output.
                    from nexus.mcp_infra import _record_physical_collection_reconciled  # noqa: PLC0415 — circular-dep avoidance (nexus.mcp_infra)
                    _record_physical_collection_reconciled()
            return (str(existing.tumbler), False) if with_created else str(existing.tumbler)

        # nexus-u8n4r: refuse registration when the identity about to be
        # STORED (``fp`` — absolute when no ``base_path`` was threaded, as
        # is the case for the standalone ``nx index md``/``nx index rdr``
        # pre-flight path) sits under an agent worktree or system temp
        # dir, UNLESS this owner's own repo_root is itself rooted there
        # (throwaway owners / the pytest tmp-dir suite). See
        # ``nexus.repo_identity.should_skip_ephemeral_registration`` for
        # the full rationale; curator owners (this function's default)
        # normally carry an empty repo_root, so this is a documented
        # residual, not a gap this call site closes on its own.
        from nexus.repo_identity import (  # noqa: PLC0415 — circular-dep avoidance (nexus.repo_identity)
            canonicalize_worktree_path,
            is_worktree_or_tempdir_path,
            owner_repo_root_best_effort,
            should_skip_ephemeral_registration,
        )
        _owner_repo_root = owner_repo_root_best_effort(reader, owner)
        # nexus-kkumv: when the owner's own root is NOT itself worktree/
        # tempdir-rooted (i.e. NOT the deliberate throwaway-owner
        # population the guard below exempts), rewrite a worktree-marker
        # ``fp`` to its primary-repo identity BEFORE the refusal check —
        # but only when the primary-repo mirror genuinely exists on disk
        # (mirrors indexer.py's worktree-unique-file precedent). A file
        # that only exists inside the worktree still falls through to the
        # refusal below, unchanged.
        if not is_worktree_or_tempdir_path(_owner_repo_root):
            _canonical_fp = canonicalize_worktree_path(fp)
            if _canonical_fp != fp and Path(_canonical_fp).is_file():
                fp = _canonical_fp
        if should_skip_ephemeral_registration(fp, _owner_repo_root):
            _log.warning(
                "ephemeral_path_registration_skipped",
                path=fp, owner=str(owner), reason="worktree_or_tempdir",
            )
            from nexus.mcp_infra import _record_ephemeral_registration_skip  # noqa: PLC0415 — circular-dep avoidance (nexus.mcp_infra)
            _record_ephemeral_registration_skip(fp, str(owner), reason="worktree_or_tempdir")
            return ("", False) if with_created else ""

        try:
            source_mtime = file_path.stat().st_mtime
        except OSError:
            source_mtime = 0.0
        # nexus-uxg4u task 2: only request the created-vs-matched signal
        # when THIS call's caller asked for it — mirroring every existing
        # call site's plain-tumbler contract exactly (never touching the
        # wire shape they rely on) is safer than always asking, since a
        # test double's ``writer.register`` fake (there are several,
        # returning a bare tumbler/string) would otherwise need to know
        # about a kwarg it predates.
        if with_created:
            _write_result = writer.register(
                owner=owner,
                title=title or file_path.stem,
                content_type=content_type,
                file_path=fp,
                corpus=corpus,
                physical_collection=physical_collection,
                chunk_count=0,
                year=year,
                author=author,
                source_mtime=source_mtime,
                source_uri=source_uri,
                with_created=True,
            )
            # Defensive: a test double's ``writer.register`` fake (several
            # exist, predating with_created) ignores the kwarg and returns
            # a bare tumbler/string rather than a 2-tuple. Treat that the
            # same way HttpCatalogClient.register treats an older engine
            # that omits the wire field entirely — "created=True" is the
            # historical assumption every caller ignoring this parameter
            # already makes, not a guess this call site invents.
            if isinstance(_write_result, tuple):
                tumbler, created = _write_result
            else:
                tumbler, created = _write_result, True
            return str(tumbler), created
        tumbler = writer.register(
            owner=owner,
            title=title or file_path.stem,
            content_type=content_type,
            file_path=fp,
            corpus=corpus,
            physical_collection=physical_collection,
            chunk_count=0,
            year=year,
            author=author,
            source_mtime=source_mtime,
            source_uri=source_uri,
        )
        return str(tumbler)
    except (SourceUriNotFoundError, SourceUriCollectionMismatchError):
        # nexus-y8qtj: fail-loud rules propagate — never swallowed by the
        # best-effort catch-all below.
        raise
    except Exception:  # noqa: BLE001 — best-effort/telemetry path; must not crash caller
        # nexus-h9f1w / GH #1350 Fix C: a preflight registration failure meant a
        # new file gets no catalog node (orphan chunks) yet the ingest still
        # reports success. Surface it at warning, not debug, so the operator can
        # see a non-clean run instead of silent data loss.
        _log.warning("preflight_register_failed", exc_info=True)
        return ("", False) if with_created else ""
    finally:
        if writer is not None:
            writer.close()
        if reader is not None:
            reader.close()  # nexus-qnp5s: HttpCatalogClient.close() is safe; Catalog._db.close() is internal


def _make_local_embed_fn() -> tuple[EmbedFn, str]:
    """Build an ``embed_fn`` backed by :class:`LocalEmbeddingFunction`,
    returned alongside the model name it will report.

    Used by ``_index_document`` and ``index_pdf`` as the credential-
    free fallback path: when the user is in local mode
    (:func:`nexus.config.is_local_mode`), ingestion uses the same
    ONNX MiniLM / fastembed model that ``store_put`` and local-mode
    ``nx search`` already use. The chunk metadata records the actual
    model that ran, not the requested ``target_model`` — staleness
    checks fire correctly when a later upgrade to cloud changes the
    target model.

    The caller overrides its ``target_model`` with the returned model
    name so the staleness check + chunk metadata are consistent: a
    re-index in local mode against unchanged content is a no-op
    instead of a silent re-embed.
    """
    from nexus.db.local_ef import LocalEmbeddingFunction  # noqa: PLC0415 — circular-dep avoidance (nexus.db.local_ef)

    local_ef = LocalEmbeddingFunction()
    model_name = local_ef.model_name

    def _local_embed(texts: list[str], _target_model: str) -> tuple[list[list[float]], str]:
        # Honest about the actual model used. ``staleness_check`` in
        # ``_index_document`` compares ``stored_model == target_model``;
        # the caller overrides ``target_model`` with this name so
        # repeat-indexes against unchanged content are no-ops.
        embeddings = local_ef(texts)
        # Normalise to ``list[list[float]]`` regardless of which tier
        # ran underneath. ChromaDB's ONNXMiniLM_L6_V2 (TIER0) returns
        # ``np.ndarray`` per row; the chromadb upsert validator
        # accepts list[list[float]] or list[np.ndarray] but rejects
        # list[list[np.float32]] — so we convert all the way down to
        # native floats. fastembed (TIER1) is converted by
        # LocalEmbeddingFunction but ``np.float32`` scalars survive.
        normalized: list[list[float]] = []
        for vec in embeddings:
            if hasattr(vec, "tolist"):
                normalized.append(vec.tolist())  # numpy → list[float]
            else:
                normalized.append([float(x) for x in vec])
        return normalized, model_name

    return _local_embed, model_name


_INCREMENTAL_BATCH_SIZE = 128  # Chunks per incremental embed/upsert batch
_INCREMENTAL_THRESHOLD = 128  # Use incremental path when chunk count exceeds this
_STREAMING_THRESHOLD = 0      # All PDFs use the streaming pipeline (resilient path)


def _upsert_skip_reembed(
    db: Any,
    collection_name: str,
    ids: list[str],
    documents: list[str],
    embeddings: list,
    metadatas: list[dict],
    *,
    force: bool = False,
) -> int:
    """Upsert chunks, short-circuiting server-side re-embedding of known chashes.

    nexus-h8rf6.4: the 6.2.0 shakeout measured full-run service-mode indexing
    at ~4.7 files/min — every chunk went to ``/v1/vectors/upsert-chunks`` and
    was embedded via Voyage even when its chash already existed in the
    collection. Chunks are content-addressed (``chash = sha256(chunk_text)``),
    so an existing chash means IDENTICAL text and the stored embedding is
    already correct by construction; only the METADATA may need refreshing
    (source_path/indexed_at — the pre-optimization upsert's ON CONFLICT DO
    UPDATE refreshed it, so skipping outright would strand stale metadata).

    Service mode only: the split happens BEFORE any embedding cost is paid
    (the server embeds). In local mode the embeddings were already computed
    by the caller, so there is nothing left to save — full upsert unchanged.

    The existence probe is an optimization, never a gate: any probe failure
    (or a db shape without ``existing_ids``) degrades to the full upsert,
    i.e. exactly the pre-optimization behavior.

    ``force`` (RDR-181 §Approach step 3): when True, this function's OWN
    client-side existence probe (the ``nexus-h8rf6.4`` optimization above,
    independent of and predating the RDR-181 server-side embed-skip) is
    bypassed entirely — every chunk is sent through
    ``upsert_chunks_with_embeddings`` with ``force_re_embed=True`` so the
    server also skips its existence-partition and re-embeds unconditionally.
    Without this, a ``--force`` reindex would still take the client-side
    metadata-only-update branch for unchanged chashes below, silently
    defeating the caller's intent to force a full re-embed.

    Returns the number of chunks actually sent down the embed path.
    """
    from nexus.db import http_vector_client as _hvc  # noqa: PLC0415 — circular-dep avoidance (nexus.db.http_vector_client)

    if not ids:
        return 0
    if not _hvc.is_vector_service_mode():
        db.upsert_chunks_with_embeddings(collection_name, ids, documents, embeddings, metadatas)
        return len(ids)
    if not _hvc.is_service_backed(db):
        # nexus-5lygi: NX_STORAGE_BACKEND_VECTORS says service mode (the
        # ``is_vector_service_mode()`` gate above passed), but the db
        # handle actually resolved here is not ``HttpVectorClient`` — env
        # state and handle type CAN diverge (see
        # ``is_vector_service_mode``'s own docstring), and this is exactly
        # the shape a leaked test fixture produces by swapping
        # ``mcp_infra._t3_instance`` to an in-process handle without
        # restoring it (nexus-gtl01 root cause: T2
        # nexus/gtl01-root-cause-2026-08-09). A WARNING, not a raise or a
        # skip: tests legitimately inject non-service handles on purpose,
        # and the branches below still need to run against whatever ``db``
        # is. This single log line is the correlation that would have
        # turned two days of gtl01 triage aimed at the engine into an
        # immediate "the handle is wrong, not the engine" read — see the
        # scoped comment below on what the ack-contract invariant does and
        # does not prove for a handle like this.
        _log.warning(
            "upsert_skip_reembed_non_service_handle",
            collection=collection_name,
            handle_type=type(db).__name__,
        )
    if force:
        _log.debug(
            "upsert_skip_reembed_branch",
            collection=collection_name,
            branch="force_full_upsert",
            count=len(ids),
        )
        db.upsert_chunks_with_embeddings(
            collection_name, ids, documents, embeddings, metadatas,
            force_re_embed=True,
        )
        return len(ids)
    present: set[str] = set()
    try:
        present = set(db.existing_ids(collection_name, ids))
    except Exception as exc:  # noqa: BLE001 — probe is best-effort; full upsert is the correct fallback
        _log.warning(
            "existing_ids_probe_failed_full_upsert",
            collection=collection_name,
            error=str(exc),
        )
    # nexus-gtl01: per-batch existing-probe verdict — counts only (bounded),
    # never the chash lists themselves. This is the decision this whole
    # function turns on (skip-reembed vs full upsert vs metadata-only), and
    # prior to this bead only its FAILURE was logged — the routine verdict
    # (including a probe that legitimately found nothing, which is
    # indistinguishable in the logs from a probe that never ran) had no
    # trace at all.
    _log.debug(
        "upsert_skip_reembed_probe",
        collection=collection_name,
        total=len(ids),
        present=len(present),
        new=len(ids) - len(present),
    )
    if not present:
        _log.debug(
            "upsert_skip_reembed_branch",
            collection=collection_name,
            branch="full_upsert_no_existing",
            count=len(ids),
        )
        db.upsert_chunks_with_embeddings(collection_name, ids, documents, embeddings, metadatas)
        # nexus-gtl01 (upsert-chunks ACK coverage): tie the outcome to the
        # branch event above via collection + count. This is the branch the
        # captured 2026-08-08 recurrence took (probe present=0, branch=
        # full_upsert_no_existing, count=1) immediately before the chunk was
        # found absent at verify, with no exception raised in between.
        #
        # nexus-5lygi: the claim that follows is SCOPED to ``db`` actually
        # being an ``HttpVectorClient`` — it is a property of that ONE
        # implementation of this duck-typed interface, not of "reaching
        # this line" in general. For HttpVectorClient, reaching this line
        # means ``upsert_chunks_with_embeddings`` RETURNED without raising
        # — its ack-mismatch house pattern raises inside it on a missing/
        # wrong count, so a normal return DOES rule out "the write call
        # never completed" and "an exception was silently swallowed above
        # this line" as explanations for a later-absent chunk, narrowing
        # the healthy-shape residue to the engine-side commit-durability
        # question logged alongside HttpVectorClient's own request/
        # response events (see http_vector_upsert_chunks_response's
        # comment for what remains undecidable from the client side
        # alone).
        #
        # For any OTHER handle reachable here — e.g. a ``T3Database`` over
        # ``InMemoryVectorClient``, the shape a leaked test fixture can
        # swap into the ``mcp_infra._t3_instance`` singleton without
        # restoring it — a normal return proves NONE of that. Such a
        # handle returns having written to an in-process structure only;
        # it says nothing about whether any bytes ever reached the engine,
        # let alone whether the engine committed them. The WARNING logged
        # above (``upsert_skip_reembed_non_service_handle``) fires exactly
        # when this scoping matters: this comment's invariant held, but it
        # was being read as if it covered the handle that was actually in
        # play, which is precisely how two days of gtl01 triage got aimed
        # at engine-side commit durability and cross-tenant RLS/GUC bleed
        # while the real cause was upsert-never-sent from a non-service
        # handle (root cause: T2 nexus/gtl01-root-cause-2026-08-09).
        _log.debug(
            "upsert_skip_reembed_upsert_outcome",
            collection=collection_name,
            branch="full_upsert_no_existing",
            count=len(ids),
            completed=True,
        )
        return len(ids)
    new_idx = [i for i, cid in enumerate(ids) if cid not in present]
    old_idx = [i for i, cid in enumerate(ids) if cid in present]
    _log.debug(
        "upsert_skip_reembed_branch",
        collection=collection_name,
        branch="split",
        content_write=len(new_idx),
        metadata_only_candidate=len(old_idx),
    )
    if new_idx:
        db.upsert_chunks_with_embeddings(
            collection_name,
            [ids[i] for i in new_idx],
            [documents[i] for i in new_idx],
            [embeddings[i] for i in new_idx],
            [metadatas[i] for i in new_idx],
        )
    if old_idx:
        # Metadata-only refresh — no embedding cost, preserves the
        # pre-optimization ON CONFLICT DO UPDATE metadata semantics.
        missing = db.update_chunks(
            collection_name,
            [ids[i] for i in old_idx],
            [metadatas[i] for i in old_idx],
        )
        # nexus-gtl01: the update_chunks "missing"-list disposition, logged
        # at the decision point regardless of which of the three branches
        # below is taken (raise / reroute / clean) — prior to this bead the
        # ONLY trace of this decision was the reroute WARNING (fires only
        # when missing is truthy) and http_vector_client's own None-case
        # WARNING; the routine "missing == []" outcome (engine positively
        # confirmed every id, no reroute needed) had no trace anywhere.
        _log.debug(
            "upsert_skip_reembed_update_chunks_disposition",
            collection=collection_name,
            candidate_count=len(old_idx),
            missing_reported=missing is not None,
            missing_count=(len(missing) if missing is not None else None),
        )
        # nexus-5xn3k.5 (memo §3.6, AC6 client half): ``missing`` is None
        # when the engine's response omitted the "missing" field (a
        # pre-nexus-5xn3k.2 engine) — "cannot tell", not "zero misses".
        # HttpVectorClient.update_chunks already logged the WARNING for
        # that case.
        #
        # nexus-tp8yk D1: "cannot tell" used to fall through as "no
        # reroute" and return normally — the caller's manifest hook then
        # wrote rows for a batch this function never confirmed landed
        # (design memo §1 P1). CONFIRMED-LANDING CONTRACT: every id this
        # function is given is either (a) a fresh upsert, confirmed by
        # upsert_chunks' own ack-mismatch check, (b) a genuine metadata-
        # only refresh (``missing`` reported and this id wasn't in it), or
        # (c) rerouted through a full upsert below when ``missing`` names
        # it. A ``None`` response satisfies none of the three — refuse
        # rather than silently proceed. All four call sites are already
        # wrapped in a fence-bracketed try/except (``_fence_fail`` then
        # re-raise), so this raise fails the run loudly instead of
        # minting an unconfirmed manifest.
        #
        # nexus-gtl01 DESIGN QUESTION (deliberately log-only, not a new
        # fail-loud check): a "missing": [] response here means the engine
        # POSITIVELY confirms every one of these ids exists — agreeing with
        # our own existing_ids probe. This is not merely an agreeing signal
        # that COULD both be wrong — it is demonstrably sound: engine-side,
        # PgVectorRepository.updateMetadataWithMissing derives "missing"
        # directly from each per-row UPDATE's own SQL rowcount (rows == 0
        # -> missing.add(id); see the per-id loop around `affected += rows`
        # in that method) — it is not a separate, independently-computable
        # anti-join that could disagree with the UPDATE it describes.
        # "missing": [] IS PostgreSQL's own statement rowcount asserting
        # each row physically existed at update time, not a second opinion
        # that happens to agree with our probe. The hypothesized double-
        # fault (client probe false-positive AND engine missing-computation
        # false-negative, simultaneously) would require that rowcount
        # itself to lie about whether the UPDATE touched a row — there is
        # no local information at this call site that could distinguish
        # that from the true-positive case, and the discarded "affected"/
        # "updated" count is not a hidden cheap cross-check either: it
        # equals len(ids) - len(missing) by construction (both are summed
        # from the same per-id rowcounts), so comparing them is vacuous.
        # The only way to positively rule the double-fault out would be an
        # extra read-verification per batch, which duplicates the
        # /index-run/complete fail-closed fence's job (bead nexus-5xn3k.4)
        # at per-batch cost for a case that fence already catches at
        # completion time. Left to that fence rather than added here; the
        # deferral is demonstrated by the above, not merely a judgment
        # call — revisit only if the fence is ever found to miss this
        # shape in practice.
        if missing is None:
            from nexus.errors import ChunkLandingUnverifiedError  # noqa: PLC0415 — deferred import: avoids import cycle at module load
            raise ChunkLandingUnverifiedError(
                collection=collection_name, count=len(old_idx),
            )
        if missing:
            # The existing_ids probe was a STALE POSITIVE for these ids:
            # reported present, but the row was gone by the time the
            # metadata-only update above ran, so it silently touched
            # nothing for them. Re-route through a full upsert (content +
            # embeddings) so the content actually lands instead of being
            # dropped. Service mode embeds server-side, so whatever
            # embeddings shape the new_idx branch above already forwards
            # (including empty passthrough) is safe to forward here too.
            #
            # Division of labor (nexus-5xn3k.5 vs .4): this reroute repairs
            # a STALE-POSITIVE PROBE miss only — the row was already gone
            # by the time update_chunks ran above. A row that vanishes
            # AFTER this reroute's write succeeds (a post-repair race) is
            # NOT this path's job; that window is covered by the
            # /index-run/complete fail-closed verify (bead nexus-5xn3k.4).
            missing_set = set(missing)
            reroute_idx = [i for i in old_idx if ids[i] in missing_set]
            if reroute_idx:
                _log.warning(
                    "update_chunks_missing_rerouted",
                    collection=collection_name,
                    count=len(reroute_idx),
                )
                db.upsert_chunks_with_embeddings(
                    collection_name,
                    [ids[i] for i in reroute_idx],
                    [documents[i] for i in reroute_idx],
                    [embeddings[i] for i in reroute_idx],
                    [metadatas[i] for i in reroute_idx],
                )
    _log.debug(
        "upsert_skip_reembed",
        collection=collection_name,
        total=len(ids),
        embedded=len(new_idx),
        skipped=len(old_idx),
    )
    return len(new_idx)


def _resolve_write_db(t3: Any) -> Any:
    """Resolve the T3-like client an indexer will write through.

    nexus-o5x2c (nexus-35ok4 round 4 SHIP-BLOCKER): factored out so the
    three ``docs_leaf_fallback_collection_name`` call sites below
    (``_index_document``, ``index_pdf``, ``index_markdown``) can resolve
    the client BEFORE deriving a fallback collection name — needed to
    supply ``docs_leaf_fallback_collection_name``'s ``collection_exists``
    grandfather probe — and then reuse the SAME instance for the actual
    write, instead of resolving it twice (once, uselessly, discarded).

    RDR-152 Seam B: when *t3* is None, route through ``get_t3()`` in
    service mode so ``HttpVectorClient`` is used instead of a daemon
    ``T3Database``, preventing the split-brain where indexing writes to
    daemon-Chroma but search reads service-Chroma. In non-service mode,
    ``make_t3()`` preserves existing test-mock contracts (tests patch
    ``nexus.doc_indexer.make_t3``).
    """
    if t3 is not None:
        return t3
    from nexus.db.http_vector_client import is_vector_service_mode  # noqa: PLC0415 — circular-dep avoidance (nexus.db.http_vector_client)
    if is_vector_service_mode():
        from nexus.mcp_infra import get_t3  # noqa: PLC0415 — circular-dep avoidance (nexus.mcp_infra)
        return get_t3()
    return make_t3()


def _index_document(
    file_path: Path,
    corpus: str,
    chunk_fn: ChunkFn,
    t3: Any = None,
    *,
    collection_name: str | None = None,
    embed_fn: EmbedFn | None = None,
    force: bool = False,
    return_metadata: bool = False,
    on_progress: Callable[[int, int], None] | None = None,
    source_key: str | None = None,
    hooks: "HookRegistry | None" = None,
    doc_id: str = "",
    source_uri: str = "",
) -> int | list[dict]:
    """Shared indexing pipeline: credential check, staleness, embed, upsert, prune.

    *chunk_fn(file_path, content_hash, target_model, now_iso)* produces the
    per-format (chunk_id, document_text, metadata_dict) tuples.  Returns the
    number of chunks indexed, or 0 if skipped.

    When *collection_name* is provided it is used as the T3 collection name
    directly, bypassing the default ``docs__{corpus}`` derivation.  This is
    used for RDR collections (``rdr__<repo>-<hash8>``).

    When *embed_fn* is provided it replaces the server-embed stub used in
    service mode.  This supports local dry-run mode (ONNX /
    DefaultEmbeddingFunction) without requiring any API keys.

    When *return_metadata* is True, returns the prepared chunk metadatas list
    instead of a bare int.  Callers (index_pdf, index_markdown) use it to
    build format-specific summary dicts.  Default False preserves the existing
    int return type with zero overhead.

    When *source_key* is provided it overrides ``str(file_path)`` as the
    ``source_path`` value used in the staleness check and stale-chunk pruning.
    Callers pass a relative path here so that T3 metadata lookups match the
    relative ``source_path`` stored in chunk metadata (RDR-060).

    When *doc_id* is provided (the caller — ``index_markdown`` — already
    resolved catalog identity, possibly via *source_uri*), it is used
    directly for the post-store hook chains instead of re-deriving via a
    bare ``file_path``-only ``_register_or_lookup_doc_id`` call. Mirrors
    ``pipeline_stages.pipeline_index_pdf``'s ``if not doc_id:`` guard.
    Re-deriving here bypassed the caller's *source_uri*-keyed resolution
    and could mint a fork Document when the resolved path differs from
    the registered one (nexus-y8qtj, reproduced inside its own fix).
    *source_uri* is forwarded only when this function must register
    fresh (``doc_id`` empty).
    """
    # GH #336: when ``nx index md/pdf`` runs in local mode we want
    # the local ONNX/fastembed embedder rather than a hard fail. The
    # local model name overrides ``target_model`` so the staleness
    # check + chunk metadata are consistent.
    #
    # RDR-152 Seam B (nexus-gmiaf.22): service mode is checked FIRST —
    # before is_local_mode() and before the credential guard — so that a
    # production service-mode node with NO Voyage/Chroma creds (the
    # correct configuration when the service embeds) can call
    # ``nx index md/pdf`` without raising CredentialsMissingError.
    # Checking is_local_mode() first was the original ordering bug: the
    # integration test's NX_LOCAL=1 caused _make_local_embed_fn() to fire
    # before the service-mode stub branch, making the "no Python embed"
    # proof vacuous.
    local_target_model: str | None = None
    if embed_fn is None:
        from nexus.db.http_vector_client import is_vector_service_mode  # noqa: PLC0415 — circular-dep avoidance (nexus.db.http_vector_client)

        if is_vector_service_mode():
            # Service embeds server-side. embed_fn stays None here;
            # the embed-stub branch at the upsert site (below) handles it.
            # No Voyage/Chroma creds required; no local ONNX constructed.
            pass
        else:
            from nexus.config import is_local_mode  # noqa: PLC0415 — circular-dep avoidance (nexus.config)

            if is_local_mode():
                embed_fn, local_target_model = _make_local_embed_fn()
            else:
                # nexus-sghyo: non-service, non-local cloud-mode ingestion
                # was retired — the client no longer embeds via Voyage
                # (Hal determination 2026-07-28). Fail loud instead of
                # falling through to a dead credential-driven embed path.
                from nexus.errors import CredentialsMissingError  # noqa: PLC0415 — circular-dep avoidance (nexus.errors)

                raise CredentialsMissingError(
                    "non-service cloud-mode ingestion was retired: the client "
                    "no longer embeds via Voyage. Unset NX_STORAGE_BACKEND_"
                    "VECTORS (service mode is the default) or set NX_LOCAL=1 "
                    "for local-mode ingestion (no API keys needed)."
                )

    # Normalize to absolute so staleness checks are path-form-independent.
    file_path = file_path.resolve()

    sp = source_key if source_key is not None else str(file_path)
    content_hash = _sha256(file_path)
    # RDR-152 Seam B: resolve the write client BEFORE the collection-name
    # fallback below (moved up, nexus-o5x2c) so it can also serve as the
    # grandfather-probe for docs_leaf_fallback_collection_name; reused as
    # ``db`` for the actual write, not resolved twice. See
    # _resolve_write_db's docstring for the service/non-service dispatch.
    db = _resolve_write_db(t3)
    if collection_name is None:
        # RDR-103 Phase 5 leaf fallback. ``corpus`` is a string (the
        # repo basename or operator-supplied corpus tag), not a Path;
        # the conformant ``cat.collection_for_repo`` requires a Path
        # and an initialized catalog with the owner registered.
        # Production hot paths always pass ``collection_name`` from
        # the indexer; this fallback fires for ad-hoc/test invocations
        # and synthesises a conformant 4-segment name so it satisfies
        # ``T3Database.get_or_create_collection``'s strict-naming
        # guard. The owner segment is the corpus tag with underscores
        # rewritten to hyphens (``_`` is the conformant grammar's
        # segment separator); an explicit owner row is not required
        # for ad-hoc paths.
        from nexus.corpus import docs_leaf_fallback_collection_name  # noqa: PLC0415 — circular-dep avoidance (nexus.corpus)

        # nexus-o5x2c: pass the grandfather probe so a keyless
        # voyage-configured local install with a pre-existing bge/minilm
        # collection reuses it instead of crashing (live-repro'd bug).
        collection_name = docs_leaf_fallback_collection_name(
            corpus, collection_exists=lambda name: db.collection_exists(name),
        )
    col = db.get_or_create_collection(collection_name)

    target_model = index_model_for_collection(collection_name)
    if local_target_model is not None:
        # Local-mode override: chunk metadata records the local model
        # name so re-indexes against unchanged content skip cleanly,
        # and a later upgrade to cloud mode triggers re-embed (the
        # cloud target_model differs from the locally-stored name).
        target_model = local_target_model

    # Incremental sync: skip if file is already indexed with the same hash AND model.
    # nexus-dcym: prefer doc_id-keyed lookup; content_hash fallback when
    # the catalog is absent (RDR-101 Phase 5c — source_path is gone).
    incremental_where = _identity_where(sp, corpus, content_hash=content_hash)
    existing = _vector_with_retry(
        col.get,
        where=incremental_where,
        include=["metadatas"],
        limit=1,
    )
    if not force and existing["metadatas"]:
        stored_hash = existing["metadatas"][0].get("content_hash", "")
        stored_model = existing["metadatas"][0].get("embedding_model", "")
        # nexus-5xn3k AC2 / RUNFENCE (nexus-5xn3k.3): the match above is
        # satisfied by ONE surviving chunk (limit=1), so a mid-write failure
        # leaves the document permanently "fresh". The three-way fence check
        # (index_state == 'complete' + hash match -> skip; 'indexing'/'failed'
        # -> re-index; unknown -> one engine manifest/verify call) closes that
        # gap. Identity is resolved READ-ONLY and only on the skip path; a
        # miss fails open.
        if (
            stored_hash == content_hash
            and stored_model == target_model
            and _index_run_fresh(col, _doc_id_for_path(file_path), content_hash)
        ):
            return 0

    now_iso = datetime.now(UTC).isoformat()
    prepared = chunk_fn(file_path, content_hash, target_model, now_iso, corpus)
    if not prepared:
        return 0

    ids = [p[0] for p in prepared]
    documents = [p[1] for p in prepared]
    metadatas = [p[2] for p in prepared]

    # nexus-5xn3k.4 RUNFENCE C2: doc_id resolution HOISTED here — below the
    # staleness gate/`if not prepared: return 0` above (a fresh-skip must
    # NEVER register a doc_id; `_doc_id_for_path`'s read-only property on
    # that path is deliberate) and before the embed/upsert, so the fence
    # can be committed BEFORE the first byte of content lands (memo §3.5
    # T0). The doc_id used to be resolved post-upsert, right before
    # `hooks.fire_batch`; a pointer comment marks the old site.
    #
    # nexus-zq79 F2: use _register_or_lookup_doc_id, NOT _lookup_existing_doc_id.
    # The read-only lookup returns "" for first-time indexes; post-Phase-3
    # chunks have no doc_id fallback so the manifest hook short-circuits and
    # the catalog document ships with chunk_count=0 / empty manifest. Routing
    # via the register-or-lookup path closes the gap idempotently.
    #
    # nexus-y8qtj (reproduced inside its own fix): when the caller already
    # resolved doc_id — index_markdown does this up front, possibly via a
    # source_uri-keyed lookup — reuse it instead of re-deriving via a bare
    # file_path-only _register_or_lookup_doc_id call. Re-deriving here
    # bypassed the caller's source_uri resolution and could mint a fork
    # Document when the resolved path differs from the registered one.
    # Mirrors pipeline_stages.pipeline_index_pdf's `if not doc_id:` guard.
    _catalog_doc_id_for_batch = doc_id
    if not _catalog_doc_id_for_batch:
        _ct_for_register = (metadatas[0].get("content_type") if metadatas else "") or "prose"
        _catalog_doc_id_for_batch = _register_or_lookup_doc_id(
            file_path, corpus,
            content_type=_ct_for_register,
            physical_collection=collection_name,
            source_uri=source_uri,
        )
    if _catalog_doc_id_for_batch:
        _fence_begin(_catalog_doc_id_for_batch, content_hash, collection_name)

    # nexus-5xn3k.4 review follow-up (code-review-expert MEDIUM): begin was
    # already bracketed above; this try/except adds the missing fail
    # bracket around the embed/upsert/hook region. The skip paths (early
    # `return 0` above) precede `begin` and stay fence-untouched by
    # construction — they are outside this try block entirely.
    try:
        if embed_fn is not None:
            embeddings, actual_model = embed_fn(documents, target_model)
        else:
            from nexus.db.http_vector_client import is_vector_service_mode  # noqa: PLC0415 — circular-dep avoidance (nexus.db.http_vector_client)
            if is_vector_service_mode():
                # RDR-152 Seam B (nexus-gmiaf.22): service embeds server-side.
                # Pass empty embeddings; HttpVectorClient.upsert_chunks_with_embeddings
                # ignores them and routes to /v1/vectors/upsert-chunks (JVM embeds).
                embeddings = [[]] * len(documents)
                actual_model = target_model
            else:
                # nexus-sghyo: non-service embedding was retired — the
                # client no longer embeds via Voyage.
                raise RuntimeError(
                    "non-service embedding was retired: the client no "
                    "longer embeds via Voyage. Set NX_STORAGE_BACKEND_"
                    "VECTORS=service (the default) or unset it."
                )
        if actual_model != target_model:
            for m in metadatas:
                m["embedding_model"] = actual_model
        _upsert_skip_reembed(db, collection_name, ids, documents, embeddings, metadatas, force=force)

        # Post-store hook chains (RDR-095). Both single-doc and batch chains
        # fire from every storage event; the per-doc loop covers single-shape
        # consumers on CLI ingest.
        if hooks is None:
            from nexus.hook_registry import HookRegistry, install_default_hooks  # noqa: PLC0415 — circular-dep avoidance: deferred intra-package import
            hooks = HookRegistry()
            install_default_hooks(hooks)
        # doc_id resolution (and the fence `begin`) HOISTED above the
        # embed/upsert (nexus-5xn3k.4) — see `_catalog_doc_id_for_batch` up
        # near `metadatas = [p[2] for p in prepared]`.
        #
        # nexus-tp8yk D2a: this whole function is single-flush (one upsert,
        # one fire_batch — no streaming). It USED TO ride write_manifest_
        # many's optional `complete` map for the completion stamp — but the
        # production writer never exposes write_manifest_many (dcv2k: the
        # op is absent from both CATALOG_WRITE_OPS and
        # _SERVICE_ONLY_WRITE_OPS), so that ride never fired on any real
        # run; completion fell through to mcp_infra's per-doc
        # `_stamp_index_run_complete`, whose refusal is recorded but never
        # propagates to the CLI (design memo §1 P1). manifest_complete is
        # now always None here; the explicit, PROPAGATING `_fence_complete`
        # call below (mirroring `_index_pdf_incremental`'s tail) is the
        # completion stamp for this path.
        hooks.fire_batch(
            ids, collection_name, documents, embeddings, metadatas,
            catalog_doc_id=_catalog_doc_id_for_batch,
        )
        for _did, _doc in zip(ids, documents):
            hooks.fire_single(_did, collection_name, _doc)
        # RDR-089 document-grain chain — fires once per file boundary.
        # content="" because only chunk text is in scope here; the hook
        # reads source_path itself per the P0.1 content-sourcing contract.
        # nexus-tdgc: pre-flight catalog lookup so the aspect-queue hook
        # can capture the doc_id alongside source_path.
        hooks.fire_document(
            sp, collection_name, "",
            doc_id=_catalog_doc_id_for_batch,
        )
    except Exception as exc:
        # _fence_fail never raises, so the original exception always
        # propagates unmasked.
        if _catalog_doc_id_for_batch:
            _fence_fail(_catalog_doc_id_for_batch, str(exc))
        raise

    # nexus-tbkk1: stale-chunk prune via _identity_where's source_path
    # fallback DELETED as dead code. RDR-102 D2 (2026-05-02) removed
    # source_path from make_chunk_metadata — every chunk written above
    # carries no source_path, so a `col.get(where={"source_path": ...})`
    # query here always matched zero rows in production; discovered and
    # documented at nexus-tp8yk's test_index_pdf_prune_union_guard_wired_
    # at_call_site (superseded by nexus-tbkk1's dead-code-deletion tests).
    # This closes only the doc_indexer.py/pipeline_stages.py HALF of
    # RDR-102 D2's "Phase 5b" 4-site class — indexer.py/indexer_utils.py
    # sibling sites were audited and deleted by nexus-afudo (2026-08-05)
    # — Phase 5b is now fully closed. Automatic
    # replacement protection is mcp_infra._sweep_superseded_vectors
    # (manifest-diff based, fires on every hooks.fire_batch/fire_document
    # call above), proven end-to-end at tests/integration/test_tp8yk_
    # manifest_never_outruns_chunks.py::test_union_guard_keeps_shared_
    # chunk_at_the_production_wiring — NOT comprehensive for legacy rows
    # a manifest never referenced; nx t3 gc (chash-vs-manifest, src/
    # nexus/commands/t3.py:219) is the comprehensive but manual/operator-
    # triggered backstop. Full evidence + defense-in-depth discussion:
    # _identity_where's docstring above.

    # nexus-tp8yk D2a: explicit completion stamp, replacing the dead
    # manifest_complete ride (see the comment above `hooks.fire_batch`).
    # Zero-extraction never reaches here — `if not prepared: return 0`
    # above already short-circuits, so `len(prepared)` is always > 0.
    if _catalog_doc_id_for_batch:
        _fence_complete(_catalog_doc_id_for_batch, content_hash, len(prepared))

    if return_metadata:
        return metadatas
    return len(prepared)


def _index_pdf_incremental(
    file_path: Path,
    corpus: str,
    prepared: list[tuple[str, str, dict]],
    content_hash: str,
    collection_name: str,
    t3: Any,
    *,
    embed_fn: EmbedFn | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    hooks: "HookRegistry | None" = None,
    force: bool = False,
    doc_id: str = "",
    source_uri: str = "",
    dry_run: bool = False,
    on_doc_registered: Callable[[str, bool], None] | None = None,
) -> int:
    """Embed and upsert chunks in batches with checkpoint support.

    Designed for large PDFs where the embed/upsert phase can take many minutes.
    Writes a checkpoint after each batch so a crash loses at most one batch
    of work (~128 chunks).

    The full document has already been extracted and chunked — this function
    only handles the embed → upsert → checkpoint loop.

    ``force`` (RDR-181 §Approach step 3) is forwarded to
    :func:`_upsert_skip_reembed` per batch so a ``--force`` reindex reaches
    the server's ``forceReEmbed`` escape here too, not just the small-document
    all-at-once path.

    When *doc_id* is provided (the caller — ``index_pdf`` — already resolved
    catalog identity, possibly via *source_uri*), it is reused directly
    instead of re-deriving via a bare ``file_path``-only
    ``_register_or_lookup_doc_id`` call. Mirrors
    ``pipeline_stages.pipeline_index_pdf``'s ``if not doc_id:`` guard.
    Re-deriving here bypassed the caller's *source_uri*-keyed resolution
    and could mint a fork Document when the resolved path differs from
    the registered one — the >128-chunk incremental path is the flagship
    reproduction of nexus-y8qtj inside its own fix. *source_uri* is
    forwarded only when this function must register fresh (``doc_id``
    empty).

    Pass *dry_run=True* (nexus-uxg4u) to skip the fallback registration,
    the completion fence (``_fence_begin``/``_fence_complete``/
    ``_fence_fail``), and any T2 telemetry those writes would trigger —
    mirrors ``index_pdf``'s own dry-run gate. Not expected on the normal
    call path (``index_pdf`` already resolves *doc_id* and stays on the
    small-document path for a dry run's typically tiny preview), kept
    for direct callers and defense in depth.

    Pass *on_doc_registered* (nexus-uxg4u round 2, code-review-expert
    Finding B) to be notified as ``(doc_id, created)`` whenever THIS
    call's own fallback registration (below) fires and resolves an
    identity — a caller with dry_run=False needs this because *doc_id*
    can arrive here empty for a reason OTHER than "no catalog": the
    caller's own pre-flight can return "" via the worktree/tempdir
    ephemeral-skip path or a swallowed registration exception, in which
    case THIS function's fallback mint is the only registration event
    for the whole run, and the caller's own created-tracking (if any)
    has no way to see it without this callback.

    Returns the total number of chunks indexed.
    """
    target_model = prepared[0][2]["embedding_model"] if prepared else "voyage-context-3"
    total = len(prepared)

    # Check for existing checkpoint — resume from where we left off.
    #
    # Indexing review I1: if the extractor/chunker produced fewer chunks
    # this run than the checkpoint claims (e.g. Docling vs MinerU version
    # mismatch or PDF re-chunked under a new chunk_chars setting), the
    # naive ``min(ckpt.chunks_upserted, total)`` would skip the whole
    # loop and leave the T3 collection with stale chunks beyond index
    # ``total``. Detect the mismatch and discard the checkpoint so we
    # re-index from 0 — slower but correct.
    ckpt = read_checkpoint(content_hash, collection_name)
    start_offset = 0
    if ckpt is not None and ckpt.chunks_upserted > total:
        _log.warning(
            "checkpoint_count_shrunk_discarding",
            stored=ckpt.chunks_upserted,
            current=total,
            pdf=str(file_path),
        )
        delete_checkpoint(content_hash, collection_name)
        ckpt = None
    if ckpt is not None:
        start_offset = min(ckpt.chunks_upserted, total)
        _log.info(
            "checkpoint_resume",
            pdf=str(file_path),
            chunks_done=start_offset,
            total=total,
        )

    ids_all = [p[0] for p in prepared]
    documents_all = [p[1] for p in prepared]
    metadatas_all = [p[2] for p in prepared]

    # Resolve catalog doc_id once outside the per-batch loop (RDR-108
    # Phase 3: chunk metadata no longer carries it; manifest hook reads
    # via the HookRegistry.fire_batch kwarg).
    # nexus-zq79 F2: register-or-lookup, not pure lookup (see _index_document
    # for the rationale — fresh indexes returned "" pre-fix).
    #
    # nexus-y8qtj (reproduced inside its own fix): reuse the caller's
    # already-resolved doc_id (index_pdf resolves it up front, possibly
    # via source_uri) instead of re-deriving via a bare file_path-only
    # _register_or_lookup_doc_id call, which bypassed source_uri
    # resolution and could mint a fork Document for every >128-chunk
    # PDF. Mirrors pipeline_stages.pipeline_index_pdf's `if not doc_id:`
    # guard.
    _catalog_doc_id_for_batch = doc_id
    if not _catalog_doc_id_for_batch and not dry_run:
        _ct_for_register = (metadatas_all[0].get("content_type") if metadatas_all else "") or "pdf"
        # nexus-uxg4u round 2: with_created=True + on_doc_registered so a
        # mint made HERE (the caller's own doc_id arrived empty) is not
        # invisible to the caller's rollback tracking — see this
        # function's own docstring for why doc_id can be empty here for a
        # reason other than "no catalog".
        _reg_result = _register_or_lookup_doc_id(
            Path(file_path), corpus,
            content_type=_ct_for_register,
            physical_collection=collection_name,
            source_uri=source_uri,
            with_created=True,
        )
        if isinstance(_reg_result, tuple):
            _catalog_doc_id_for_batch, _fallback_created = _reg_result
        else:
            _catalog_doc_id_for_batch, _fallback_created = _reg_result, False
        if on_doc_registered is not None:
            on_doc_registered(_catalog_doc_id_for_batch, _fallback_created)
    # nexus-5xn3k.4 review follow-up (code-review-expert HIGH): this path was
    # unfenced. Resolution above already sits before the first upsert (the
    # batch loop below), so no hoist is needed here — just the begin call.
    if _catalog_doc_id_for_batch:
        _fence_begin(_catalog_doc_id_for_batch, content_hash, collection_name)

    try:
        for batch_start in range(start_offset, total, _INCREMENTAL_BATCH_SIZE):
            batch_end = min(batch_start + _INCREMENTAL_BATCH_SIZE, total)
            batch_docs = documents_all[batch_start:batch_end]
            batch_ids = ids_all[batch_start:batch_end]
            batch_metas = metadatas_all[batch_start:batch_end]

            # Embed
            if embed_fn is not None:
                embeddings, actual_model = embed_fn(batch_docs, target_model)
            else:
                from nexus.db.http_vector_client import is_vector_service_mode  # noqa: PLC0415 — circular-dep avoidance (nexus.db.http_vector_client)
                if is_vector_service_mode():
                    # RDR-152 Seam B (nexus-gmiaf.22): service embeds server-side.
                    # Pass empty embeddings; HttpVectorClient.upsert_chunks_with_embeddings
                    # ignores them and routes to /v1/vectors/upsert-chunks (JVM embeds).
                    embeddings = [[]] * len(batch_docs)
                    actual_model = target_model
                else:
                    # nexus-sghyo: non-service embedding was retired — the
                    # client no longer embeds via Voyage.
                    raise RuntimeError(
                        "non-service embedding was retired: the client no "
                        "longer embeds via Voyage. Set NX_STORAGE_BACKEND_"
                        "VECTORS=service (the default) or unset it."
                    )

            if actual_model != target_model:
                for m in batch_metas:
                    m["embedding_model"] = actual_model

            # Upsert (nexus-h8rf6.4: known chashes skip the server-side embed)
            _upsert_skip_reembed(t3, collection_name, batch_ids, batch_docs, embeddings, batch_metas, force=force)

            # RDR-108 Phase 3: inject the global chunk_index per row before
            # firing the batch chain. ``batch_metas`` came from
            # ``make_chunk_metadata`` (post-Phase-3, no chunk_index); the
            # incremental loop slices ``metadatas_all[batch_start:batch_end]``
            # so the per-row global index is ``batch_start + i``. Without
            # this injection the manifest hook defaults to a batch-local
            # enumeration that resets to 0 each batch, truncating the
            # manifest. T3 already received the post-Phase-3 metadata; the
            # local copy mutation here only affects the hook payload.
            for _i, _meta in enumerate(batch_metas):
                _meta["chunk_index"] = batch_start + _i

            # Post-store hook chains (RDR-095). Both single-doc and batch
            # chains fire from every storage event; the per-doc loop covers
            # single-shape consumers on CLI ingest.
            #
            # nexus-uxg4u round 2 (code-review-expert Finding A /
            # substantive-critic ship-blocker-adjacent): gated on dry_run
            # explicitly here too -- default hooks (install_default_hooks,
            # reached whenever a caller passes hooks=None) wire
            # aspect_extraction_enqueue_hook, a REAL T2 write. The CLI's
            # own empty-HookRegistry() convention is not something dry_run
            # itself enforces; a direct caller with hooks=None must not
            # get a real T2/manifest write on a dry run.
            if hooks is None:
                from nexus.hook_registry import HookRegistry, install_default_hooks  # noqa: PLC0415 — circular-dep avoidance: deferred intra-package import
                hooks = HookRegistry()
                install_default_hooks(hooks)
            if not dry_run:
                hooks.fire_batch(
                    batch_ids, collection_name, batch_docs, embeddings, batch_metas,
                    catalog_doc_id=_catalog_doc_id_for_batch,
                )
                for _did, _doc in zip(batch_ids, batch_docs):
                    hooks.fire_single(_did, collection_name, _doc)

            # Checkpoint
            write_checkpoint(CheckpointData(
                pdf=str(file_path),
                collection=collection_name,
                content_hash=content_hash,
                chunks_upserted=batch_end,
                total_chunks=total,
                embedding_model=target_model,
            ))

            if on_progress:
                on_progress(batch_end, total)
    except Exception as exc:
        # _fence_fail never raises, so the original exception always
        # propagates unmasked (nexus-5xn3k.4 review follow-up). Over-work,
        # never under-work: fire_batch already fired per-landed-increment
        # above, so those manifest rows are real even though the run as a
        # whole did not finish.
        if _catalog_doc_id_for_batch:
            _fence_fail(_catalog_doc_id_for_batch, str(exc))
        raise

    # nexus-tbkk1: stale-chunk prune via _identity_where's source_path
    # fallback DELETED as dead code — same rationale as _index_document's
    # former prune block above (RDR-102 D2 removed source_path from
    # make_chunk_metadata; this where-clause always matched zero rows).
    # Closes only the doc_indexer.py/pipeline_stages.py HALF of RDR-102
    # D2's "Phase 5b" — the indexer.py/indexer_utils.py siblings were
    # audited and deleted by nexus-afudo (2026-08-05); Phase 5b is now
    # fully closed. Automatic replacement protection is
    # mcp_infra._sweep_superseded_vectors, proven end-to-end at tests/
    # integration/test_tp8yk_manifest_never_outruns_chunks.py::
    # test_union_guard_keeps_shared_chunk_at_the_production_wiring — not
    # comprehensive for manifest-absent legacy rows; nx t3 gc (src/nexus/
    # commands/t3.py:219) is the comprehensive manual backstop. Full
    # evidence: _identity_where's docstring above.

    # Clean up checkpoint on success
    delete_checkpoint(content_hash, collection_name)

    # nexus-5xn3k.4 review follow-up: this path is multi-batch (fire_batch
    # per increment), so completion is an explicit call with the run's
    # total chunk count and the SAME content_hash threaded from the
    # caller (hash-once) — never a manifest_complete ride claim, which is
    # only sound for genuinely single-flush callers. Zero-chunks routes to
    # fail, never a trivially-satisfied /complete(0).
    if _catalog_doc_id_for_batch:
        if total == 0:
            _fence_fail(_catalog_doc_id_for_batch, "zero chunks extracted")
        else:
            _fence_complete(_catalog_doc_id_for_batch, content_hash, total)
    return total


def _pdf_chunks(
    pdf_path: Path,
    content_hash: str,
    target_model: str,
    now_iso: str,
    corpus: str,
    *,
    chunk_chars: int | None = None,
    bib_enrich_enabled: bool = False,
    extractor: str = "auto",
    on_formula_oom: str = "fail",
    git_meta: dict | None = None,
    doc_id: str = "",
    allow_degraded_extraction: bool = False,
) -> list[tuple[str, str, dict]]:
    """Chunk a PDF and return (id, text, metadata) tuples.

    *chunk_chars* overrides the default chunk size (1500 chars).  When None
    the PDFChunker default is used.  Pass ``tuning.pdf_chunk_chars`` from
    TuningConfig to honour per-repo configuration.

    *bib_enrich_enabled* controls whether Semantic Scholar is queried for
    bibliographic metadata (year, venue, authors, citation count).  Disable
    for offline/air-gapped environments or bulk indexing.

    *extractor* selects the PDF extraction backend (``"auto"``, ``"docling"``,
    or ``"mineru"``).

    *allow_degraded_extraction* (nexus-wi1uv) forwarded to
    :meth:`PDFExtractor.extract` — bypasses the post-extraction quality
    gate that fails loud on space-stripped/garbage extraction output.
    Default ``False``.

    *git_meta* — flat ``git_*`` provenance dict. When ``None`` the function
    auto-detects via :func:`nexus.indexer_utils.detect_git_metadata` from
    ``pdf_path``. Pass an explicit value when the caller has already
    resolved it (the repo-walk path does this once per repo). Empty dict
    when *pdf_path* is not in a git repository — :func:`normalize` then
    omits ``git_meta`` per the empty-set rule (nexus-2my fix #3).
    """
    if git_meta is None:
        from nexus.indexer_utils import detect_git_metadata  # noqa: PLC0415 — circular-dep avoidance: deferred intra-package import
        git_meta = detect_git_metadata(pdf_path)
    result = PDFExtractor().extract(
        pdf_path, extractor=extractor, on_formula_oom=on_formula_oom,
        allow_degraded=allow_degraded_extraction,
    )
    chunker = PDFChunker(chunk_chars=chunk_chars) if chunk_chars is not None else PDFChunker()
    chunks = chunker.chunk(result.text, result.metadata)
    if not chunks:
        if result.text.strip():
            # nexus-aold: text was extracted but the chunker produced zero chunks.
            # Pre-fix this fell through to a silent ``return []`` which the
            # indexer reported as success-with-0-records (invisible failure).
            raise RuntimeError(
                f"chunker produced zero chunks for {pdf_path.name} despite "
                f"non-empty extracted text ({len(result.text)} chars, "
                f"extraction_method={result.metadata.get('extraction_method', 'unknown')}). "
                "This usually indicates a chunker bug or a mismatch between "
                "extractor output and chunker expectations; rerun with "
                "--extractor mineru or file a bug with the source PDF."
            )
        return []

    # Heuristic: fewer than 20 chars per page suggests a scanned/image-only PDF.
    # Per-page normalisation avoids false positives on short-but-real documents.
    _page_count = result.metadata.get("page_count", 1) or 1
    is_image_pdf = (len(result.text) / _page_count) < 20
    has_formulas = result.metadata.get("formula_count", 0) > 0

    # Compute source_title once before the loop so bib lookup uses the same value.
    # nexus-8l6 fallback: extractor metadata wins; otherwise derive from
    # first H1 or normalised filename (preserves initialisms like RDR, API).
    from nexus.indexer_utils import resolve_pdf_title  # noqa: PLC0415 — circular-dep avoidance: deferred intra-package import
    source_title = resolve_pdf_title(result.metadata, pdf_path, result.text)
    bib: dict = {}
    if bib_enrich_enabled:
        from nexus.bib_enricher import enrich as bib_enrich  # noqa: PLC0415 — circular-dep avoidance: deferred intra-package import
        bib = bib_enrich(source_title)

    from nexus.metadata_schema import make_chunk_metadata  # noqa: PLC0415 — circular-dep avoidance (nexus.metadata_schema)

    prepared: list[tuple[str, str, dict]] = []
    for chunk in chunks:
        # ``chunk_id`` is the per-chunk Chroma natural-id:
        # ``chunk_text_hash[:32]`` per RDR-108 D1 (nexus-kmb6).
        chunk_text_hash_full = hashlib.sha256(chunk.text.encode()).hexdigest()
        chunk_id = _chunk_id_from_hash(chunk_text_hash_full)  # nexus-4pvho
        # RDR-101 Phase 5c dropped corpus, store_type, git_meta. Title kept.
        # RDR-108 Phase 3 dropped chunk_index, chunk_count, doc_id;
        # catalog manifest is authoritative.
        meta = make_chunk_metadata(
            content_type="pdf",
            chunk_text_hash=chunk_text_hash_full,
            content_hash=content_hash,
            chunk_start_char=chunk.metadata.get("chunk_start_char", 0),
            chunk_end_char=chunk.metadata.get("chunk_end_char", 0),
            page_number=chunk.metadata.get("page_number", 0),
            indexed_at=now_iso,
            embedding_model=target_model,
            title=source_title,
            source_author=result.metadata.get("pdf_author", ""),
            section_title=chunk.metadata.get("section_title", ""),
            section_type=chunk.metadata.get("section_type", ""),
            tags="pdf",
            category="paper",
            bib_year=bib.get("year", 0),
            bib_authors=bib.get("authors", ""),
            bib_venue=bib.get("venue", ""),
            bib_citation_count=bib.get("citation_count", 0),
            extraction_method=result.metadata.get("extraction_method", ""),
            quality_gate_overridden=bool(result.metadata.get("quality_gate_overridden", False)),
        )
        prepared.append((chunk_id, chunk.text, meta))
    return prepared


def _markdown_chunks(
    md_path: Path,
    content_hash: str,
    target_model: str,
    now_iso: str,
    corpus: str,
    *,
    base_path: Path | None = None,
    git_meta: dict | None = None,
    doc_id: str = "",
    extraction_source: str = "file",
) -> list[tuple[str, str, dict]]:
    """Chunk a Markdown file and return (id, text, metadata) tuples.

    *git_meta* — flat ``git_*`` provenance dict. ``None`` triggers
    auto-detection from *md_path* via
    :func:`nexus.indexer_utils.detect_git_metadata`. Empty dict outside
    a git repo (nexus-2my fix #3).
    """
    from nexus.catalog.types import make_relative  # noqa: PLC0415 — circular-dep avoidance: deferred intra-package import

    if git_meta is None:
        from nexus.indexer_utils import detect_git_metadata  # noqa: PLC0415 — circular-dep avoidance: deferred intra-package import
        git_meta = detect_git_metadata(md_path)

    raw_text = md_path.read_text(encoding="utf-8")
    frontmatter, body = parse_frontmatter(raw_text, source=str(md_path), strict=True)
    frontmatter_len = len(raw_text) - len(body)

    # RDR-102 D2: source_path is no longer carried at any layer of the
    # chunk-write path (schema-removed). The chunker's base_metadata
    # spread (md_chunker.py:380) propagates whatever is in this dict to
    # each chunk's intermediate metadata, but ``_markdown_chunks``
    # builds the final T3 metadata from scratch via
    # ``make_chunk_metadata`` and reads only chunk_start_char /
    # chunk_end_char / header_path / section_type from
    # ``chunk.metadata`` — source_path was always dead in the output.
    base_meta: dict = {
        "corpus": corpus,
    }
    chunks = SemanticMarkdownChunker().chunk(body, base_meta)
    if not chunks:
        return []

    # nexus-8l6: source_title fallback chain. Frontmatter ``title:`` wins;
    # otherwise derive from the first H1 or the normalised filename so
    # ``nx store list`` never displays ``untitled``.
    from nexus.indexer_utils import derive_title  # noqa: PLC0415 — circular-dep avoidance: deferred intra-package import
    source_title = (
        str(frontmatter.get("title") or "").strip()
        or derive_title(md_path, body)
    )

    from nexus.metadata_schema import make_chunk_metadata  # noqa: PLC0415 — circular-dep avoidance (nexus.metadata_schema)

    prepared: list[tuple[str, str, dict]] = []
    for chunk in chunks:
        # ``chunk_id`` is the per-chunk Chroma natural-id:
        # ``chunk_text_hash[:32]`` per RDR-108 D1 (nexus-kmb6).
        chunk_text_hash_full = hashlib.sha256(chunk.text.encode()).hexdigest()
        chunk_id = _chunk_id_from_hash(chunk_text_hash_full)  # nexus-4pvho
        # RDR-101 Phase 5c dropped corpus, store_type, git_meta. Title kept.
        # RDR-108 Phase 3 dropped chunk_index, chunk_count, doc_id;
        # catalog manifest is authoritative.
        meta = make_chunk_metadata(
            content_type="markdown",
            chunk_text_hash=chunk_text_hash_full,
            content_hash=content_hash,
            chunk_start_char=chunk.metadata.get("chunk_start_char", 0) + frontmatter_len,
            chunk_end_char=chunk.metadata.get("chunk_end_char", 0) + frontmatter_len,
            page_number=chunk.metadata.get("page_number", 0),
            indexed_at=now_iso,
            embedding_model=target_model,
            title=source_title,
            source_author=str(frontmatter.get("author", "")),
            section_title=chunk.metadata.get("header_path", ""),
            section_type=chunk.metadata.get("section_type", ""),
            tags="markdown",
            category="prose",
            extraction_source=extraction_source,
        )
        prepared.append((chunk_id, chunk.text, meta))
    return prepared


def index_pdf(
    pdf_path: Path,
    corpus: str,
    t3: Any = None,
    *,
    collection_name: str | None = None,
    embed_fn: EmbedFn | None = None,
    force: bool = False,
    return_metadata: bool = False,
    on_progress: Callable[[int, int], None] | None = None,
    enrich: bool = False,
    extractor: str = "auto",
    on_formula_oom: str = "fail",
    streaming: str = "auto",
    hooks: "HookRegistry | None" = None,
    source_uri: str = "",
    on_fork_detected: Callable[[list[tuple[str, int]]], None] | None = None,
    allow_degraded_extraction: bool = False,
    dry_run: bool = False,
) -> int | dict:
    """Index *pdf_path* into a T3 collection.

    By default the collection is ``docs__{corpus}``.  Pass *collection_name*
    to override (e.g. ``knowledge__delos`` for external reference corpora).

    Returns the number of chunks indexed, or 0 if skipped (no credentials or
    content unchanged since last index with the same embedding model).

    Pass *embed_fn* to override the default server-side embedding (e.g. a
    local ONNX function for dry-run mode).

    Pass *force=True* to bypass the staleness check and always re-index.

    When *return_metadata* is True, returns a dict instead of an int::

        {"chunks": int, "pages": list[int], "title": str, "author": str}

    Metadata is derived from chunk metadatas produced during extraction
    (no additional T3 query) on the batch and incremental paths.  The
    streaming path (nexus-w6wp0) does one additional content_hash-keyed T3
    query after upload, since streaming discards per-chunk metadata as it
    flushes and does not retain a full in-process list to derive from.
    Default False preserves existing int behavior.

    Pass *enrich=True* to enable Semantic Scholar bibliographic metadata
    lookup (year, venue, authors, citations).  Default is False (opt-in)
    to avoid network calls in offline/air-gapped environments.  Use
    ``nx enrich <collection>`` for deliberate backfill.

    Pass *source_uri* (nexus-y8qtj) to resolve catalog identity by URI
    (e.g. ``x-devonthink-item://<UUID>``) INSTEAD of by file path — the
    fix for path-based re-indexing forking a second Document when the
    original was registered under an out-of-band identity. Fail-loud: a
    *source_uri* that resolves to no live document raises
    :class:`~nexus.errors.SourceUriNotFoundError` (never falls back to
    registering new); a *source_uri* resolving to a document in a
    DIFFERENT collection than this run targets raises
    :class:`~nexus.errors.SourceUriCollectionMismatchError`.

    Pass *on_fork_detected* to receive the result of the end-of-run
    document-fork check (see :func:`_check_document_fork`) as
    ``[(other_doc_id, shared_chunk_count), ...]`` — empty when no fork is
    suspected. The check always runs and always logs a structlog WARNING
    for any hit regardless of whether a callback is given; the callback
    exists so the CLI can fold a count into its run summary without a
    second catalog round-trip.

    Pass *allow_degraded_extraction=True* (nexus-wi1uv) to bypass the
    post-extraction text-quality gate (see
    :func:`nexus.pdf_extractor.assess_extraction_quality`) that otherwise
    fails loud on space-stripped/garbage extraction output — e.g. docling
    completing "successfully" on a formula-dense page but running every
    word together. Default ``False``; surfaced as
    ``--allow-degraded-extraction`` on ``nx index pdf``. Threaded to both
    the streaming pipeline and the batch/incremental path below, since
    either can be selected depending on *streaming* and page count.

    Pass *dry_run=True* (nexus-uxg4u) to preview extraction and chunking
    ONLY — no catalog registration, no fence writes, no catalog hook
    registration/linking. The pre-flight catalog registration below is
    skipped entirely (``doc_id`` stays ``""``) rather than inferred from
    *embed_fn*, so the preview never mints a phantom catalog Document
    that a subsequent (never-taken, dry-run) completion could refuse.
    Extraction + chunking counts are unaffected. When *dry_run=False*
    (the default) and this call's own pre-flight registration MINTED a
    brand-new Document (vs. finding a pre-existing one), a failure
    anywhere downstream — most notably :class:`~nexus.errors.
    IndexRunVerifyRefused` from the completion fence — rolls that fresh
    registration back via :func:`nexus.catalog.store_hook.
    rollback_minted_catalog_entry` before re-raising; a run against a
    pre-existing document is left exactly as the fence marked it
    (``_fence_fail``), never rolled back.
    """
    from functools import partial  # noqa: PLC0415 — deliberate deferred import: branch-local / startup-cost avoidance

    _empty_meta = {"chunks": 0, "pages": [], "title": "", "author": ""}
    # GH #336 mirror: same local-fallback semantics as ``_index_document``.
    # RDR-152 Seam B (nexus-gmiaf.22): service mode checked FIRST (same
    # ordering fix as _index_document — prevents CredentialsMissingError on
    # a service-mode node with no Voyage/Chroma creds).
    local_target_model: str | None = None
    if embed_fn is None:
        from nexus.db.http_vector_client import is_vector_service_mode  # noqa: PLC0415 — circular-dep avoidance (nexus.db.http_vector_client)

        if is_vector_service_mode():
            # Service embeds server-side. embed_fn stays None; the upsert
            # site's stub branch handles it. No creds / no local ONNX.
            pass
        else:
            from nexus.config import is_local_mode  # noqa: PLC0415 — circular-dep avoidance (nexus.config)

            if is_local_mode():
                embed_fn, local_target_model = _make_local_embed_fn()
            else:
                # nexus-sghyo: non-service, non-local cloud-mode ingestion
                # was retired — the client no longer embeds via Voyage
                # (Hal determination 2026-07-28). Fail loud instead of
                # falling through to a dead credential-driven embed path.
                from nexus.errors import CredentialsMissingError  # noqa: PLC0415 — circular-dep avoidance (nexus.errors)

                raise CredentialsMissingError(
                    "non-service cloud-mode ingestion was retired: the client "
                    "no longer embeds via Voyage. Unset NX_STORAGE_BACKEND_"
                    "VECTORS (service mode is the default) or set NX_LOCAL=1 "
                    "for local-mode ingestion (no API keys needed)."
                )

    # Normalize to absolute so staleness checks are path-form-independent.
    pdf_path = pdf_path.resolve()

    # nexus-1sd0f (round 3, substantive-critic round-2 verification,
    # 2026-08-17): a zero-byte PDF can never yield extracted text/pages,
    # so registering a catalog document ahead of that outcome (below)
    # mints a permanent chunk_count=0 phantom no re-index can ever
    # clear -- the identical mechanism index_markdown's round-2 guard
    # already closes for md/rdr (nexus-rqsh1). Unlike index_markdown,
    # this is zero-byte ONLY: PDFs are legitimately binary content, so
    # looks_like_binary_content (a UTF-8 text sniff) does not apply
    # here -- every real PDF would misclassify as "binary" under that
    # check, which is not the defect this bead is about. Malformed-but-
    # nonempty PDF extraction failures (docling/mineru/pymupdf errors)
    # are a separate, already-handled concern (ExtractionQualityError /
    # IndexingError below) and are out of scope for this guard.
    from nexus.errors import UnchunkableContentError  # noqa: PLC0415 — circular-dep avoidance (nexus.errors)
    try:
        _pdf_size = pdf_path.stat().st_size
    except OSError as exc:
        raise UnchunkableContentError(f"cannot stat {pdf_path}: {exc}") from exc
    if _pdf_size == 0:
        raise UnchunkableContentError(
            f"{pdf_path} is empty (0 bytes) and cannot be chunked"
        )

    content_hash = _sha256(pdf_path)
    # RDR-152 Seam B: resolve the write client BEFORE the collection-name
    # fallback below (moved up, nexus-o5x2c) — see _index_document /
    # _resolve_write_db for the full rationale.
    db = _resolve_write_db(t3)
    # RDR-103 Phase 5 leaf fallback (see _index_document for the
    # full rationale). Synthesises a conformant 4-segment name for
    # ad-hoc invocations; production hot paths always pass
    # ``collection_name``.
    if collection_name is not None:
        col_name = collection_name
    else:
        from nexus.corpus import docs_leaf_fallback_collection_name  # noqa: PLC0415 — circular-dep avoidance (nexus.corpus)
        # nexus-o5x2c: grandfather probe — see _index_document.
        col_name = docs_leaf_fallback_collection_name(
            corpus, collection_exists=lambda name: db.collection_exists(name),
        )
    col = db.get_or_create_collection(col_name)
    target_model = index_model_for_collection(col_name)
    if local_target_model is not None:
        # See _index_document for rationale: keep the staleness check
        # + chunk metadata aligned with the local embedder's actual
        # model so repeat-indexes are no-ops.
        target_model = local_target_model

    # RDR-102 Phase A: pre-flight catalog registration. Resolve doc_id BEFORE
    # the staleness check so a fresh index lands chunks with doc_id populated
    # at write time (not via ChromaDB's undocumented upsert metadata-merge).
    # Idempotent on re-index via Catalog.register's by_file_path early-return
    # (no duplicate DocumentRegistered events). Returns "" when the catalog
    # is absent — preserves the no-catalog ingest contract.
    #
    # ``content_type="paper"`` here is the CATALOG content_type (used by
    # ``_register_or_lookup_doc_id`` to derive the curator owner default
    # ``standalone-pdfs`` and to populate ``Document.content_type``).
    # The chunk-metadata content_type at write time below is ``"pdf"``
    # (set inside ``_pdf_chunks`` via ``make_chunk_metadata(content_type=
    # "pdf", ...)``). The two namespaces are distinct: catalog tracks
    # source typing; chunk metadata tracks T3 routing.
    if dry_run:
        # nexus-uxg4u: a dry run must never touch the catalog — this is
        # the phantom-registration bug this flag exists to close. Gated
        # on an explicit flag, never inferred from embed_fn/t3 shape.
        doc_id = ""
        _doc_id_freshly_minted = False
    else:
        # nexus-uxg4u: defensive unpacking — a test double patching this
        # function (widespread across the suite, pre-dating this fix)
        # returns a bare ``str`` regardless of *with_created*, since a
        # mock/lambda ignores kwargs it doesn't know about. Treat a
        # non-tuple result as "not freshly minted" rather than raising a
        # ValueError on unpack: safe for every existing double (rollback
        # simply never fires for them, which matches what they already
        # assert) and exact for the real function, which always returns
        # a 2-tuple when with_created=True.
        _reg_result = _register_or_lookup_doc_id(
            pdf_path, corpus,
            content_type="paper",
            physical_collection=col_name,
            source_uri=source_uri,
            with_created=True,
        )
        if isinstance(_reg_result, tuple):
            doc_id, _doc_id_freshly_minted = _reg_result
        else:
            doc_id, _doc_id_freshly_minted = _reg_result, False

    # nexus-uxg4u round 2 (code-review-expert Finding B / substantive-critic
    # SIGNIFICANT): the pre-flight call above is not the ONLY place THIS
    # call can mint a fresh Document. `_register_or_lookup_doc_id` returns
    # ("", False) on the worktree/tempdir ephemeral-skip path and on its
    # own best-effort exception swallow — when either happens, doc_id=""
    # flows into pipeline_index_pdf / _index_pdf_incremental with
    # dry_run=False, and BOTH have their own `if not doc_id:` fallback
    # registration that can independently mint. The small-doc branch's
    # second, nominally-idempotent lookup call (`_catalog_doc_id_for_batch`
    # below) can too, for the identical reason. A plain local variable
    # captured once here cannot see any of those — a MUTABLE state dict,
    # updated via `_note_fallback_mint` from wherever a fallback mint
    # actually happens (including across the pipeline_stages.py module
    # boundary via the `on_doc_registered` callback), is what lets the one
    # rollback closure below cover every mint this call can make, not just
    # the pre-flight's own.
    _mint_state: dict[str, object] = {
        "doc_id": doc_id, "freshly_minted": _doc_id_freshly_minted,
    }

    def _note_fallback_mint(fallback_doc_id: str, created: bool) -> None:
        """Record a fallback registration performed by THIS call, wherever
        it happened. Only ever called when the site's own `if not doc_id:`
        (or, for the small-doc branch's second call, an explicit
        `if created:`) guard already established the pre-flight above did
        NOT mint — so this never downgrades a genuine pre-flight mint,
        only supplies information the pre-flight had none of.
        """
        _mint_state["doc_id"] = fallback_doc_id
        _mint_state["freshly_minted"] = created

    def _rollback_if_freshly_minted(exc: BaseException) -> None:
        """nexus-uxg4u task 2 (+ round 2 fallback-mint coverage): undo a
        registration THIS call minted — whether at the pre-flight above or
        at a callee's fallback site — when the run fails downstream (e.g.
        ``IndexRunVerifyRefused`` from the completion fence, or any other
        exception). A pre-existing document (``freshly_minted=False``) is
        left exactly as the fence marked it — never a rollback candidate,
        since deleting it could discard someone else's prior, unrelated
        indexing work. Never masks *exc* — this is a best-effort
        compensation the caller re-raises past regardless of outcome.
        """
        _mint_doc_id = _mint_state["doc_id"]
        if dry_run or not _mint_state["freshly_minted"] or not _mint_doc_id:
            return
        from nexus.catalog.store_hook import rollback_minted_catalog_entry  # noqa: PLC0415 — circular-dep avoidance (nexus.catalog.store_hook)
        rollback_minted_catalog_entry(_mint_doc_id, original_error=str(exc))
        _log.warning(
            "index_pdf_fresh_registration_rolled_back",
            doc_id=_mint_doc_id, pdf=str(pdf_path), reason=str(exc),
        )

    # Incremental sync: skip if file is already indexed with the same hash AND model.
    # nexus-dcym: prefer doc_id-keyed lookup; content_hash fallback when
    # the catalog is absent (RDR-101 Phase 5c — source_path is gone).
    incremental_where = _identity_where(str(pdf_path), corpus, content_hash=content_hash)
    existing = _vector_with_retry(
        col.get,
        where=incremental_where,
        include=["metadatas"],
        limit=1,
    )
    if not force and existing["metadatas"]:
        stored_hash = existing["metadatas"][0].get("content_hash", "")
        stored_model = existing["metadatas"][0].get("embedding_model", "")
        # nexus-5xn3k AC2 / RUNFENCE (nexus-5xn3k.3): the hash/model match
        # above is satisfied by ONE surviving chunk (limit=1), so a mid-write
        # failure leaves the document permanently "fresh". The three-way
        # fence check (index_state == 'complete' + hash match -> skip;
        # 'indexing'/'failed' -> re-index; unknown -> one engine
        # manifest/verify call) closes that gap.
        if (
            stored_hash == content_hash
            and stored_model == target_model
            and _index_run_fresh(col, doc_id, content_hash)
        ):
            if return_metadata:
                return {"chunks": 0, "pages": [], "title": "", "author": ""}
            return 0

    # Streaming pipeline routing: check page count before full extraction.
    if streaming in ("auto", "always"):
        try:
            import pymupdf  # noqa: PLC0415 — heavy/optional dep (pymupdf) deferred to call time to keep module import cheap
            with pymupdf.open(str(pdf_path)) as _doc:
                page_count = len(_doc)
        except Exception:  # noqa: BLE001 — best-effort page-count probe; falls through to batch path on failure
            page_count = -1  # can't open PDF — fall through to batch path
        use_streaming = streaming == "always" or (page_count >= 0 and page_count >= _STREAMING_THRESHOLD)
        if use_streaming:
            from nexus.pipeline_stages import pipeline_index_pdf  # noqa: PLC0415 — circular-dep avoidance: deferred intra-package import
            # Returns 0 if skipped (already completed by another process).
            # The staleness check above (line 638-644) handles the "unchanged"
            # case. nexus-lcmbp: a concurrent, still-fresh 'running' pipeline
            # is NOT a 0 here — pipeline_index_pdf lets PipelineConflictRunning
            # propagate instead, so a stranded-row retry is a loud failure,
            # never a silent 0-chunk "success".
            try:
                count = pipeline_index_pdf(
                    pdf_path, content_hash, col_name, db,
                    embed_fn=embed_fn, extractor=extractor, on_formula_oom=on_formula_oom,
                    corpus=corpus, target_model=target_model,
                    force=force,
                    doc_id=doc_id,
                    hooks=hooks,
                    source_uri=source_uri,
                    allow_degraded_extraction=allow_degraded_extraction,
                    dry_run=dry_run,
                    on_doc_registered=_note_fallback_mint,
                )
            except Exception as exc:
                _rollback_if_freshly_minted(exc)
                raise
            # nexus-y8qtj: end-of-run fork check. Best-effort, always runs;
            # the callback (if any) lets the CLI fold a count into its
            # summary without a second catalog round-trip.
            _forks = _check_document_fork(doc_id, col_name)
            if on_fork_detected is not None:
                on_fork_detected(_forks)
            if return_metadata:
                # nexus-w6wp0: query T3 for metadata after streaming upload.
                # NOT _identity_where's no-hash source_path fallback, which
                # RDR-102 D2 (2026-05-02, commit 83ac62c7) made permanently
                # unable to match any chunk written since (make_chunk_
                # metadata dropped source_path entirely; see
                # _identity_where's docstring / nexus-tbkk1).
                #
                # Review round (code-review-expert + substantive-critic,
                # 2026-08-05, verified round 2) Critical-2: a bare
                # content_hash-keyed query is scoped to the whole
                # COLLECTION, not to this document -- it can pick up rows
                # from a DIFFERENT, DISJOINT-or-partially-overlapping
                # registration event that happens to share this
                # content_hash (e.g. a stale/orphaned leftover batch from
                # an earlier superseded write not yet swept, or a second
                # registration under a different doc_id whose manifest does
                # not actually reference these rows). When doc_id is known,
                # the catalog manifest (doc_id -> document_chunks -> chash)
                # scopes the read to exactly the rows THIS document's
                # manifest currently claims, which such decoys are not
                # part of.
                #
                # What this does NOT fix (round-2 correction -- the
                # original comment here overclaimed "collision-free"):
                # TRUE byte-identical duplicate PDFs (identical
                # content_hash implies identical per-chunk text, hence
                # identical chash SETS) resolve to the SAME shared,
                # content-deduplicated T3 rows either document's manifest
                # points at (RDR-108's chunk-level dedup: "identical chunk
                # text in the same collection collapses to one T3 row by
                # design"; see t3.py's store_put docstring and
                # test_within_collection_identical_chunks_collapse). Those
                # shared rows carry whichever registration's title/
                # source_author metadata was written LAST (last-write-
                # wins on the shared row -- t3.py ~780-783), regardless of
                # which document's manifest you scope by. Manifest-scoping
                # does not and cannot disambiguate that case differently
                # from a content_hash-keyed query, because both paths land
                # on the identical physical rows. This is acceptable for a
                # summary display (the chunk CONTENT returned is correct
                # either way; only the title/author attribution on a
                # genuine duplicate is "whoever indexed most recently"),
                # not a defect this fix introduces or needs to solve --
                # just not a case manifest-scoping helps with.
                #
                # content_hash remains the fallback ONLY when doc_id is
                # empty. Per nexus-i711w the service owns the catalog in
                # EVERY mode -- doc_id=="" here is NOT a "no catalog
                # configured" mode, it means _register_or_lookup_doc_id hit
                # an UNEXPECTED catalog-registration failure (its own
                # best-effort ``except Exception`` swallowed the error and
                # returned ""). That is an anomalous state, not a
                # supported mode, so it is logged as a warning below rather
                # than silently treated as "normal, no catalog here."
                #
                # Why not thread extraction-time metadata through in-memory
                # instead (the original brief's preferred option, and what
                # the batch/incremental branches already do)? Streaming's
                # chunker_loop discards each chunk's page_number once
                # uploaded -- it never accumulates a "pages actually
                # chunked" set in this process, only pipeline_index_pdf's
                # post-pass ``extraction_result.metadata['page_count']``
                # (total PDF pages, not "pages with chunks" -- a different,
                # less precise fact than what batch/incremental report).
                # Threading the real per-chunk data through would mean (a)
                # chunker_loop maintaining a shared page-number set across
                # its producer thread, and (b) changing pipeline_index_pdf's
                # return type from a bare ``int`` for every caller (batch
                # paths, tests in pipeline_stages.py/test_pipeline_stages.py
                # -- currently unverifiable in this session: the T2 engine
                # substrate those tests need is mid-rebuild by a concurrent
                # sibling editing service/). That is real surface in
                # pipeline_stages.py, outside this bead's granted file
                # scope (doc_indexer.py + its tests only) in a shared tree.
                # The manifest-scoped read below gets the same collision-
                # safety with zero pipeline_stages.py changes -- the
                # smaller, honest fix for this bead; threading extraction
                # metadata through is a legitimate follow-up with its own
                # scope.
                #
                # Residual race (SIGNIFICANT, substantive-critic): pipeline_
                # stages.pipeline_index_pdf's force=True pre-flight deletes
                # T3 chunks matching this content_hash before a *concurrent*
                # force re-run's own pipeline executes. Such a concurrent
                # run targets the SAME content_hash (same document, same
                # content) and so could delete this run's just-written
                # chunks between this read's manifest fetch and its
                # ids-query, racing the guard below into a false failure.
                # This is not new: the pre-existing staleness-check read
                # above and _check_document_fork's own manifest read carry
                # the identical non-atomicity. Not fixed here (would need
                # pipeline-side locking, out of scope for a read-path bug
                # fix); documented honestly rather than silently accepted.
                if doc_id:
                    all_meta = _metadata_for_doc_id(col, doc_id)
                else:
                    # nexus-w6wp0 round 2: doc_id=="" here means catalog
                    # REGISTRATION FAILED for this run (nexus-i711w: the
                    # service owns the catalog in every mode; there is no
                    # "no catalog configured" mode to fall back from) --
                    # log it as the anomaly it is rather than silently
                    # treating it as an ordinary, supported path.
                    _log.warning(
                        "index_pdf_metadata_read_no_doc_id",
                        pdf=str(pdf_path),
                        content_hash=content_hash,
                        reason="catalog registration failed or returned no doc_id; "
                               "falling back to a content_hash-scoped metadata read",
                    )
                    meta_where = _identity_where(str(pdf_path), corpus, content_hash=content_hash)
                    all_meta = []
                    offset = 0
                    while True:
                        batch = _vector_with_retry(
                            col.get,
                            where=meta_where,
                            include=["metadatas"],
                            limit=300,
                            offset=offset,
                        )
                        all_meta.extend(batch.get("metadatas", []))
                        if len(batch.get("ids", [])) < 300:
                            break
                        offset += 300
                if count and not all_meta:
                    # FAIL LOUD (nexus-w6wp0, reworded round 2): indexing
                    # itself SUCCEEDED -- the pipeline already reports
                    # *count* chunks committed to T3 -- this is a failure
                    # of the METADATA DISPLAY READ only (pages/title/author
                    # for the CLI summary), most likely a concurrent
                    # re-index race (e.g. a --force re-run deleting and
                    # rewriting this content_hash's rows between this run's
                    # write and this read; see the race note above). No
                    # data was lost. Never paper over it with a silently
                    # empty pages/title/author dict, which is the original
                    # bug this fix removes -- but the message must not read
                    # as data loss either.
                    from nexus.errors import IndexingError  # noqa: PLC0415 — circular-dep avoidance (nexus.errors)

                    _verify_hint = (
                        f"'nx catalog show {doc_id}' to confirm index_state and chunk "
                        f"presence"
                        if doc_id else
                        "'nx doctor' to check for a damaged manifest (no doc_id was "
                        "resolved for this run to target a single document directly)"
                    )
                    raise IndexingError(
                        f"index_pdf: indexing succeeded ({count} chunk(s) committed to "
                        f"T3 for {pdf_path}, content_hash={content_hash}, "
                        f"doc_id={doc_id!r}), but the metadata display read found none "
                        f"of them -- likely a concurrent re-index race, not data loss. "
                        f"Remedy: re-run 'nx index pdf' (the staleness check will skip "
                        f"if nothing changed), or inspect {_verify_hint}."
                    )
                return {
                    "chunks": count,
                    "pages": sorted({m.get("page_number", 0) for m in all_meta}),
                    "title": all_meta[0].get("title", "") if all_meta else "",
                    "author": all_meta[0].get("source_author", "") if all_meta else "",
                }
            return count

    # Catalog registration helper for batch paths (streaming has its own hook)
    def _register_in_catalog(meta_list: list[dict], chunk_count: int) -> None:
        if dry_run:
            # nexus-uxg4u: never touch the catalog on a dry run.
            return
        try:
            from nexus.pipeline_stages import _catalog_pdf_hook  # noqa: PLC0415 — circular-dep avoidance: deferred intra-package import
            _catalog_pdf_hook(
                pdf_path, col_name,
                title=meta_list[0].get("title", "") if meta_list else "",
                author=meta_list[0].get("source_author", "") if meta_list else "",
                year=int(meta_list[0].get("year", 0)) if meta_list else 0,
                corpus=corpus,
                chunk_count=chunk_count,
                source_uri=source_uri,
            )
        except Exception:  # noqa: BLE001 — catalog registration is non-fatal; indexing continues
            pass  # catalog registration is non-fatal

    # Extract and chunk the entire document
    now_iso = datetime.now(UTC).isoformat()
    chunk_fn = partial(
        _pdf_chunks, bib_enrich_enabled=enrich, extractor=extractor, on_formula_oom=on_formula_oom,
        doc_id=doc_id, allow_degraded_extraction=allow_degraded_extraction,
    )
    prepared = chunk_fn(pdf_path, content_hash, target_model, now_iso, corpus)
    if not prepared:
        return _empty_meta if return_metadata else 0

    # Route: incremental for large documents, original path for small ones
    if len(prepared) > _INCREMENTAL_THRESHOLD:
        if hooks is None:
            from nexus.hook_registry import HookRegistry, install_default_hooks  # noqa: PLC0415 — circular-dep avoidance: deferred intra-package import
            hooks = HookRegistry()
            install_default_hooks(hooks)
        try:
            count = _index_pdf_incremental(
                pdf_path, corpus, prepared, content_hash, col_name, db,
                embed_fn=embed_fn, on_progress=on_progress, hooks=hooks,
                force=force,
                doc_id=doc_id,
                source_uri=source_uri,
                dry_run=dry_run,
                on_doc_registered=_note_fallback_mint,
            )
        except Exception as exc:
            _rollback_if_freshly_minted(exc)
            raise
        metadatas = [p[2] for p in prepared]
        _register_in_catalog(metadatas, len(metadatas))
        # RDR-089 document-grain chain — fires once per PDF boundary at the
        # incremental-branch tail. content="" (chunks already paginated
        # through T3); the hook reads source_path itself per the P0.1
        # content-sourcing contract.
        # nexus-tdgc: forward the catalog doc_id (lookup is post-register
        # so the entry exists by this point in the incremental path).
        #
        # nexus-uxg4u round 2: gated on dry_run -- both the fire_document
        # call itself (a hook, per code-review-expert Finding A) and the
        # catalog read that resolves its doc_id argument, which is
        # pointless work on a dry run (nothing was registered to look up).
        if not dry_run:
            from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — circular-dep avoidance (nexus.catalog.factory)
            _cat = make_catalog_reader()
            hooks.fire_document(
                str(pdf_path), col_name, "",
                doc_id=_lookup_existing_doc_id(_cat, str(pdf_path), corpus),
            )
        # nexus-y8qtj: end-of-run fork check (see the streaming branch above).
        _forks = _check_document_fork(doc_id, col_name)
        if on_fork_detected is not None:
            on_fork_detected(_forks)
        if return_metadata:
            return {
                "chunks": len(metadatas),
                "pages": sorted({m.get("page_number", 0) for m in metadatas}),
                "title": metadatas[0].get("title", "") if metadatas else "",
                "author": metadatas[0].get("source_author", "") if metadatas else "",
            }
        return count

    # Small document: use the original all-at-once path
    ids = [p[0] for p in prepared]
    documents = [p[1] for p in prepared]
    metadatas_list = [p[2] for p in prepared]

    # nexus-5xn3k.4 review follow-up (code-review-expert HIGH): this branch
    # is separate inline code, NOT routed through _index_document's
    # machinery — verified unfenced. doc_id resolution HOISTED here (was
    # previously post-upsert, mirroring the pre-.4 _index_document bug)
    # so the fence can be committed before the first byte of content
    # lands. This branch is single-flush (one upsert, one fire_batch —
    # same shape as _index_document). nexus-tp8yk D2a (substantive-critic
    # SIGNIFICANT, 2026-08-04 — this comment was stale, still describing
    # the pre-fix shape): completion no longer rides write_manifest_many's
    # optional `complete` map — that ride was structurally unreachable on
    # every real run (dcv2k: the production writer never exposes
    # write_manifest_many). It is now an explicit, PROPAGATING
    # `_fence_complete` call at this branch's tail, ~60 lines below (see
    # that call site's own comment for the full rationale).
    if hooks is None:
        from nexus.hook_registry import HookRegistry, install_default_hooks  # noqa: PLC0415 — circular-dep avoidance: deferred intra-package import
        hooks = HookRegistry()
        install_default_hooks(hooks)
    # nexus-zq79 F2: register-or-lookup (fresh indexes returned "" pre-fix).
    _ct_for_register = (metadatas_list[0].get("content_type") if metadatas_list else "") or "pdf"
    if dry_run:
        # nexus-uxg4u: never touch the catalog on a dry run — this call
        # would otherwise be idempotent onto the top-level pre-flight
        # registration above, which is itself skipped for dry_run.
        _catalog_doc_id_for_batch = ""
    else:
        # nexus-uxg4u round 2 (substantive-critic SIGNIFICANT, third
        # named fallback site): normally idempotent onto the pre-flight
        # above, but when THAT returned "" (worktree/tempdir skip, or a
        # swallowed exception) this call can independently mint fresh —
        # with_created=True + _note_fallback_mint makes that visible to
        # the rollback closure too. Only reports a mint (never a plain
        # lookup) so a genuine pre-flight mint above is never downgraded.
        _reg_result = _register_or_lookup_doc_id(
            pdf_path, corpus,
            content_type=_ct_for_register,
            physical_collection=col_name,
            source_uri=source_uri,
            with_created=True,
        )
        if isinstance(_reg_result, tuple):
            _catalog_doc_id_for_batch, _batch_created = _reg_result
        else:
            _catalog_doc_id_for_batch, _batch_created = _reg_result, False
        if _batch_created:
            _note_fallback_mint(_catalog_doc_id_for_batch, True)
    if _catalog_doc_id_for_batch:
        _fence_begin(_catalog_doc_id_for_batch, content_hash, col_name)

    try:
        if embed_fn is not None:
            embeddings, actual_model = embed_fn(documents, target_model)
        else:
            from nexus.db.http_vector_client import is_vector_service_mode  # noqa: PLC0415 — circular-dep avoidance (nexus.db.http_vector_client)
            if is_vector_service_mode():
                # RDR-152 Seam B (nexus-gmiaf.22): service embeds server-side.
                # Pass empty embeddings; HttpVectorClient.upsert_chunks_with_embeddings
                # ignores them and routes to /v1/vectors/upsert-chunks (JVM embeds).
                embeddings = [[]] * len(documents)
                actual_model = target_model
            else:
                # nexus-sghyo: non-service embedding was retired — the
                # client no longer embeds via Voyage.
                raise RuntimeError(
                    "non-service embedding was retired: the client no "
                    "longer embeds via Voyage. Set NX_STORAGE_BACKEND_"
                    "VECTORS=service (the default) or unset it."
                )
        if actual_model != target_model:
            for m in metadatas_list:
                m["embedding_model"] = actual_model
        # nexus-h8rf6.4: known chashes skip the server-side embed.
        _upsert_skip_reembed(db, col_name, ids, documents, embeddings, metadatas_list, force=force)

        # Post-store hook chains (RDR-095). Both single-doc and batch chains
        # fire from every storage event; the per-doc loop covers single-shape
        # consumers on CLI ingest.
        #
        # nexus-tp8yk D2a: this branch is single-flush (one upsert, one
        # fire_batch — same shape as _index_document). It USED TO ride
        # write_manifest_many's optional `complete` map — but the
        # production writer never exposes write_manifest_many (dcv2k), so
        # the ride never fired on any real run; completion fell through to
        # mcp_infra's per-doc `_stamp_index_run_complete`, whose refusal is
        # recorded but never propagates to the CLI (design memo §1 P1).
        # manifest_complete is now always None; the explicit, PROPAGATING
        # `_fence_complete` call below (mirroring `_index_pdf_incremental`)
        # is the completion stamp for this branch.
        #
        # nexus-uxg4u round 2 (code-review-expert Finding A / substantive-
        # critic): gated on dry_run -- these are real hook fires (default
        # hooks wire a real T2 aspect-queue write), not something the
        # empty-doc_id check below covers on its own.
        if not dry_run:
            hooks.fire_batch(
                ids, col_name, documents, embeddings, metadatas_list,
                catalog_doc_id=_catalog_doc_id_for_batch,
            )
            for _did, _doc in zip(ids, documents):
                hooks.fire_single(_did, col_name, _doc)
            # RDR-089 document-grain chain — fires once per small-doc PDF
            # boundary. content="" (full document text not retained in
            # this path); the hook reads source_path itself.
            # nexus-tdgc: forward the catalog doc_id post-register.
            hooks.fire_document(
                str(pdf_path), col_name, "",
                doc_id=_catalog_doc_id_for_batch,
            )
    except Exception as exc:
        # _fence_fail never raises, so the original exception always
        # propagates unmasked.
        if _catalog_doc_id_for_batch:
            _fence_fail(_catalog_doc_id_for_batch, str(exc))
        # nexus-uxg4u round 2 (substantive-critic ship-blocker): this
        # except path re-raises DIRECTLY out of index_pdf -- unlike the
        # streaming/incremental branches (each a separate function
        # wrapped by index_pdf's own try/except around the call site),
        # this embed/upsert/hooks block is INLINE in index_pdf with no
        # outer wrap of its own. Without this call, a freshly-minted
        # document whose embed/upsert/hooks stage fails here is left
        # behind forever (_fence_fail only marks 'failed', never
        # deletes) -- the exact reported bug, reachable via a real
        # --streaming never run on a small PDF.
        _rollback_if_freshly_minted(exc)
        raise

    # nexus-tbkk1: stale-chunk prune via _identity_where's source_path
    # fallback DELETED as dead code — same rationale as _index_document's
    # and _index_pdf_incremental's former prune blocks (RDR-102 D2
    # removed source_path from make_chunk_metadata; this where-clause
    # always matched zero rows). Closes only the doc_indexer.py/
    # pipeline_stages.py HALF of RDR-102 D2's "Phase 5b" — the indexer.py/
    # indexer_utils.py siblings were audited and deleted by nexus-afudo
    # (2026-08-05); Phase 5b is now fully closed.
    # Automatic replacement protection is mcp_infra._sweep_superseded_
    # vectors, proven end-to-end at tests/integration/test_tp8yk_
    # manifest_never_outruns_chunks.py::test_union_guard_keeps_shared_
    # chunk_at_the_production_wiring — not comprehensive for
    # manifest-absent legacy rows; nx t3 gc (src/nexus/commands/t3.py:219)
    # is the comprehensive manual backstop. Full evidence: _identity_
    # where's docstring above.

    # nexus-tp8yk D2a: explicit completion stamp, replacing the dead
    # manifest_complete ride (see the comment above `hooks.fire_batch`).
    # BEFORE catalog metadata registration, mirroring
    # `_index_pdf_incremental`'s tail ordering — a refusal here propagates
    # and the caller never reaches `_register_in_catalog` for this run,
    # exactly as the incremental branch's caller never does on refusal.
    if _catalog_doc_id_for_batch:
        try:
            _fence_complete(_catalog_doc_id_for_batch, content_hash, len(prepared))
        except Exception as exc:
            _rollback_if_freshly_minted(exc)
            raise

    _register_in_catalog(metadatas_list, len(metadatas_list))

    # nexus-y8qtj: end-of-run fork check (see the streaming branch above).
    _forks = _check_document_fork(doc_id, col_name)
    if on_fork_detected is not None:
        on_fork_detected(_forks)

    if return_metadata:
        return {
            "chunks": len(metadatas_list),
            "pages": sorted({m.get("page_number", 0) for m in metadatas_list}),
            "title": metadatas_list[0].get("source_title", "") if metadatas_list else "",
            "author": metadatas_list[0].get("source_author", "") if metadatas_list else "",
        }
    return len(prepared)


def _parse_md_title_year(md_path: Path) -> tuple[str, int]:
    """Parse frontmatter title + year for *md_path* (nexus-ivzw8).

    Returns ``(stem, 0)`` when there is no frontmatter or parsing fails —
    the same defaults the pre-flight registration used before the fix.
    Year comes from the first of ``created``/``date``/``accepted_date``.
    Extracted from the markdown hook so the PRE-FLIGHT registration can
    use the same values (the update branch's stem-guard backfill and the
    fresh-registration path must agree on the parse).
    """
    title = md_path.stem
    year = 0
    try:
        text = md_path.read_text(encoding="utf-8")
        if text.startswith("---"):
            import re  # noqa: PLC0415 — deliberate deferred import: branch-local / startup-cost avoidance
            m = re.search(r"^title:\s*(.+)$", text, re.MULTILINE)
            if m:
                title = m.group(1).strip().strip('"').strip("'") or md_path.stem
            for field in ("created", "date", "accepted_date"):
                ym = re.search(rf"^{field}:\s*(.+)$", text, re.MULTILINE)
                if ym:
                    dm = re.search(r"(\d{4})", ym.group(1))
                    if dm:
                        year = int(dm.group(1))
                        break
    except Exception:  # noqa: BLE001 — best-effort parse; stem/0 defaults preserved, logged for diagnosis
        import structlog  # noqa: PLC0415 — deliberate deferred import: branch-local / startup-cost avoidance
        structlog.get_logger(__name__).debug(
            "catalog_markdown_frontmatter_parse_failed",
            path=str(md_path), exc_info=True,
        )
    return title, year


def _catalog_markdown_hook(
    md_path: Path, collection_name: str, content_type: str, corpus: str, chunk_count: int,
    *, base_path: Path | None = None, source_uri: str = "",
) -> None:
    """Register markdown document in catalog after indexing. Silently skipped if absent.

    nexus-3lswy WARNING: this registers under a SEPARATE "curator" owner
    (see ``owner_name``/``curator_owner_tumbler_by_name`` below), distinct
    from the repo owner ``indexer._catalog_hook``'s batched ``register_many``
    pass uses. Calling both hooks for the SAME file produces two catalog
    Document rows for one physical file — exactly the bug nexus-3lswy fixed
    for ``nx index repo``'s RDR path (it now routes RDR files through
    ``_index_prose_file``, which never calls this function). This function
    is still correctly reachable from ``nx collection reindex``
    (commands/collection.py) and the standalone RDR-only index command
    (commands/index.py) — those are single-collection operations that never
    also run ``_catalog_hook``'s batched pass, so no double-registration
    exists today. Do NOT wire ``_catalog_hook``'s batched pass into either
    of those call sites without ALSO removing their call into this function,
    or the double-registration bug returns.
    """
    reader = None
    writer = None
    try:
        from nexus.catalog.types import make_relative  # noqa: PLC0415 — circular-dep avoidance: deferred intra-package import
        from nexus.catalog.factory import make_catalog_reader, make_catalog_writer  # noqa: PLC0415 — circular-dep avoidance: deferred intra-package import

        # nexus-e9ru2 (sibling of nexus-f1itv): no local is_initialized
        # pre-check — in service mode the Java service owns the catalog and
        # a fresh box legitimately has no local state. make_catalog_reader()
        # returns None only in the SQLite opt-out mode when uninitialised.
        reader = make_catalog_reader()
        if reader is None:
            return
        writer = make_catalog_writer()

        # Derive title and year from frontmatter or filename (shared with the
        # pre-flight registration, nexus-ivzw8).
        title, year = _parse_md_title_year(md_path)

        owner_name = corpus if corpus else "standalone-docs"
        # Curator-only lookup — see _register_or_lookup_doc_id for
        # rationale. nexus-qnp5s: curator_owner_tumbler_by_name() is
        # implemented on both SQLite Catalog and HttpCatalogClient.
        owner_t = reader.curator_owner_tumbler_by_name(owner_name)
        if owner_t is not None:
            owner = owner_t
        else:
            owner = writer.register_owner(owner_name, "curator")

        fp = make_relative(md_path, base_path) if base_path else str(md_path)
        # Known TOCTOU window (Reviewer B/I-3): this stat happens AFTER the
        # markdown content was read for chunking earlier in the pipeline.
        # A concurrent write between content-read and this stat stores an
        # mtime newer than the indexed content, suppressing a subsequent
        # staleness flag. Proper fix requires threading source_mtime from
        # ``index_markdown``'s content-read point down to this hook. Filed
        # as a follow-up (nexus-vatx was scoped to ingest observability
        # surfaces, not data-consistency reorders).
        try:
            source_mtime = md_path.stat().st_mtime
        except OSError:
            source_mtime = 0.0
        # RDR-102 Phase A: mirror _catalog_pdf_hook's existing-vs-fresh
        # branch. The pre-flight _register_or_lookup_doc_id already wrote
        # the Document row with chunk_count=0, so the unconditional
        # cat.register() this used to do hits Catalog.register's
        # by_file_path early-return and never updates chunk_count off
        # zero. Use cat.update() on the existing tumbler to write the
        # final chunk_count + indexed_at + source_mtime; fall through to
        # cat.register() only when no row exists yet (no-pre-flight
        # branch — preserves the no-catalog ingest contract for callers
        # that bypass the public entry points).
        # nexus-y8qtj: when source_uri is known, resolve by IT first — see
        # the matching comment in ``pipeline_stages._catalog_pdf_hook`` for
        # the full rationale (by_file_path alone cannot see an out-of-band
        # identity and would mint a second Document for the same source).
        existing = reader.by_source_uri(source_uri) if source_uri else None
        if existing is None:
            existing = reader.by_file_path(owner, fp)
        if existing is None:
            # nexus-tqudo: same cross-owner blind spot as the pre-flight.
            existing = _repo_owner_document_for(reader, md_path)
        if existing is not None:
            update_kwargs: dict = dict(
                physical_collection=collection_name,
                chunk_count=chunk_count,
                indexed_at=datetime.now(UTC).isoformat(),
                source_mtime=source_mtime,
            )
            # nexus-ivzw8 stem-guard backfill: the pre-flight registers with
            # title=stem, and this branch previously never wrote title/year —
            # frontmatter values were unreachable on the standard path. Apply
            # them ONLY over the stem default (or empties) so a hand-curated
            # catalog title is never clobbered by a re-index.
            existing_title = (existing.title or "").strip()
            if title != md_path.stem and existing_title in ("", md_path.stem):
                update_kwargs["title"] = title
            if year and not getattr(existing, "year", 0):
                update_kwargs["year"] = year
            writer.update(existing.tumbler, **update_kwargs)
        else:
            # nexus-u8n4r: refuse a brand-new registration when ``fp``
            # (absolute when no ``base_path`` was threaded — the shape
            # this function's callers, ``nx collection reindex`` and the
            # standalone RDR-only index command, use) sits under an
            # agent worktree or system temp dir, unless this owner's own
            # repo_root is itself rooted there. See
            # ``nexus.repo_identity.should_skip_ephemeral_registration``.
            # An already-existing doc (the branch above) is left alone —
            # this only stops NEW pollution, not a re-index of something
            # registered before this guard existed.
            from nexus.repo_identity import (  # noqa: PLC0415 — circular-dep avoidance (nexus.repo_identity)
                canonicalize_worktree_path,
                is_worktree_or_tempdir_path,
                owner_repo_root_best_effort,
                should_skip_ephemeral_registration,
            )
            _owner_repo_root = owner_repo_root_best_effort(reader, owner)
            # nexus-kkumv: same worktree-to-primary canonicalization as
            # ``_register_or_lookup_doc_id`` above — only when the owner
            # root is not itself a deliberate worktree/tempdir throwaway,
            # and only when the primary-repo mirror exists on disk.
            if not is_worktree_or_tempdir_path(_owner_repo_root):
                _canonical_fp = canonicalize_worktree_path(fp)
                if _canonical_fp != fp and Path(_canonical_fp).is_file():
                    fp = _canonical_fp
            if should_skip_ephemeral_registration(fp, _owner_repo_root):
                _log.warning(
                    "ephemeral_path_registration_skipped",
                    path=fp, owner=str(owner), reason="worktree_or_tempdir",
                )
                from nexus.mcp_infra import _record_ephemeral_registration_skip  # noqa: PLC0415 — circular-dep avoidance (nexus.mcp_infra)
                _record_ephemeral_registration_skip(fp, str(owner), reason="worktree_or_tempdir")
                return
            writer.register(
                owner=owner, title=title, content_type=content_type,
                file_path=fp, physical_collection=collection_name,
                chunk_count=chunk_count, year=year,
                source_mtime=source_mtime,
                source_uri=source_uri,
            )
    except Exception as exc:  # noqa: BLE001 — best-effort catalog markdown hook; logged + audited, cleanup in finally
        # nexus-ou4tb (site from the e9ru2 review): an indexed markdown doc
        # that never reached the catalog is invisible to every catalog-routed
        # query — the same class as the pdf hook, which already got the
        # WARNING + audit-row treatment. Post-e9ru2 this path also fires on
        # service-down (the gate no longer pre-skips), so the DEBUG swallow
        # hid real failures.
        _log.warning("catalog_markdown_hook_failed", exc_info=True)
        from nexus.hook_registry import record_catalog_hook_failure  # noqa: PLC0415 — deferred, avoids an import cycle

        record_catalog_hook_failure(
            source_path=str(md_path), collection=collection_name or "",
            hook_name="catalog_markdown_hook", error=str(exc),
        )
    finally:
        if writer is not None:
            writer.close()
        if reader is not None:
            reader.close()  # nexus-qnp5s: HttpCatalogClient.close() is safe


def index_markdown(
    md_path: Path,
    corpus: str,
    t3: Any = None,
    *,
    collection_name: str | None = None,
    embed_fn: EmbedFn | None = None,
    force: bool = False,
    return_metadata: bool = False,
    on_progress: Callable[[int, int], None] | None = None,
    content_type: str = "prose",
    base_path: Path | None = None,
    hooks: "HookRegistry | None" = None,
    extraction_source: str = "file",
    source_uri: str = "",
    on_fork_detected: Callable[[list[tuple[str, int]]], None] | None = None,
) -> int | dict:
    """Index *md_path* into a T3 collection.

    By default the collection is ``docs__{corpus}``.  Pass *collection_name*
    to override (e.g. ``rdr__<repo>-<hash8>`` for RDR documents).

    YAML frontmatter fields (title, author, date) are stored as metadata.
    Returns the number of chunks indexed, or 0 if skipped.

    Pass *embed_fn* to override the default server-side embedding (e.g. a
    local ONNX function for dry-run mode).

    Pass *force=True* to bypass the staleness check and always re-index.

    When *return_metadata* is True, returns a dict instead of an int::

        {"chunks": int, "sections": int}

    *sections* is the count of chunks with a non-empty ``section_title``
    (i.e. produced under a heading).  Default False preserves existing int behavior.

    When *base_path* is provided, ``source_path`` in T3 chunk metadata is
    stored relative to *base_path* instead of absolute (RDR-060).

    Pass *source_uri* (nexus-y8qtj) to resolve catalog identity by URI
    INSTEAD of by file path — see :func:`index_pdf` for the full
    rationale and the two fail-loud rules
    (:class:`~nexus.errors.SourceUriNotFoundError` /
    :class:`~nexus.errors.SourceUriCollectionMismatchError`).

    Pass *on_fork_detected* to receive the end-of-run document-fork check
    result — see :func:`index_pdf`.
    """
    from functools import partial  # noqa: PLC0415 — deliberate deferred import: branch-local / startup-cost avoidance

    from nexus.catalog.types import make_relative  # noqa: PLC0415 — circular-dep avoidance: deferred intra-package import

    # Normalize to absolute so staleness checks are path-form-independent.
    md_path = md_path.resolve()

    # nexus-rqsh1 round 2 (Hal directive 2026-08-15 + substantive-critic
    # Critical, 2026-08-17): a zero-byte file yields chunks=[] from
    # ``_markdown_chunks`` (read_text() on empty content, chunker
    # returns nothing) and a binary-content file fails read_text()'s
    # UTF-8 decode -- neither can ever produce a chunk, so registering
    # a catalog document ahead of that outcome (below) would mint a
    # permanent chunk_count=0 phantom no re-index can ever clear.
    # Unlike ``nx index repo``'s bulk discovery walk (indexer.py's
    # skipped_unchunkable set, silently skipped -- an unbounded
    # population where this is expected noise), the doc_indexer family
    # (``nx index md`` / ``nx index rdr``, both routed through this
    # function) targets a file the operator named explicitly: fail
    # loud, before any catalog write, rather than silently registering
    # nothing while reporting plain success.
    from nexus.classifier import looks_like_binary_content  # noqa: PLC0415 — circular-dep avoidance (nexus.classifier)
    from nexus.errors import UnchunkableContentError  # noqa: PLC0415 — circular-dep avoidance (nexus.errors)
    try:
        _md_size = md_path.stat().st_size
    except OSError as exc:
        raise UnchunkableContentError(f"cannot stat {md_path}: {exc}") from exc
    if _md_size == 0:
        raise UnchunkableContentError(
            f"{md_path} is empty (0 bytes) and cannot be chunked"
        )
    if looks_like_binary_content(md_path):
        raise UnchunkableContentError(
            f"{md_path} looks like binary content, not markdown text, "
            "and cannot be chunked"
        )

    # RDR-103 Phase 5 leaf fallback (see _index_document for the
    # full rationale). Synthesises a conformant 4-segment name for
    # ad-hoc invocations; production hot paths always pass
    # ``collection_name``.
    if collection_name is not None:
        col_name = collection_name
    else:
        from nexus.corpus import docs_leaf_fallback_collection_name  # noqa: PLC0415 — circular-dep avoidance (nexus.corpus)
        # nexus-o5x2c: grandfather probe (see _index_document) — this is
        # the live-repro'd `nx index md` without --collection crash.
        # LAZY on purpose: _resolve_write_db(t3) must NOT run here
        # unconditionally — this line runs before _index_document's own
        # credentials check further down, and a test
        # (test_index_raises_credentials_missing_when_cloud_mode_explicit)
        # pins that a misconfigured non-service/non-local install raises
        # CredentialsMissingError WITHOUT ever touching T3. Wrapping in a
        # lambda defers both the db resolution AND the attribute access
        # until resolve_write_embedding_model's probe loop actually needs
        # it (local mode + voyage-shaped + no key) — which this
        # credentials-failure config never reaches.
        col_name = docs_leaf_fallback_collection_name(
            corpus,
            collection_exists=lambda name: _resolve_write_db(t3).collection_exists(name),
        )
    # RDR-102 Phase A: pre-flight catalog registration. Resolve doc_id BEFORE
    # _index_document's staleness check so a fresh index lands chunks with
    # doc_id populated at write time. Idempotent on re-index via
    # Catalog.register's by_file_path early-return. Returns "" when the
    # catalog is absent (no-catalog ingest contract preserved).
    # nexus-ivzw8: thread the frontmatter title/year into the PRE-FLIGHT
    # registration so a fresh Document row never carries the stem default.
    _fm_title, _fm_year = _parse_md_title_year(md_path)
    doc_id = _register_or_lookup_doc_id(
        md_path, corpus,
        content_type=content_type,
        physical_collection=col_name,
        title=_fm_title,
        year=_fm_year,
        base_path=base_path,
        source_uri=source_uri,
    )
    chunk_fn = partial(
        _markdown_chunks,
        base_path=base_path,
        doc_id=doc_id,
        extraction_source=extraction_source,
    ) if base_path else partial(
        _markdown_chunks, doc_id=doc_id, extraction_source=extraction_source,
    )
    source_key = make_relative(md_path, base_path) if base_path else None
    raw = _index_document(
        md_path, corpus, chunk_fn, t3=t3,
        collection_name=collection_name, embed_fn=embed_fn,
        force=force, return_metadata=return_metadata, on_progress=on_progress,
        source_key=source_key,
        hooks=hooks,
        doc_id=doc_id,
        source_uri=source_uri,
    )
    if not return_metadata:
        assert isinstance(raw, int)
        count = raw
        if count > 0:
            _catalog_markdown_hook(md_path, col_name, content_type, corpus, count, base_path=base_path, source_uri=source_uri)
            # nexus-y8qtj: end-of-run fork check (see index_pdf for rationale).
            _forks = _check_document_fork(doc_id, col_name)
            if on_fork_detected is not None:
                on_fork_detected(_forks)
        return count
    if not isinstance(raw, list):
        return {"chunks": 0, "sections": 0}
    metadatas: list[dict] = raw
    sections = sum(1 for m in metadatas if m.get("section_title", ""))
    if metadatas:
        _catalog_markdown_hook(md_path, col_name, content_type, corpus, len(metadatas), base_path=base_path, source_uri=source_uri)
        _forks = _check_document_fork(doc_id, col_name)
        if on_fork_detected is not None:
            on_fork_detected(_forks)
    return {"chunks": len(metadatas), "sections": sections}


def batch_index_pdfs(
    paths: list[Path],
    corpus: str,
    t3: Any = None,
    *,
    force: bool = False,
    on_file: Callable[[Path, int, float], None] | None = None,
    extractor: str = "auto",
    on_formula_oom: str = "fail",
    hooks: "HookRegistry | None" = None,
) -> dict[str, str]:
    """Index multiple PDFs sequentially, returning per-file status.

    Returns dict mapping ``str(path)`` -> ``"indexed"`` | ``"skipped"`` | ``"failed"``.
    Failures are logged and do not abort the remaining paths.

    Pass *force=True* to bypass the staleness check on every file.

    *on_file*, if provided, is called after each file as
    ``on_file(path, chunks, elapsed_s)`` where *chunks* is the number of
    chunks upserted (0 for skipped/failed) and *elapsed_s* is wall time.
    """
    results: dict[str, str] = {}
    for path in paths:
        count: int = 0
        t0 = time.monotonic()
        try:
            raw = index_pdf(path, corpus, t3=t3, force=force, extractor=extractor, on_formula_oom=on_formula_oom, hooks=hooks)
            count = raw if isinstance(raw, int) else 0
            results[str(path)] = "indexed" if count else "skipped"
        except Exception as e:  # noqa: BLE001 — best-effort path; failure surfaced via log.warning, must not crash caller
            _log.warning("batch_index_pdfs: failed", path=str(path), error=str(e))
            results[str(path)] = "failed"
        if on_file:
            on_file(path, count, time.monotonic() - t0)
    return results


def batch_index_markdowns(
    paths: list[Path],
    corpus: str,
    t3: Any = None,
    *,
    collection_name: str | None = None,
    content_type: str = "prose",
    force: bool = False,
    on_file: Callable[[Path, int, float], None] | None = None,
    base_path: Path | None = None,
    embed_fn: EmbedFn | None = None,
    hooks: "HookRegistry | None" = None,
) -> dict[str, str]:
    """Index multiple Markdown files sequentially, returning per-file status.

    Pass *collection_name* to override the default ``docs__{corpus}`` target
    (used for RDR collections).

    Pass *content_type* to set the catalog content type (default: "prose",
    use "rdr" for RDR documents).

    Returns dict mapping ``str(path)`` -> ``"indexed"`` | ``"skipped"`` | ``"failed"``.
    Failures are logged and do not abort the remaining paths.

    Pass *force=True* to bypass the staleness check on every file.

    *on_file*, if provided, is called after each file as
    ``on_file(path, chunks, elapsed_s)`` where *chunks* is the number of
    chunks upserted (0 for skipped/failed) and *elapsed_s* is wall time.

    When *base_path* is provided, ``source_path`` in T3 chunk metadata and
    catalog ``file_path`` are stored relative to *base_path* (RDR-060).
    """
    results: dict[str, str] = {}
    for path in paths:
        count: int = 0
        t0 = time.monotonic()
        try:
            raw = index_markdown(path, corpus, t3=t3, collection_name=collection_name,
                                 content_type=content_type, force=force,
                                 base_path=base_path, embed_fn=embed_fn, hooks=hooks)
            count = raw if isinstance(raw, int) else 0
            results[str(path)] = "indexed" if count else "skipped"
        except Exception as e:  # noqa: BLE001 — best-effort path; failure surfaced via log.warning, must not crash caller
            _log.warning("batch_index_markdowns: failed", path=str(path), error=str(e))
            results[str(path)] = "failed"
        if on_file:
            on_file(path, count, time.monotonic() - t0)
    return results
