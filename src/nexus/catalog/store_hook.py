# SPDX-License-Identifier: AGPL-3.0-or-later
"""Knowledge-entry catalog registration helper.

nexus-8g79.10 (V1): hosted at this lower layer so MCP infra
(``mcp/core.py``) and CLI command modules can both invoke without
the MCP layer reaching up into the CLI presentation layer.
Previously this function lived in ``commands/store.py`` and was
imported FROM ``mcp/core.py:1029`` — a layering inversion flagged
by the post-4.32.4 multi-agent audit.

Callers: ``mcp/core.py`` (MCP ``store_put`` tool),
``commands/store.py`` (``nx store put`` CLI),
``commands/memory.py`` (``nx memory promote`` CLI).
"""
from __future__ import annotations

import hashlib

import httpx
import structlog

from nexus.aspect_readers import uri_for

_log = structlog.get_logger(__name__)


def single_chunk_manifest_metadata(content: str) -> tuple[str, list[dict]]:
    """Compute the T3 natural id and manifest-hook chunk metadata for a
    single-chunk store event (MCP ``store_put`` / CLI ``nx store put``).

    Mirrors ``T3Database.put``'s single-chunk derivation (RDR-108 D1 /
    nexus-kmb6; width per RDR-180): the T3 natural id is the FULL
    ``sha256(content).hexdigest()``. ``manifest_write_batch_hook``
    (GH #1371) gets the same full hex under ``chunk_text_hash``
    (stored verbatim — the [:32] write-time truncation is retired).
    Both MCP
    ``store_put`` and CLI ``nx store put`` are single-chunk by
    construction, so ``chunk_start_char=0`` / ``chunk_end_char=len(content)``
    span the whole document and position defaults to 0 (the batch's only
    element).

    Returns ``(doc_id, metadatas)`` — *metadatas* is a 1-element list
    ready to pass straight through as the ``fire_batch`` /
    ``fire_store_chains`` ``metadatas`` argument. Without real metadata
    here ``manifest_write_batch_hook`` short-circuits on
    ``if not metadatas: return`` and no ``catalog_document_chunks``
    manifest row (nor the ``documents.chunk_count`` update) is ever
    written for these two callers (GH #1370 Defect 4b). (Historically
    this also unblocked the chash dual-write hook, which hit the same
    ``metadatas`` guard — that hook was retired by RDR-187 /
    nexus-piwya.4.)
    """
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    doc_id = content_hash  # RDR-180: the full digest IS the natural id
    metadata = {
        "chunk_text_hash": content_hash,
        "chunk_start_char": 0,
        "chunk_end_char": len(content),
    }
    return doc_id, [metadata]


def resolve_knowledge_doc_for_chash(reader, chash: str, *, log_event: str):
    """Resolve *chash* to the single store_put-origin catalog document it
    identifies, or ``None`` if there is no match or the match is ambiguous.

    nexus-5axey: ``by_doc_id`` is a TUMBLER-only lookup on the engine (the
    settled wji11 contract: tumbler is the only document identity); it
    cannot answer "which document has this content-chash in its
    ``meta.doc_id``" — that used to alias ``resolve()`` and simply mismatch
    every chash-shaped input. This is the chash-appropriate replacement,
    built on :meth:`docs_for_chashes` (chash -> ``[doc_id, ...]``, the
    reverse-manifest-lookup primitive present on every deployed engine).

    chash -> document is one-to-many in general: identical chunk text in a
    collection collapses to one T3 row, and the manifest can point many
    documents at that shared chash (RDR-108's collapsing-by-design). For
    store_put / memory-promote-origin documents specifically (identified by
    ``content_type == "knowledge"`` with no ``file_path`` — the same filter
    the pre-existing delete-path cleanup already applied) an UNAMBIGUOUS
    single match is trusted. More than one candidate is treated
    conservatively as "no safe match" — this function returns ``None`` and
    logs at WARNING — rather than acting on an arbitrary pick or every
    candidate: acting on the wrong one (or deleting/reconciling onto all of
    them) is a worse outcome than leaving one ghost row for the periodic
    ``nx catalog gc`` sweep to reap.

    *log_event* names the calling site (e.g. ``"catalog_store_hook_dedup"``)
    so the ambiguity warning is attributable to dedup vs. delete-path vs.
    tombstone-reap without three near-identical log statements.

    A malformed *chash* (not the hex digest production always produces —
    e.g. a legacy non-hex meta.doc_id) 400s at the wire rather than simply
    missing; that specific case is treated the same as a miss (``None``,
    WARNING logged). Anything else (connectivity failure, 5xx, ...)
    PROPAGATES — nexus-f1itv/ou4tb's fail-loud contract depends on this
    reaching the caller's own broad except (WARNING + audit row), not
    being silently absorbed into "proceed as if nothing existed".
    """
    try:
        by_chash = reader.docs_for_chashes([chash])
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 400:
            raise
        # 400 == the wire rejected *chash* itself (not valid hex) — a
        # malformed input can never resolve, so it is a miss, not a fault.
        _log.warning(f"{log_event}_malformed_chash", chash=chash, error=str(exc))
        return None
    matches = by_chash.get(chash, [])
    candidates = []
    for tumbler in matches:
        entry = reader.resolve(tumbler)
        if entry is not None and entry.content_type == "knowledge" and not entry.file_path:
            candidates.append(entry)
    if len(candidates) > 1:
        _log.warning(
            f"{log_event}_ambiguous_chash",
            chash=chash,
            candidate_count=len(candidates),
            tumblers=[str(c.tumbler) for c in candidates],
        )
        return None
    return candidates[0] if candidates else None


