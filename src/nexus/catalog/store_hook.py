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

import structlog

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


def _find_ghost_by_title(reader, owner, title: str):
    """Return an existing GHOST catalog entry under *owner* whose title
    exactly matches *title*, or ``None``.

    GH #1370 Defect 4a: a pre-existing catalog entry with the same
    title (e.g. a ghost with ``chunk_count=0`` and empty ``head_hash``,
    left behind by a pre-migration catalog or an earlier failed index)
    is invisible to ``by_doc_id`` — that entry's ``meta.doc_id`` predates
    this content's hash. Without this lookup, ``catalog_store_hook``
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
    ``writer.register`` path). Dedup hits (``by_doc_id`` or the
    GH #1370 ghost-by-title reconcile) return ``created=False`` so the
    nexus-b6enc C2 compensation never deletes a pre-existing row the
    put deduped onto. Returns ``("", False)`` when an error occurs, or
    in the SQLite opt-out mode
    when no local catalog is initialised (service mode always has a
    catalog — the Java service owns it; nexus-f1itv) — the schema
    funnel drops empty ``doc_id`` at the boundary.

    ``doc_id`` here is the T3 chunk natural-id (RDR-108 D1 / nexus-kmb6;
    the FULL ``sha256(content)`` hex per RDR-180). It is consulted for legacy
    ``meta.doc_id`` dedup via ``cat.by_doc_id``: catalog entries
    written before Phase 4 stored the legacy 16-char sha256-of-
    collection-and-title under ``meta.doc_id``, so this lookup misses
    on those legacy entries and the hook re-registers. When that
    happens (or when this is the first-ever store for *title*), a
    second, title-scoped lookup (:func:`_find_ghost_by_title`) reuses
    a pre-existing GHOST entry's tumbler instead of minting a
    duplicate (GH #1370 Defect 4a). Only when both lookups miss does
    the hook register a brand-new document.
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

        # Dedup by chunk_chroma_id stored in legacy meta.doc_id.
        existing = reader.by_doc_id(doc_id)
        if existing is not None:
            return str(existing.tumbler), False

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

        # GH #1370 Defect 4a: reconcile onto a pre-existing ghost with the
        # same title (under the knowledge curator owner) instead of minting
        # a near-duplicate. See _find_ghost_by_title for the ghost-only
        # restriction rationale.
        ghost = _find_ghost_by_title(reader, owner, title)
        if ghost is not None:
            writer.update(
                ghost.tumbler,
                physical_collection=collection_name,
                meta={"doc_id": doc_id},
            )
            _log.debug(
                "catalog_store_hook_deduped",
                deduped_by="title", tumbler=str(ghost.tumbler),
            )
            return str(ghost.tumbler), False

        tumbler = writer.register(
            owner=owner, title=title, content_type="knowledge",
            physical_collection=collection_name,
            meta={"doc_id": doc_id},
        )
        return str(tumbler), True
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


def store_delete_catalog_cleanup(chash_doc_id: str) -> tuple[str, str]:
    """Delete-asymmetry compensation for ``store_delete`` (nexus-b6enc C4).

    MCP ``store_delete`` historically removed only the T3 chunk; the
    catalog row + manifest survived with a stale ``chunk_count`` — a
    permanent ghost. For store_put-origin docs (``content_type ==
    'knowledge'`` with no ``file_path``) whose ``meta.doc_id`` matches
    the deleted chunk's natural id, delete the catalog row too
    (``delete_document`` cascades the manifest on both backends: the
    local Catalog deletes ``document_chunks`` explicitly, the engine via
    the fk-001 CASCADE).

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
        entry = reader.by_doc_id(chash_doc_id)
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

    if entry is None or entry.content_type != "knowledge" or entry.file_path:
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