def _find_ghost_by_title(reader, owner, title: str):
    """Return an existing GHOST catalog entry under *owner* whose title
    exactly matches *title*, or ``None``.

    GH #1370 Defect 4a: a pre-existing catalog entry with the same
    title (e.g. a ghost with ``chunk_count=0`` and empty ``head_hash``,
    left behind by a pre-migration catalog or an earlier failed index)
    is invisible to :func:`resolve_knowledge_doc_for_chash` — that entry's
    ``meta.doc_id`` predates this content's hash. Without this lookup,
    ``catalog_store_hook``
    mints a brand-new document with a fresh tumbler and the ghost is
    never reconciled.

    There is no dedicated exact-title index on the catalog reader
    protocol (only ``by_file_path`` / ``by_source_uri`` have that
    shape), so this filters ``find()``'s FTS5 results — which use
    token matching, not substring matching — down to entries whose
    ``title`` is a byte-for-byte match AND whose tumbler is a
    descendant of *owner* (the "knowledge" curator owner; ``find()``
    has no owner scoping of its own, and content_type="knowledge" alone
    is not owner-specific).

    Restricted to GHOST entries (``chunk_count == 0``): reconciling
    onto a non-ghost entry would silently repoint an already-populated
    document's ``meta.doc_id`` / ``physical_collection`` at unrelated
    new content, orphaning its existing ``document_chunks`` manifest
    rows — a worse outcome than the duplicate-entry bug being fixed
    here. A same-titled non-ghost match therefore falls through to
    ``register()`` exactly as before.

    Skipped (returns ``None`` immediately) when *title* is empty — an
    empty title must never match arbitrary same-titled ("") entries.
    """
    if not title:
        return None
    for entry in reader.find(title, content_type="knowledge"):
        if entry.title == title and entry.chunk_count == 0 and owner.is_prefix_of(entry.tumbler):
            return entry
    return None


def catalog_store_hook(
    title: str, doc_id: str, collection_name: str,
) -> str:
    """Back-compat wrapper over :func:`catalog_store_hook_tracked`.

    Returns only the tumbler string; callers that need to know whether
    the row was minted in this call (nexus-b6enc C2 ghost-register
    compensation) use the tracked variant directly.
    """
    tumbler, _created = catalog_store_hook_tracked(title, doc_id, collection_name)
    return tumbler


def catalog_store_hook_tracked(
    title: str, doc_id: str, collection_name: str,
) -> tuple[str, bool]:
    """Register a knowledge entry in the catalog.

    Returns ``(tumbler, created)`` — *tumbler* is the catalog
    ``Document.doc_id`` (Tumbler string) so the caller can pass it to
    ``T3Database.put()`` as ``catalog_doc_id`` for chunk-write-time
    embedding (RDR-101 Phase 3 PR δ Stage B.4); *created* is True only
    when this call MINTED a brand-new document row (the
    ``writer.register`` path). Dedup hits (:func:`resolve_knowledge_doc_for_chash`,
    the nexus-sdp0u source_uri reconcile below, or the GH #1370
    ghost-by-title reconcile) return ``created=False`` so the nexus-b6enc
    C2 compensation never deletes a pre-existing row the put deduped
    onto. The ``writer.register`` leg itself now (nexus-vfef0) also
    returns ``created=False`` for the one race the three prechecks above
    cannot close — a genuinely CONCURRENT first-put race on a brand-new
    (collection, title), where the engine's wire response tells a race
    LOSER (this call landed on the WINNER's row, not its own) apart from
    a genuine mint; see ``HttpCatalogClient.register``'s ``with_created``
    kwarg. Returns ``("", False)`` when an error occurs, or in the
    SQLite opt-out mode
    when no local catalog is initialised (service mode always has a
    catalog — the Java service owns it; nexus-f1itv) — the schema
    funnel drops empty ``doc_id`` at the boundary.

    ``doc_id`` here is the T3 chunk natural-id (RDR-108 D1 / nexus-kmb6;
    the FULL ``sha256(content)`` hex per RDR-180). It is consulted for legacy
    ``meta.doc_id`` dedup via :func:`resolve_knowledge_doc_for_chash`
    (nexus-5axey; ``docs_for_chashes``-backed, since ``by_doc_id`` is a
    TUMBLER-only lookup and cannot answer a chash-keyed question): catalog
    entries written before Phase 4 stored the legacy 16-char sha256-of-
    collection-and-title under ``meta.doc_id``, so this lookup misses
    on those legacy entries and the hook re-registers. When that
    happens, a second lookup keyed on the synthesized ``source_uri``
    identity (nexus-sdp0u; non-empty *title* only, see below) reconciles
    a RE-PUT of the same (collection, title) onto its existing row
    regardless of chunk_count. When that also misses, a third,
    title-scoped lookup (:func:`_find_ghost_by_title`) reuses a
    pre-existing GHOST entry's tumbler instead of minting a duplicate
    (GH #1370 Defect 4a; legacy rows registered before this fix carry
    ``source_uri=""`` and are unreachable by the second lookup, so this
    third one stays the reconciliation path for them). Only when all
    three lookups miss does the hook register a brand-new document —
    with the synthesized ``source_uri`` attached, so a LATER re-put finds
    it via the second lookup instead of falling through to this one.

    nexus-sdp0u: pre-fix, this function always passed ``source_uri=""``
    to ``writer.register`` — the engine's upsert-on-``(tenant,
    source_uri)`` identity (``CatalogRepository.registerDocument``'s
    leg-1 SELECT) therefore never matched, and re-putting the same
    title minted an unbounded run of documents with contradictory
    content (production: 1.1.1/1.1.2/1.1.3 from three puts of one
    title). When *title* is non-empty this now synthesizes a stable
    ``source_uri`` via :func:`nexus.aspect_readers.uri_for` — the SAME
    convention the aspect-extraction reader already uses to resolve
    knowledge-collection identity (chunk metadata carries no
    ``source_path`` for these single-chunk callers since RDR-102 D2,
    nexus-bm8dd, so the reader's knowledge-collection identity field is
    *title* — this reuses that exact convention rather than forking a
    second one). Empty *title* synthesizes nothing (``source_uri=None``):
    a title-less ``chroma://<collection>/`` URI would collapse every
    untitled document under one identity, which is worse than today's
    unlimited-duplicate behavior for that one case — so it is left
    exactly as before (nexus-39upx: legacy-duplicate collapse is a
    separate, out-of-scope backfill).
    """
    # RDR-146 P1.2: this hook fires on every store_put / memory promote,
    # including the long-lived MCP server process. It MUST NOT open a
    # direct .catalog.db writer (the two-writer hazard RDR-146 closes).
    # Reads go through the read-only reader; writes route through the
    # write-only daemon proxy (the single writer). Handles closed in
    # finally so the hot path does not leak.
    reader = None
    writer = None
    try:
        from nexus.catalog.factory import make_catalog_reader, make_catalog_writer  # noqa: PLC0415 - deferred to avoid circular import at module load

        # nexus-f1itv: presence semantics belong to the factory. In service
        # mode the Java service owns the catalog and no local state exists —
        # the old local ``Catalog.is_initialized(catalog_path())`` pre-check
        # silently skipped registration on every fresh box (migrated boxes
        # passed it only via the frozen migration-source ``.catalog.db``).
        # ``make_catalog_reader()`` returns ``None`` only in the SQLite
        # opt-out mode with an uninitialised local catalog.
        reader = make_catalog_reader()
        if reader is None:
            return "", False

        # Dedup by chash stored in meta.doc_id. nexus-5axey: by_doc_id is a
        # TUMBLER-only lookup on the engine and always mismatched this
        # chash-shaped doc_id; resolve_knowledge_doc_for_chash uses
        # docs_for_chashes, the chash-appropriate reverse lookup.
        existing = resolve_knowledge_doc_for_chash(
            reader, doc_id, log_event="catalog_store_hook_dedup"
        )
        if existing is not None:
            return str(existing.tumbler), False

        # nexus-sdp0u: stable, collection-scoped identity for this document.
        # Reuses aspect_readers.uri_for's exact chroma:// convention (the
        # knowledge-collection identity field the reader already resolves
        # by is *title*, since these single-chunk callers carry no
        # source_path in chunk metadata post-RDR-102 D2) — one URI format,
        # never a second one. Empty title synthesizes nothing: see the
        # docstring for why a title-less URI must not be minted.
        source_uri = uri_for(collection_name, title) if title else None

        # Get or create "knowledge" curator owner, filtered on owner_type so
        # a same-named REPO owner cannot shadow the intended curator (same
        # bug shape as the doc_indexer family fix). Via the protocol method
        # (nexus-qnp5s, implemented on BOTH backends), NOT raw reader._db
        # SQL: HttpCatalogClient._db raises RuntimeError in service mode,
        # and the raw-SQL version of this lookup made the outer best-effort
        # except swallow that — turning this entire hook into a silent
        # no-op for every service-mode store_put (GH #1370 review finding).
        owner_t = reader.curator_owner_tumbler_by_name("knowledge")
        # RDR-146 P2 (nexus-5p2ci.12): store_put / memory promote are
        # user-initiated and latency-sensitive. The MCP server is non-tty, so
        # the isatty() fallback would misclassify these as batch; tag
        # interactive so they take fairness priority over a background index.
        writer = make_catalog_writer(priority="interactive")
        owner = owner_t if owner_t is not None else writer.register_owner(
            "knowledge", "curator"
        )

        # nexus-sdp0u: reconcile a RE-PUT of the same (collection, title)
        # identity onto its existing LIVE row — regardless of chunk_count,
        # unlike the ghost-by-title fallback below — instead of minting a
        # sibling. by_source_uri is an exact-identity lookup (the URI
        # already encodes the exact collection+title pair store_put /
        # memory-promote register under), so unlike _find_ghost_by_title's
        # FTS token match it needs no owner-prefix post-filter.
        #
        # This precheck (rather than letting the engine's own leg-1
        # idempotency SELECT inside registerDocument silently return the
        # existing tumbler from writer.register) is what lets this
        # function keep created=True meaning "genuinely minted": get that
        # wrong and a later t3.put failure would run
        # rollback_minted_catalog_entry against a live, already-populated
        # document instead of the row this call actually minted
        # (nexus-b6enc C2 firing on the wrong row).
        if source_uri is not None:
            existing_by_uri = reader.by_source_uri(source_uri)
            if existing_by_uri is not None:
                writer.update(
                    existing_by_uri.tumbler,
                    physical_collection=collection_name,
                    meta={"doc_id": doc_id},
                )
                _log.debug(
                    "catalog_store_hook_reput",
                    tumbler=str(existing_by_uri.tumbler), source_uri=source_uri,
                )
                return str(existing_by_uri.tumbler), False

        # GH #1370 Defect 4a: reconcile onto a pre-existing ghost with the
        # same title (under the knowledge curator owner) instead of minting
        # a near-duplicate. See _find_ghost_by_title for the ghost-only
        # restriction rationale.
        #
        # nexus-sdp0u fix-round (round-1 critique CRITICAL): this branch
        # MUST also stamp source_uri, not just physical_collection/meta.
        # A legacy ghost carries source_uri="" (pre-dates this fix); if the
        # reconcile below left it "", the row's manifest gets populated
        # right after (chunk_count becomes > 0) and it falls out of BOTH
        # identity checks on the NEXT re-put — by_source_uri misses on ""
        # and _find_ghost_by_title requires chunk_count == 0 — so that
        # very next re-put would mint a fresh duplicate, reproducing this
        # bead's own bug for the entire RDR-145 ghost population. Passing
        # source_uri here is what lets the SECOND-and-later re-put reach
        # this document via the by_source_uri lookup above instead.
        ghost = _find_ghost_by_title(reader, owner, title)
        if ghost is not None:
            writer.update(
                ghost.tumbler,
                physical_collection=collection_name,
                meta={"doc_id": doc_id},
                source_uri=source_uri or "",
            )
            _log.debug(
                "catalog_store_hook_deduped",
                deduped_by="title", tumbler=str(ghost.tumbler),
            )
            return str(ghost.tumbler), False

        # KNOWN RESIDUAL (nexus-n90xg): a legacy NON-ghost row (chunk_count
        # > 0, source_uri="" — populated before nexus-sdp0u) is unreachable
        # by all three lookups above, so its first post-fix re-put falls
        # through here and mints ONE bounded duplicate. That new document
        # carries the synthesized source_uri, so the second-and-later
        # re-puts converge onto it via by_source_uri. The legacy row itself
        # is collapsed by the nexus-n90xg one-shot backfill sweep, not here.
        #
        # nexus-vfef0: this is also the ONLY leg exposed to the genuinely
        # CONCURRENT first-put race the three prechecks above cannot close
        # (two callers both miss all three lookups and both reach here for
        # a brand-new (collection, title)). ``with_created=True`` surfaces
        # the engine's created-vs-matched wire signal so the race LOSER
        # reports ``created=False`` (its own tumbler-shaped return is
        # actually the WINNER's row) instead of the previously-hardcoded
        # ``True`` — the exact gap rollback_minted_catalog_entry's KNOWN
        # RESIDUAL documented.
        tumbler, created = writer.register(
            owner=owner, title=title, content_type="knowledge",
            physical_collection=collection_name,
            meta={"doc_id": doc_id},
            source_uri=source_uri or "",
            with_created=True,
        )
        return str(tumbler), created
    except Exception as exc:  # noqa: BLE001 - best-effort post-store catalog hook must not crash caller; logged + audited
        # nexus-ou4tb: the "" return is indistinguishable from "no tumbler
        # assigned", so at DEBUG this was a silent non-registration. WARNING +
        # audit row so nx doctor can say how many documents are affected.
        _log.warning("catalog_store_hook_failed", exc_info=True)
        from nexus.hook_registry import record_catalog_hook_failure  # noqa: PLC0415 — deferred, avoids an import cycle

        record_catalog_hook_failure(
            source_path=doc_id or title or "", collection=collection_name or "",
            hook_name="catalog_store_hook", error=str(exc),
        )
        return "", False
    finally:
        if writer is not None:
            try:
                writer.close()
            except Exception:  # noqa: BLE001 — best-effort handle cleanup in finally; a raising close AFTER a successful register would DISCARD the (tumbler, created=True) return (return-in-try + raising-finally semantics) and orphan the created-flag the nexus-b6enc C2 compensation depends on
                _log.warning("catalog_store_hook_writer_close_failed", exc_info=True)
        if reader is not None:
            try:
                reader._db.close()
            except Exception:  # noqa: BLE001 — best-effort handle cleanup in finally; close failure is non-critical and intentionally silent
                pass


def rollback_minted_catalog_entry(tumbler: str, *, original_error: str = "") -> bool:
    """Best-effort delete of a catalog row minted earlier IN THIS CALL
    (nexus-b6enc C2 ghost-register compensation).

    Both ``store_put`` paths register the catalog row BEFORE ``t3.put``;
    when the put fails the just-minted row must not survive as a ghost
    (row + zero manifest + zero chunks — unrecoverable content loss for
    agent callers that drop MCP error strings). Callers invoke this ONLY
    when :func:`catalog_store_hook_tracked` reported ``created=True`` —
    a dedup hit must never be deleted.

    Fail-loud discipline: this compensation must never MASK the original
    put error, so it never raises — its own failure is logged at WARNING
    with *original_error* attached so both failures are visible.

    FIXED (nexus-vfef0, was a KNOWN RESIDUAL from the nexus-sdp0u round-1
    code review): ``created=True`` from :func:`catalog_store_hook_tracked`
    used to be reliable only for every SEQUENTIAL case (the
    ``by_source_uri`` / ghost-by-title prechecks close those), but NOT for
    a genuinely CONCURRENT first-put race on a brand-new ``(collection,
    title)``: two callers can both precheck-miss and both call
    ``writer.register()``; the engine's own upsert-on-``source_uri``
    idempotency (or its unique-constraint-loser retry) then hands the RACE
    LOSER back the WINNER's tumbler. Pre-fix, the wire response carried no
    created-vs-matched signal to tell the two apart, so the loser's call
    still reported ``created=True`` — if that loser's subsequent
    ``t3.put()`` then failed, this function was invoked against the
    WINNER's live, possibly-already-populated tumbler and deleted it.

    The engine's ``/doc/register`` and ``/doc/register_many`` responses now
    carry a per-call/per-entry ``created`` boolean (additive wire field;
    ``CatalogRepository.RegisterOutcome`` on the engine side), threaded
    through here via ``HttpCatalogClient.register(..., with_created=True)``
    — a race LOSER now reports ``created=False`` and this function is never
    invoked against it. The residual now spans ONLY engines predating this
    field: an older engine omits ``created`` entirely, and the client
    treats an absent field as ``created=True`` (the historical assumption,
    preserved for compatibility) — so against a pre-tag engine the race
    loser can still report ``created=True`` and this function can still be
    invoked against the winner's row. This narrows to zero once the
    deployed engine floor reaches the tag that shipped this field (floor
    bump rides a later, paired release — see AGENTS.md's paired-release
    choreography — not this bead).

    Returns True when the row was deleted.
    """
    writer = None
    try:
        from nexus.catalog.factory import make_catalog_writer  # noqa: PLC0415 — deferred to avoid circular import at module load
        from nexus.catalog.tumbler import Tumbler  # noqa: PLC0415 — deferred, avoids import cycle

        writer = make_catalog_writer(priority="interactive")
        deleted = bool(writer.delete_document(Tumbler.parse(tumbler)))
        _log.warning(
            "store_put_ghost_register_compensated",
            tumbler=tumbler,
            deleted=deleted,
            original_error=original_error[:300],
        )
        return deleted
    except Exception:  # noqa: BLE001 — compensation must not mask the original t3.put error; both are logged
        _log.warning(
            "store_put_ghost_register_compensation_failed",
            tumbler=tumbler,
            original_error=original_error[:300],
            exc_info=True,
        )
        return False
    finally:
        if writer is not None:
            try:
                writer.close()
            except Exception:  # noqa: BLE001 — best-effort handle cleanup in finally
                pass


def store_put_manifest_direct(catalog_doc_id: str, metadatas: list[dict]) -> None:
    """Direct, fail-loud manifest write for the store_put path
    (nexus-b6enc C3 / F2).

    The generic ``fire_batch`` chain swallows every hook exception by
    contract (best-effort, correct for indexer batches). For store_put
    the manifest leg is load-bearing — a swallowed failure leaves the
    catalog row at ``chunk_count=0`` with zero manifest rows while the
    tool still returns "Stored:". This helper writes the manifest
    DIRECTLY via the whitelisted write ops (``atomic_manifest_replace``
    + ``resync_chunk_count_cache`` — both implemented on the local
    Catalog and the service ``HttpCatalogClient``) and then VERIFIES the
    rows landed via a fresh reader. Any failure RAISES so the caller can
    return an explicit "stored but NOT cataloged" result instead of a
    bare success.

    Does not replace the fire_batch manifest hook for other producers;
    the store_put re-write it implies is an idempotent replace.
    """
    if not catalog_doc_id:
        return
    chunks = [
        {
            "chash": m.get("chunk_text_hash", ""),
            "position": int(m.get("chunk_index", i)),
            "chunk_index": m.get("chunk_index"),
            "line_start": m.get("line_start") or None,
            "line_end": m.get("line_end") or None,
            "char_start": m.get("chunk_start_char") or None,
            "char_end": m.get("chunk_end_char") or None,
        }
        for i, m in enumerate(metadatas or [])
    ]
    chunks = [c for c in chunks if c["chash"]]
    if not chunks:
        raise RuntimeError(
            f"manifest write for {catalog_doc_id}: no chunk_text_hash in "
            "metadatas — nothing to catalog"
        )
    from nexus.catalog.factory import make_catalog_reader, make_catalog_writer  # noqa: PLC0415 — deferred to avoid circular import at module load

    writer = make_catalog_writer(priority="interactive")
    try:
        writer.atomic_manifest_replace(catalog_doc_id, chunks)
        writer.resync_chunk_count_cache(catalog_doc_id)
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001 — best-effort handle cleanup
            pass

    # VERIFY the rows landed (nexus-b6enc F2: never trust a silent path).
    reader = make_catalog_reader()
    if reader is None:
        raise RuntimeError(
            f"manifest write for {catalog_doc_id}: catalog reader "
            "unavailable — cannot verify the manifest landed"
        )
    try:
        landed = {row.chash for row in reader.get_manifest(catalog_doc_id)}
    finally:
        try:
            reader._db.close()
        except Exception:  # noqa: BLE001 — service-mode reader has no SQLite handle (property raises)
            pass
    expected = {c["chash"] for c in chunks}
    missing = expected - landed
    if missing:
        raise RuntimeError(
            f"manifest write for {catalog_doc_id} did not land: "
            f"{len(missing)} of {len(expected)} chunk hashes missing "
            f"after write (e.g. {sorted(missing)[0][:16]}…)"
        )


def store_delete_catalog_cleanup(
    chash_doc_id: str, *, expected_collection: str | None,
) -> tuple[str, str]:
    """Delete-asymmetry compensation for ``store_delete`` (nexus-b6enc C4).

    MCP ``store_delete`` historically removed only the T3 chunk; the
    catalog row + manifest survived with a stale ``chunk_count`` — a
    permanent ghost. For store_put-origin docs (``content_type ==
    'knowledge'`` with no ``file_path``) whose ``meta.doc_id`` matches
    the deleted chunk's natural id, delete the catalog row too via
    ``delete_document``.

    *expected_collection* is REQUIRED, no default (nexus-h7nax: a keyword
    that is silently skippable by omission is exactly what produced this
    bug class twice — see :func:`reap_catalog_manifest_for_chashes`'s own
    docstring for the full history). Pass ``None`` explicitly at a call
    site that genuinely has no collection to scope to (e.g. a test
    exercising ambiguity-detection or the "nothing to clean" miss path,
    where no live document exists to protect either way) — that is a
    deliberate, visible choice, not an accident of a default nobody
    noticed. When given (nexus-c53hy defense-in-depth): the resolved
    entry's ``physical_collection`` must match it, or cleanup is
    skipped (returns ``("", "")``, same as "nothing to clean"). The chash
    -> document resolution below is GLOBAL (no collection scoping of its
    own — see :func:`resolve_knowledge_doc_for_chash`), so without this
    check a chash that happens to also be a live document's natural id in
    a DIFFERENT physical collection than the one the caller is deleting
    from would get tombstoned by mistake. Callers that have already
    confirmed (via a collection-scoped T3 existence check) that this chash
    exists in a specific collection should pass that collection here; this
    is the second of two layers — see ``mcp/core.py::store_delete``'s own
    existence pre-check, which is the primary defense and is what actually
    stops the reap from firing on a doc_id that is not in the target
    collection at all.

    CORRECTED (nexus-3ck2g; this docstring previously claimed
    ``delete_document`` cascades the manifest on both backends — false).
    The engine soft-tombstones: it stamps ``deleted_at`` on the catalog row
    and DELIBERATELY leaves ``document_chunks`` (the manifest) and the T3
    chunk rows untouched, so a manual restore stays possible
    (nexus-xavu7) and so ``nexus.purge_trash``'s own orphan predicate
    (``EXISTS`` manifest row AND ``NOT EXISTS`` a live parent) still has
    something to find later — cascading at tombstone time would strand
    those chunks (manifest-less) forever, since ``purge_trash`` never
    sweeps a manifest-less chunk (pinned by
    ``CatalogDocumentCascadeTest`` / ``SoftDeleteTest``). The manifest and
    T3 chunks survive until an operator runs ``nx catalog purge-trash
    --no-dry-run --confirm`` (the engine's ``nexus.purge_trash(interval)``,
    wired to a caller by nexus-3ck2g) — CAVEAT (nexus-8j1zx fix round):
    that reclaim is NOT age-gated the way "past the retention window"
    might suggest. ``purge_trash``'s chunk sweep runs on EVERY currently-
    tombstoned document on the very next non-dry-run invocation, regardless
    of how recently it was deleted; only the catalog row's physical delete
    honors ``--older-than-days``. So "manual restore stays possible" holds
    only until the NEXT ``purge-trash --no-dry-run --confirm`` run anywhere
    in the tenant, not until some age threshold for this particular
    document. Until the engine ships the RDR-156 read-side tombstone
    filter (also nexus-3ck2g), the deleted content also stays fully
    searchable in the interim — this cleanup only stops the CATALOG ROW
    from resolving.

    Returns ``(tumbler, error)`` — ``("", "")`` when no matching
    store_put-origin row exists (nothing to clean), ``(tumbler, "")`` on
    successful cleanup, ``(tumbler, error)`` when a row was found but
    cleanup failed (caller surfaces it — fail loud, never silent).
    """
    reader = None
    entry = None
    try:
        from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred to avoid circular import at module load

        reader = make_catalog_reader()
        if reader is None:
            return "", ""
        # nexus-5axey: by_doc_id is a TUMBLER-only lookup (settled wji11
        # contract) and always mismatched this chash-shaped id;
        # resolve_knowledge_doc_for_chash is the chash-appropriate lookup
        # and already applies the content_type == "knowledge" / no
        # file_path filter below.
        entry = resolve_knowledge_doc_for_chash(
            reader, chash_doc_id, log_event="store_delete_catalog_lookup"
        )
    except Exception as exc:  # noqa: BLE001 — lookup failure must not mask the successful T3 delete; surfaced to caller
        _log.warning(
            "store_delete_catalog_lookup_failed",
            doc_id=chash_doc_id, exc_info=True,
        )
        return "", f"catalog lookup failed: {exc}"
    finally:
        if reader is not None:
            try:
                reader._db.close()
            except Exception:  # noqa: BLE001 — best-effort handle cleanup in finally
                pass

    if entry is None:
        return "", ""

    if expected_collection is not None and entry.physical_collection != expected_collection:
        _log.debug(
            "store_delete_catalog_cleanup_collection_mismatch",
            doc_id=chash_doc_id, expected_collection=expected_collection,
            actual_collection=entry.physical_collection, tumbler=str(entry.tumbler),
        )
        return "", ""

    tumbler = str(entry.tumbler)
    writer = None
    try:
        from nexus.catalog.factory import make_catalog_writer  # noqa: PLC0415 — deferred to avoid circular import at module load
        from nexus.catalog.tumbler import Tumbler  # noqa: PLC0415 — deferred, avoids import cycle

        writer = make_catalog_writer(priority="interactive")
        writer.delete_document(Tumbler.parse(tumbler))
        _log.info(
            "store_delete_catalog_row_removed",
            tumbler=tumbler, doc_id=chash_doc_id,
        )
        return tumbler, ""
    except Exception as exc:  # noqa: BLE001 — cleanup failure surfaced to the caller, never silently swallowed
        _log.warning(
            "store_delete_catalog_cleanup_failed",
            tumbler=tumbler, doc_id=chash_doc_id, exc_info=True,
        )
        return tumbler, str(exc)
    finally:
        if writer is not None:
            try:
                writer.close()
            except Exception:  # noqa: BLE001 — best-effort handle cleanup in finally
                pass


def reap_catalog_manifest_for_chashes(
    chashes: list[str], *, expected_collection: str | None,
) -> None:
    """Best-effort: tombstone the catalog entry that owns each *chash*,
    BEFORE the caller deletes the corresponding T3 chunk.

    *expected_collection* is REQUIRED, no default (nexus-h7nax). This
    function shipped its collection-scoping guard at nexus-c53hy and a
    THIRD call site (``db/http_vector_client.py::expire()``) was still
    found omitting it two rounds of critique later, on a defaultable
    keyword nobody remembered to pass — the identical shape as the bug
    the guard itself exists to close, one layer up. A required keyword
    turns "forgot to scope it" into a ``TypeError`` at the call site
    instead of a silent global-chash resolution; callers with genuinely
    nothing to scope to (e.g. a test exercising the ambiguity guard or
    the plain "no match" miss path, where no live document exists to
    protect either way) pass ``expected_collection=None`` explicitly —
    a visible decision, not an accident.

    When given as a real collection name: a resolved entry only gets
    reaped if its ``physical_collection`` matches. The per-chash
    resolution below is a GLOBAL chash lookup with no collection scoping
    of its own, so without this guard a chash that is ALSO (coincidentally,
    or via duplicate content) a live document's natural id in a DIFFERENT
    physical collection than the one being acted on would get tombstoned
    by mistake. This is a second, cheap layer — the primary defense is the
    caller doing a collection-scoped T3 existence check before calling
    this at all (see ``commands/store.py::delete_cmd``'s ``--id`` branch).

    Relocated from ``commands/store.py::_reap_catalog_for_doc_ids``
    (nexus-o8dil.5 Fix 1, RDR-191 P2) so ``db/http_vector_client.py`` and
    ``commands/store.py`` can both call it without either reaching into
    the other's layer — same layering rationale as this module's own
    header docstring for :func:`single_chunk_manifest_metadata`.

    ORDERING IS LOAD-BEARING (nexus-o8dil.5 round 2 finding). Callers
    MUST invoke this BEFORE deleting the T3 chunk(s), not after.
    ``PgVectorRepository#delete``'s anti-join (RDR-191 F10c) refuses to
    delete a chash while ANY live (non-tombstoned-owner) manifest row
    still references it — including the very document whose own note is
    being deleted, since ``delete_document`` is a soft tombstone that
    deliberately leaves ``catalog_document_chunks`` in place. Reaping
    AFTER the chunk delete (the pre-nexus-o8dil.5 order in both this
    module's callers) means the manifest row is still live at delete
    time, every deletion is silently refused, and reaping afterward
    tombstones a document whose chunk never actually left T3 — exactly
    the TTL-sweep leak this fix closes. Reaping first lets the chunk
    delete succeed for the common single-owner case, while still
    correctly leaving a GENUINELY shared chash's chunk protected (this
    function no-ops when :func:`resolve_knowledge_doc_for_chash` finds
    no unambiguous store_put-origin owner — see that function's own
    ambiguity-handling docstring).

    *chashes* are T3 chunk natural ids, not tumblers (nexus-5axey:
    ``resolve_knowledge_doc_for_chash`` is the chash-appropriate lookup;
    ``by_doc_id`` is tumbler-only and always mismatches these). Skipped
    silently when the catalog is uninitialised, and per-chash lookup/
    tombstone failures are logged at DEBUG and swallowed — this is a
    best-effort cleanup, not a fail-loud boundary (contrast
    :func:`store_delete_catalog_cleanup`, MCP ``store_delete``'s
    fail-loud sibling). A failed reap for one chash simply means the
    anti-join will (correctly) refuse to delete that chash's chunk; the
    caller's own deleted-count must come from the ACTUAL server response
    (never from ``len(requested ids)``) so a reap failure here shows up
    as an honest under-count, not a silent lie.
    """
    from nexus.catalog.factory import make_catalog_reader, make_catalog_writer  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    reader = None
    writer = None
    try:
        # nexus-kmo9h: presence semantics belong to the factory (None only
        # in SQLite opt-out mode when uninitialised) — the old local
        # is_initialized gate silently skipped the post-delete catalog
        # tombstone reap on every fresh service-mode box.
        reader = make_catalog_reader()
        if reader is None:
            return
        writer = make_catalog_writer()
        for chash in chashes:
            entry = resolve_knowledge_doc_for_chash(
                reader, chash, log_event="catalog_reap"
            )
            if entry is None:
                continue
            if expected_collection is not None and entry.physical_collection != expected_collection:
                _log.debug(
                    "catalog_reap_collection_mismatch",
                    chash=chash, expected_collection=expected_collection,
                    actual_collection=entry.physical_collection, tumbler=str(entry.tumbler),
                )
                continue
            writer.delete_document(entry.tumbler)
    except Exception:  # noqa: BLE001 — best-effort catalog reap; failure logged at debug, cleanup in finally
        _log.debug("catalog_reap_failed", exc_info=True, doc_ids=chashes)
    finally:
        if writer is not None:
            writer.close()
        if reader is not None:
            reader.close()
