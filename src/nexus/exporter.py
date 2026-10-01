# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Collection export/import for T3 ChromaDB backup and migration.

Format: ``.nxexp`` (Nexus Export)
- Line 1: JSON header (newline-terminated) containing format metadata
- Remainder: gzip-compressed msgpack stream of records

Each record is a dict:
    {"id": str, "document": str, "metadata": dict, "embedding": bytes}

Embeddings are stored as little-endian float32 bytes (numpy tobytes).

nexus-wbfpw.31 (RDR-192 Step 5, client): each record MAY also carry an
``owner`` key -- ``{"source_uri": str, "title": str, "content_type": str,
"position": int}`` -- naming the chunk's LIVE, own-collection catalog
document at export time and its manifest position within it. This is an
UNKNOWN key to every importer predating this change (older importers
ignore unrecognized record fields), so ``FORMAT_VERSION`` is not bumped.
Without it, an imported chunk has no catalog document or manifest row at
all: since RDR-192 Step 5 (nexus-wbfpw.10) a content read returns only
chunks with a live owner, so a manifest-less imported chunk is invisible
to search once its liveness grace window lapses, and the RDR-192 reaper
deletes it outright.

RDR-223 (nexus-z0o2p.19): ``import_collection`` registers (or finds) the
catalog document per distinct owner identity in the file as each page of
records arrives, and writes every chunk together with its owner row through
the catalog manifest routes (see :class:`_OwnerImport` and
``nexus.catalog.multi_document_write``), carrying the exported vector, so a
chunk is never stored ownerless and the engine embeds nothing. A document
that already owns chunks keeps its manifest (nexus-wbfpw.40): the file's
chunks for it are left out of the import and reported.
"""
from __future__ import annotations

import fnmatch
import gzip
import hashlib
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import msgpack
import numpy as np
import structlog

from nexus.aspect_readers import uri_for
from nexus.catalog.tumbler import Tumbler
from nexus.corpus import (
    embedding_model_for_collection_name,
    index_model_for_collection,
)
from nexus.db.limits import QUOTAS
from nexus.db.local_ef import _MODEL_DIMS as _LOCAL_RAW_MODEL_DIMS
from nexus.db.local_ef import _MODEL_TOKENS as _LOCAL_MODEL_TOKENS
from nexus.db.t3 import _BYPASS_SCHEMA_PREFIXES  # noqa: PLC0415 — same cross-module reuse pattern as commands/catalog_cmds/doctor.py
from nexus.errors import (
    BatchWriteFailedError,
    EmbeddingDimensionMismatch,
    EmbeddingModelMismatch,
    FormatVersionError,
    NexusError,
)
from nexus.retry import _vector_with_retry

if TYPE_CHECKING:
    from nexus.db.http_vector_client import HttpVectorClient
    from nexus.db.t3 import T3Database
    from nexus.hook_registry import HookRegistry

_log = structlog.get_logger(__name__)

#: The format version written by this implementation.
FORMAT_VERSION: int = 1

#: The maximum format version this importer can read.
#: If an export file's format_version > this, import MUST abort.
MAX_SUPPORTED_FORMAT_VERSION: int = 1

#: Pipeline version tag embedded in every export header.
_PIPELINE_VERSION: str = "nexus-1"

#: Content-addressed chunk id length in hex interchange form (the FULL
#: sha256, RDR-180). The Postgres ``chunks_<dim>`` tables enforce
#: ``CHECK (octet_length(chash) = 32)`` (32 bytes = 64 hex) -- any export
#: record whose id doesn't satisfy this on import must be re-derived
#: (GH #1370 D1 lineage).
_CHASH_LEN: int = 64

#: Known embedding-model -> vector-dimension table (GH #1370 D2). Reused
#: from the local-mode table (``nexus.db.local_ef``) rather than
#: duplicated, keyed by the RDR-109 collection-name token; Voyage models
#: aren't in that table (cloud-only) so they're listed explicitly here.
_MODEL_DIMENSIONS: dict[str, int] = {
    "voyage-3": 1024,
    "voyage-code-3": 1024,
    "voyage-context-3": 1024,
    **{
        token: _LOCAL_RAW_MODEL_DIMS[raw]
        for raw, token in _LOCAL_MODEL_TOKENS.items()
    },
}


def _rehash_nonconformant_id(rec_id: str, doc: str) -> tuple[str, str]:
    """Return ``(new_id, full_hash)`` for an export record id that isn't
    the conformant ``_CHASH_LEN``-char content hash (GH #1370 D1).

    Content-addressed derivation matches production indexing
    (``chunk_text_hash[:32]``): hash the document text when present.
    Vector-only entries (``document == ""``) have no meaningful content
    to hash, so the *old* id is hashed instead -- deterministic and
    stable across repeated re-imports of the same legacy file.
    """
    basis = doc if doc else rec_id
    full_hash = hashlib.sha256(basis.encode()).hexdigest()
    return full_hash, full_hash  # RDR-180: the full digest IS the id


#: Substrings that indicate an upsert failure is a chash/constraint
#: conflict rather than an unrelated error (GH #1370 D3 cheap-win UX).
_CONSTRAINT_HINT_KEYWORDS: tuple[str, ...] = (
    "constraint", "integrity", "duplicate", "chash", "length(",
)


def _upsert_with_hint(
    db: "T3Database | HttpVectorClient",
    collection_name: str,
    ids: list[str],
    documents: list[str],
    embeddings: list[list[float]],
    metadatas: list[dict],
    hooks: "HookRegistry",
) -> None:
    """Upsert a batch and fire post-store hook chains, wrapping
    constraint-violation errors with an actionable hint (chash length /
    --skip-existing) instead of the raw opaque backend error (GH #1370 D3).

    Hook-firing lives in this function (rather than the caller) so the
    nexus-9099 drift guard (``test_every_cli_t3_write_function_fires_
    store_chains``) sees both the T3 write and the hook-chain fire in
    the same function body.
    """
    try:
        db.upsert_chunks_with_embeddings(
            collection_name=collection_name,
            ids=ids,
            documents=documents,
            embeddings=embeddings,
            metadatas=metadatas,
        )
    except Exception as exc:
        msg = str(exc).lower()
        if any(keyword in msg for keyword in _CONSTRAINT_HINT_KEYWORDS):
            raise NexusError(
                f"{exc}\nHint: this looks like a chunk-id constraint "
                f"conflict in collection {collection_name!r} -- a "
                "non-conformant legacy chunk id or a duplicate key. If "
                "you're re-running a partial import, retry with "
                "--skip-existing."
            ) from exc
        raise
    _fire_store_chains_grouped_by_doc(
        ids, collection_name, documents, embeddings, metadatas, hooks,
    )


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _apply_filter(
    source_path: str | None,
    includes: tuple[str, ...],
    excludes: tuple[str, ...],
) -> bool:
    """Return True if this record should be included in the export.

    Entries without a source_path (e.g. nx store put entries) pass
    unconditionally regardless of include/exclude patterns.

    Include logic (OR): if any pattern matches, the entry is included.
    Exclude logic (AND): if any pattern matches, the entry is excluded.
    Excludes are evaluated after includes.
    """
    if source_path is None:
        # No source_path — pass through unconditionally.
        return True

    if includes:
        if not any(fnmatch.fnmatch(source_path, p) for p in includes):
            return False

    if excludes:
        if any(fnmatch.fnmatch(source_path, p) for p in excludes):
            return False

    return True


def _apply_remap(source_path: str, remaps: list[tuple[str, str]]) -> str:
    """Apply the first matching prefix remap to *source_path*.

    Each element of *remaps* is a ``(old_prefix, new_prefix)`` pair.
    The first matching pair wins; subsequent pairs are not evaluated.
    """
    for old, new in remaps:
        if source_path.startswith(old):
            return new + source_path[len(old):]
    return source_path


def _fire_store_chains_grouped_by_doc(
    ids: list[str],
    collection_name: str,
    documents: list[str],
    embeddings: list[list[float]],
    metadatas: list[dict],
    hooks: "HookRegistry",
) -> None:
    """nexus-8g79.1: fire post-store chains per-doc so the manifest
    hook can attribute chunks to the right catalog tumbler.

    Pre-RDR-108-Phase-3 exports carry ``meta["doc_id"]`` per chunk
    (the tumbler string was stored in chunk metadata at write-time).
    Post-Phase-3 exports do NOT carry it; for those chunks the group
    key is the empty string and the manifest hook short-circuits —
    accepted limitation until export-format extension carries the
    catalog manifest sidecar. The grouping handles the legacy path
    correctly and degrades to the existing no-doc_id behaviour for
    Phase-3 exports.

    Records are partitioned by ``meta.get("doc_id", "")``; each group
    fires its own ``HookRegistry.fire_store_chains`` call with that
    group key as ``catalog_doc_id`` so the manifest hook attributes
    the chunks correctly. Insertion order within each group is
    preserved so ``chunk_index`` re-injection sees a stable position.
    """
    from collections import defaultdict  # noqa: PLC0415 — stdlib import kept branch-local

    groups: dict[str, list[int]] = defaultdict(list)
    for i, m in enumerate(metadatas):
        groups[(m or {}).get("doc_id", "")].append(i)

    for doc_id_key, indices in groups.items():
        sub_ids = [ids[i] for i in indices]
        sub_docs = [documents[i] for i in indices]
        sub_embs = [embeddings[i] for i in indices] if embeddings else None
        sub_metas = [metadatas[i] for i in indices]
        sub_paths = [(metadatas[i] or {}).get("source_path", "") for i in indices]
        hooks.fire_store_chains(
            sub_ids, collection_name, sub_docs,
            source_paths=sub_paths,
            embeddings=sub_embs,
            metadatas=sub_metas,
            catalog_doc_id=doc_id_key,
        )


def _owners_apply(db: object) -> bool:
    """True when *db* stores chunks in the same engine as the catalog
    (nexus-wbfpw.31), so a catalog manifest can reference them. The same
    instance-based capability guard ``mcp/core.py`` uses for its
    catalog-routed query path.
    """
    from nexus.db.http_vector_client import is_service_backed  # noqa: PLC0415 — deferred to avoid import cycle

    return is_service_backed(db)


def _resolve_export_owners(
    reader: Any, chashes: list[str], collection_name: str,
) -> dict[str, dict]:
    """Best-effort per-chash owner resolution for one export page
    (nexus-wbfpw.31): ``{chash: {"source_uri", "title", "content_type",
    "position"}}`` for every *chashes* entry that has a LIVE catalog
    document in *collection_name* whose manifest names it.

    Three batched catalog round trips total, regardless of page size:
    ``docs_for_chashes`` (reverse lookup), ``get_manifests`` (positions),
    ``resolve_many`` (document attributes) — never one call per chash.

    A chash with NO manifested document is not a failure: export already
    reads through the live-filtered content read (RDR-192 Step 5), so
    every chash here is LIVE, but liveness during the grace window does
    not require a manifest row yet (a raw T3 write that bypasses the
    catalog is live and unowned for exactly that window). Such a chash
    is simply absent from the returned dict; only a genuinely unreachable
    catalog propagates, and the caller converts that into a loud export
    failure rather than silently emitting owner-less records.

    Identical chunk text manifested by more than one live document in
    THIS collection (RDR-108's collapsing-by-design) picks the lowest
    tumbler deterministically and logs the rest at DEBUG.
    """
    if not chashes:
        return {}
    by_chash = reader.docs_for_chashes(chashes)
    all_tumblers = sorted({t for ts in by_chash.values() for t in ts})
    if not all_tumblers:
        return {}
    manifests = reader.get_manifests(all_tumblers)
    entries = reader.resolve_many(all_tumblers)
    owners: dict[str, dict] = {}
    for chash in chashes:
        candidates = [
            t for t in by_chash.get(chash, [])
            if t in entries
            and entries[t].physical_collection == collection_name
            and any(row.chash == chash for row in manifests.get(t, []))
        ]
        if not candidates:
            continue
        if len(candidates) > 1:
            _log.debug(
                "export_owner_ambiguous",
                chash=chash, collection=collection_name, candidates=len(candidates),
            )
        chosen = min(candidates, key=lambda t: Tumbler.parse(t).segments)
        entry = entries[chosen]
        position = next(
            (row.position for row in manifests.get(chosen, []) if row.chash == chash),
            0,
        )
        owners[chash] = {
            "source_uri": entry.source_uri,
            "title": entry.title,
            "content_type": entry.content_type,
            "position": position,
        }
    return owners


def export_collection(
    db: "T3Database | HttpVectorClient",
    collection_name: str,
    output_path: Path,
    includes: tuple[str, ...] = (),
    excludes: tuple[str, ...] = (),
) -> dict:
    """Export *collection_name* to *output_path* in ``.nxexp`` format.

    Parameters
    ----------
    db:
        A connected T3Database or HttpVectorClient instance (GH #1373:
        production's ``make_t3()`` returns ``HttpVectorClient`` in both
        local and cloud mode -- this must work against either backend's
        ``get_collection`` / ``get_embeddings`` surface, never a
        backend-private attribute).
    collection_name:
        Fully-qualified collection name (e.g. ``code__myrepo``).
    output_path:
        Destination file path.  Parent directories must exist.
    includes:
        Glob patterns matched against ``source_path`` metadata.
        If non-empty, only entries whose source_path matches at least one
        pattern are exported.  Entries without source_path pass through.
    excludes:
        Glob patterns matched against ``source_path`` metadata.
        Entries whose source_path matches any pattern are excluded.
        Entries without source_path are never excluded.

    Returns
    -------
    dict with keys: collection_name, record_count, exported_count,
    file_bytes, elapsed_seconds, output_path.
    """
    t0 = time.monotonic()

    # Backend-neutral collection handle (GH #1373): db.get_collection()
    # resolves to a real chromadb.Collection on T3Database or a
    # _ServiceCollectionStub on HttpVectorClient -- never reach for the
    # Chroma-only db._client_for() private method here.
    col = db.get_collection(collection_name)
    total_count = _vector_with_retry(col.count)

    embedding_model = index_model_for_collection(collection_name)

    _log.info(
        "export_start",
        collection=collection_name,
        total_count=total_count,
        embedding_model=embedding_model,
        output=str(output_path),
    )

    # Determine database_type from the collection's catalog row.
    # RDR-204 Phase 3 repoint (nexus-ft04v.26), class (c): `collection_name`
    # may be a legacy/unregistered-but-live collection the operator named
    # directly -- reads the row directly (never nexus.corpus's
    # name-parsing primitives). No row -> "knowledge", the same safe
    # default the prior no-separator branch already used.
    from nexus.mcp_infra import get_collection_row  # noqa: PLC0415 — circular-dep avoidance (nexus.mcp_infra)
    _row = get_collection_row(collection_name)
    prefix = _row["content_type"] if _row is not None else "knowledge"

    # Write header (record_count/embedding_dim filled after streaming).
    # The header is written first so the file is valid even during writing.
    # record_count and embedding_dim are informational metadata (not validated
    # on import) and are updated in a final rewrite pass after streaming.
    header: dict = {
        "format_version": FORMAT_VERSION,
        "collection_name": collection_name,
        "database_type": prefix,
        "embedding_model": embedding_model,
        "record_count": total_count,  # estimate; refined below
        "embedding_dim": 0,           # informational only; not validated on import
        "exported_at": _now_iso(),
        "pipeline_version": _PIPELINE_VERSION,  # informational; not checked on import
    }
    header_line = json.dumps(header).encode() + b"\n"

    # Stream records page-by-page: paginate ChromaDB, filter, and write each
    # page directly to the gzip stream.  This avoids accumulating all records
    # in memory (031-I1).
    page_size = QUOTAS.MAX_RECORDS_PER_WRITE
    exported_count = 0
    embedding_dim = 0

    # nexus-wbfpw.31: one shared catalog reader for the whole export — the
    # shared-slot handle's own close() is a deliberate no-op (see
    # nexus.catalog.factory._SharedServiceCatalogHandle), so there is no
    # per-call teardown to manage here. Owner resolution needs the catalog
    # and the chunks in the SAME engine, so it runs only for a
    # service-backed handle (_owners_apply); production's make_t3() always
    # returns one.
    reader = None
    if _owners_apply(db):
        from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred to avoid import cycle
        reader = make_catalog_reader()

    with open(output_path, "wb") as f:
        f.write(header_line)
        with gzip.GzipFile(fileobj=f, mode="wb") as gz:
            offset = 0
            while True:
                result = _vector_with_retry(
                    col.get,
                    include=["documents", "metadatas"],
                    limit=page_size,
                    offset=offset,
                )
                page_ids = result["ids"]
                if not page_ids:
                    break

                # Embeddings are fetched via the backend-neutral
                # get_embeddings() surface rather than
                # col.get(include=["embeddings"]): the service-mode
                # collection stub (_ServiceCollectionStub) never returns
                # embeddings through its get() envelope regardless of
                # `include` (GH #1373), so requesting them there would
                # silently export zero-length vectors on the HttpVectorClient
                # path. get_embeddings() reorders its response to match
                # request order on both backends.
                emb_array = db.get_embeddings(collection_name, page_ids)
                if emb_array.shape[0] != len(page_ids):
                    raise NexusError(
                        f"Export failed: collection {collection_name!r} page "
                        f"at offset {offset} returned {len(page_ids)} chunk "
                        f"ids but only {emb_array.shape[0]} stored "
                        "embeddings -- a chunk without a vector indicates a "
                        "data-integrity issue; re-index the collection "
                        "before exporting."
                    )

                # nexus-wbfpw.31: resolve owners for the WHOLE page's chashes
                # in one batched round trip (never per-chunk) -- see
                # _resolve_export_owners. A catalog failure here fails the
                # export loudly rather than silently writing owner-less
                # records for every remaining chunk.
                try:
                    owners_by_chash = (
                        _resolve_export_owners(reader, list(page_ids), collection_name)
                        if reader is not None else {}
                    )
                except Exception as exc:
                    raise NexusError(
                        f"Export failed: could not resolve chunk owners for "
                        f"collection {collection_name!r} at offset {offset} "
                        f"-- the catalog is unreachable: {exc}"
                    ) from exc

                for rec_id, doc, meta, emb in zip(
                    page_ids,
                    result["documents"],
                    result["metadatas"],
                    emb_array,
                ):
                    source_path = (meta or {}).get("source_path")
                    if not _apply_filter(source_path, includes, excludes):
                        continue
                    emb_bytes: bytes = np.asarray(emb, dtype=np.float32).tobytes()
                    if embedding_dim == 0 and emb_bytes:
                        embedding_dim = len(emb_bytes) // 4
                    record: dict = {
                        "id": rec_id,
                        "document": doc,
                        "metadata": meta or {},
                        "embedding": emb_bytes,
                    }
                    owner = owners_by_chash.get(rec_id)
                    if owner is not None:
                        record["owner"] = owner
                    gz.write(msgpack.packb(record, use_bin_type=True))
                    exported_count += 1

                offset += len(page_ids)
                if len(page_ids) < page_size:
                    break  # last page

    file_bytes = output_path.stat().st_size
    elapsed = time.monotonic() - t0

    _log.info(
        "export_complete",
        collection=collection_name,
        exported_count=exported_count,
        file_bytes=file_bytes,
        elapsed_seconds=round(elapsed, 2),
    )

    return {
        "collection_name": collection_name,
        "record_count": total_count,
        "exported_count": exported_count,
        "file_bytes": file_bytes,
        "elapsed_seconds": round(elapsed, 2),
        "output_path": str(output_path),
    }


def _resolve_import_owner_tumbler(collection_name: str, reader: Any, writer: Any) -> Tumbler:
    """The owner an import-minted document is registered under
    (nexus-wbfpw.31, nexus-wbfpw.33). Read from the collection's CATALOG
    ROW, never from its name (RDR-204): an owner segment is not always
    tumbler-derived (``code__arcaneum-2ad2825c__...``), and a name with a
    non-canonical model segment does not even parse.

    * A non-knowledge collection whose row's ``owner_id`` is a registered
      owner: that owner. Writers store it in the name's hyphenated form
      (``1-1``), and before nexus-7tys2 ``upsertCollection`` overwrote it
      from the name on every chunk write. The fixed engine no longer does,
      but nothing backfills a row that was already overwritten, so those
      still hold the name's segment. Both the stored value and its
      hyphens-as-dots form are tried, and either is used only when the
      catalog confirms it is a registered owner. A slug
      (``arcaneum-2ad2825c``) matches no owner and falls through.
    * Otherwise, for a non-knowledge collection, the owner of a live
      document already in it.
    * Everything else (a knowledge collection, or a collection with no
      usable row and no documents, such as gate-xr789's): the
      ``knowledge`` curator, the owner ``catalog_store_hook_tracked``
      registers every note under. Liveness needs a live owning document,
      not a particular owner.
    """
    row = reader.get_collection(collection_name) or {}
    if row.get("content_type") != "knowledge":
        owner_id = str(row.get("owner_id") or "")
        for candidate in dict.fromkeys((owner_id, owner_id.replace("-", "."))):
            if candidate and reader.get_owner_by_prefix(candidate) is not None:
                try:
                    return Tumbler.parse(candidate)
                except Exception:  # noqa: BLE001 — an unparseable owner id falls through
                    _log.warning(
                        "import_owner_row_unparseable", collection=collection_name, owner_id=candidate,
                    )
        existing = reader.list_by_collection(collection_name, limit=1)
        if existing:
            return existing[0].tumbler.owner_address()
    owner_t = reader.curator_owner_tumbler_by_name("knowledge")
    if owner_t is not None:
        return owner_t
    return writer.register_owner("knowledge", "curator")


def _locate_owner_group(
    owner_groups: dict[str, dict],
    owner_meta: Any,
    *,
    fallback_source_uri: str,
    fallback_title: str,
    fallback_content_type: str,
    target_collection: str,
) -> tuple[dict, int]:
    """Find (or open) the owner group a record belongs to and return it with the record's
    position (nexus-wbfpw.31), accumulated across the WHOLE import file rather than per
    upsert-batch.

    A document's chunks can span several 300-record pages, and
    ``manifest_write_batch_hook``'s own per-batch position enumeration
    (``int(m.get("chunk_index", i))`` where ``i`` is the LOCAL index
    within that one hook call) restarts at 0 on every batch/group. Grouping
    every record for one owner identity here, across every page, is what
    gives a multi-page document correct positions; see ``import_collection``'s
    own docstring.

    Two identity sources, in order:

    * *owner_meta* -- the export-time ``owner`` record field
      (``{"source_uri", "title", "content_type", "position"}``,
      nexus-wbfpw.31). ``source_uri`` is the group key; an owner with no
      ``source_uri`` (title-only identity, RDR-192 design) synthesizes
      one via the SAME ``chroma://<collection>/<title>`` convention
      ``catalog_store_hook_tracked`` already uses for a title-only
      knowledge note, so a later re-import (or re-put through the
      ordinary knowledge write path) converges onto the same document
      rather than minting a sibling.
    * *fallback_source_uri* / *fallback_title* -- used for a record with
      no ``owner`` field at all (a legacy post-RDR-108 export, or an
      owner-less live chunk :func:`_resolve_export_owners` could not
      resolve): every such record in ONE import file lands under one
      document, keyed by a source_uri derived from the target collection
      and the input file name, so re-importing the same file twice never
      mints a second document.

    An explicit ``position`` from *owner_meta* is honored verbatim
    (preserving the chunk's original manifest order); its absence (the
    fallback path, or a legacy owner record with no position) falls back
    to this group's own running count (``group["seen"]``) -- stable
    file-order enumeration, exactly like the legacy path.
    """
    position: int | None = None
    if isinstance(owner_meta, dict) and (owner_meta.get("source_uri") or owner_meta.get("title")):
        source_uri = owner_meta.get("source_uri") or ""
        title = owner_meta.get("title") or ""
        content_type = owner_meta.get("content_type") or fallback_content_type
        if not source_uri:
            source_uri = uri_for(target_collection, title)
        raw_position = owner_meta.get("position")
        if isinstance(raw_position, int) and not isinstance(raw_position, bool):
            position = raw_position
    else:
        source_uri = fallback_source_uri
        title = fallback_title
        content_type = fallback_content_type

    group = owner_groups.setdefault(
        source_uri,
        {"key": source_uri, "source_uri": source_uri, "title": title, "content_type": content_type, "seen": 0},
    )
    if position is None:
        position = group["seen"]
    group["seen"] += 1
    return group, position


def _locate_legacy_group(
    owner_groups: dict[str, dict],
    meta: dict,
    *,
    file_source_uri: str,
    fallback_content_type: str,
) -> tuple[dict, int]:
    """Find (or open) the owner group of a legacy record (one carrying ``meta.doc_id``,
    nexus-wbfpw.31 legacy leg) and return it with the record's position. The per-batch manifest
    hook alone cannot be trusted for these: it fires only for records actually upserted (so
    ``--skip-existing`` leaves them manifest-less), its positions restart per batch, and the named
    document is often tombstoned or absent in the target. The group records the original doc_id;
    :func:`_resolve_owner_document` keeps that document when it is live in the target collection
    and otherwise registers a new one at ``<file source_uri>#<doc_id>``, one per original
    document.
    """
    doc_id = str(meta["doc_id"])
    group = owner_groups.setdefault(
        f"legacy:{doc_id}",
        {
            "key": f"legacy:{doc_id}",
            "source_uri": f"{file_source_uri}#{doc_id}",
            "title": meta.get("title") or doc_id,
            "content_type": fallback_content_type,
            "legacy_doc_id": doc_id,
            "seen": 0,
        },
    )
    raw = meta.get("chunk_index")
    position = raw if isinstance(raw, int) and not isinstance(raw, bool) else group["seen"]
    group["seen"] += 1
    return group, position


def _resolve_owner_document(
    group: dict, collection_name: str, owner_tumbler: Tumbler, reader: Any, writer: Any,
    live_legacy: dict[str, Any] | None = None, minted_out: list[str] | None = None,
) -> str:
    """Find or register the catalog document one owner group belongs to
    in *collection_name* (nexus-wbfpw.31) and return its tumbler. Two
    groups can resolve to one document (a live document holding both
    owner-tagged and legacy doc_id chunks), so the caller keys everything
    it writes on the returned tumbler, never on the group.

    ``source_uri`` is unique across the tenant, so a document can own
    live chunks in only one collection. When the export's document still
    lives in another collection (``--collection`` naming a different
    target), import COPIES rather than moves (Sam, 2026-09-27): the
    existing document is left untouched, so its own collection stays
    live, and the target gets a separate document under the
    target-qualified identity ``nxexp://<target>/<original source_uri>``.
    Re-importing finds that qualified document again, so it is idempotent.

    A legacy group (``legacy_doc_id`` set) whose original document is live
    in *collection_name* (*live_legacy*, from one batched ``resolve_many``)
    keeps that document; otherwise it falls through to the same find-or-
    register path under its file-scoped ``#<doc_id>`` identity.

    *minted_out*, when given, receives the tumbler of a document this call REGISTERED (the engine's
    ``created`` answer), never of one it found: the importer removes those again when the run ends
    before anything was written to them (nexus-z0o2p.35, M4).
    """
    legacy = (live_legacy or {}).get(group.get("legacy_doc_id") or "")
    if legacy is not None:
        return str(legacy.tumbler)
    source_uri = group["source_uri"]
    existing = reader.by_source_uri(source_uri) if source_uri else None
    if existing is not None and existing.physical_collection != collection_name:
        source_uri = f"nxexp://{collection_name}/{source_uri}"
        existing = reader.by_source_uri(source_uri)
    if existing is not None:
        return str(existing.tumbler)
    from nexus.catalog.path_ambiguity import created_from_register_result, tumbler_from_register_result  # noqa: PLC0415 — deferred: path_ambiguity imports catalog code

    result = writer.register(
        owner=owner_tumbler,
        title=group["title"] or group["source_uri"],
        content_type=group["content_type"] or "knowledge",
        physical_collection=collection_name,
        source_uri=source_uri,
        with_created=True,
    )
    tumbler = str(tumbler_from_register_result(result))
    if minted_out is not None and created_from_register_result(result):
        minted_out.append(tumbler)
    return tumbler


@dataclass
class _PageRec:
    """One export record on its way to a combined write: the chunk and the owner group (and
    position) it belongs to."""

    rec_id: str
    doc: str
    meta: dict
    emb: list[float]
    group: dict
    position: int


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _prepass_groups(
    input_path: Path,
    *,
    fallback_source_uri: str,
    fallback_title: str,
    fallback_content_type: str,
    target_collection: str,
) -> dict[str, dict]:
    """Read the file once and return its owner groups with their record counts (``seen``) and
    highest position (``maxpos``), never the records: memory is one small dict per group. The
    import needs both before it writes anything. A document's LAST PAGE is the one that brings its
    received rows up to its count, which is when it is swept and stamped; and a row that claims a
    position already taken goes past the document's highest one, which only a whole-file scan can
    name. Grouping is the import's own (:func:`_locate_owner_group`,
    :func:`_locate_legacy_group`), so the keys and counts agree with the pass that writes."""
    groups: dict[str, dict] = {}
    with open(input_path, "rb") as f:
        f.readline()  # header
        with gzip.GzipFile(fileobj=f, mode="rb") as gz:
            unpacker = msgpack.Unpacker(gz, raw=False, max_buffer_size=10 * 1024 * 1024)
            for record in unpacker:
                meta = record["metadata"]
                if meta.get("doc_id") and not record.get("owner"):
                    group, position = _locate_legacy_group(
                        groups, meta, file_source_uri=fallback_source_uri,
                        fallback_content_type=fallback_content_type)
                else:
                    group, position = _locate_owner_group(
                        groups, record.get("owner"), fallback_source_uri=fallback_source_uri,
                        fallback_title=fallback_title, fallback_content_type=fallback_content_type,
                        target_collection=target_collection)
                group["maxpos"] = max(group.get("maxpos", -1), position)
    return groups


def _wire_embedding_model(
    db: Any, collection_name: str, effective_model: str, *, name_carries_model: bool,
) -> str:
    """The ``embedding_model`` an import request names: the model the engine will compare it with.

    The engine refuses a request whose ``embedding_model`` is not the model the collection is
    REGISTERED with. A conformant four-segment name carries its model, and the gate in
    :func:`import_collection` already proved *effective_model* equals it, so that is what is sent. A
    legacy two-segment name carries none: the export header holds the prefix-based guess (a Voyage
    model), which in a local install is not what the collection is registered with (the local
    embedder), so sending it would be refused on the first request. For such a name send the model
    the collection has, or, when it has no row yet, the one its registration will derive (the write
    path's own derivation, :func:`nexus.corpus.collection_registration_kwargs`). The engine still
    checks every vector's dimension against the collection's."""
    if name_carries_model:
        return effective_model
    resolver = getattr(db, "_resolve_collection_row", None)
    row = resolver(collection_name) if callable(resolver) else None
    model = (row or {}).get("embedding_model")
    if model:
        return str(model)
    from nexus.corpus import collection_registration_kwargs  # noqa: PLC0415 — deferred to avoid import cycle
    return collection_registration_kwargs(collection_name)["embedding_model"]


class _OwnerImport:
    """The service-backed leg of :func:`import_collection` (RDR-223, nexus-z0o2p.19): every chunk is
    written together with its owner row, through the catalog manifest routes, carrying the
    exported vector. See ``import_collection``'s docstring for the whole contract.

    A page of records arrives; each record's owner group is resolved to a catalog document (found
    or registered), then a document met for the first time is one of

    * KEPT: it already owns chunks in this collection, so the import never replaces or extends its
      manifest (nexus-wbfpw.40, Sam 2026-09-29) and the file's chunks for it are skipped and counted;
    * RESUMED: it is ``indexing`` or ``failed`` and its ``index_content_hash`` is THIS file's sha256,
      so it is the leftover of an earlier run of this same file that died (Sam 2026-09-30). It is
      finished with the APPEND form (same file, same positions: an upsert by position drops nothing,
      needs no sweep, and leaves no chunk of the dead run ownerless while the rerun catches up). A
      document in any other state, or with another hash, that owns chunks stays kept;
    * WRITTEN: it owns nothing here, so its first request is a replace and later pages append.

    Groups are resolved to documents once, in :meth:`plan`, so a document reached through several
    groups is finished once with their combined count. Decisions are made with one batched
    ``get_manifests`` per page (plus one ``resolve_many`` for the documents that own chunks). A
    kept document's chash sets are dropped as soon as all its records have been seen. The written
    documents' rows and chunks go out through
    :class:`~nexus.catalog.multi_document_write.MultiDocumentImportWriter`, which sweeps and stamps
    each document on its own last page; :meth:`plan` supplies every document's record count from a
    prepass (:func:`_prepass_groups`), which is how the last page is known. A crash therefore costs
    only the documents still open, and each of those resumes on the next run.

    Every payload chunk is written with ``force_re_embed`` so the exported vector replaces a stored
    one, including a chash another live document shares (Sam 2026-09-30): the engine counts the
    differing ones (``vector_mismatches``, reported in the import summary). A record whose chunk the
    target already holds under ``--skip-existing`` is sent without a payload, so its stored vector
    stays. ``metadata_merge`` keeps the keys another document's enrichment set on a shared chunk.
    """

    def __init__(
        self,
        *,
        db: Any,
        collection_name: str,
        hooks: "HookRegistry",
        embedding_model: str,
        file_hash: str,
        skip_existing: bool,
    ) -> None:
        self.db = db
        self.collection_name = collection_name
        self.hooks = hooks
        self.embedding_model = embedding_model
        self.file_hash = file_hash
        self.skip_existing = skip_existing
        #: owner groups met so far by the writing pass (rows are never kept: the import streams)
        self.groups: dict[str, dict] = {}
        self.imported_count = 0
        self.skipped_count = 0
        self.vector_mismatches = 0
        self.unowned_count = 0                    # kept documents' records left out of the import
        self._kept_owned = 0                      # kept documents' records the document already owns
        self._left_out: dict[str, tuple[int, str | None]] = {}   # kept tumbler -> (records left out, index_state)
        #: ``(label, reason)``: a group that failed to resolve carries its source URI, a document
        #: that failed to write or stamp carries its tumbler; the label says which.
        self.failures: list[tuple[str, str]] = []
        self._state: dict[str, str] = {}          # document tumbler -> "write" | "resume" | "kept" | "failed"
        self._kept: dict[str, dict[str, Any]] = {}        # tumbler -> chash sets, counts, index_state
        self._doc_of: dict[str, str] = {}                 # group key -> the document it resolved to
        self._doc_figures: dict[str, tuple[int, int]] = {}   # tumbler -> (records, highest position)
        self._group_failed: set[str] = set()              # group keys that did not resolve (reported)
        self._live_legacy: dict[str, Any] = {}
        self._reader: Any = None
        self._writer: Any = None
        self._import_writer: Any = None
        self._owner_tumbler: Tumbler | None = None
        #: Documents :meth:`plan` registered (found ones are never listed): phantoms if the run ends
        #: before anything lands on them (:meth:`compensate_minted`).
        self._minted: list[str] = []

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def _ensure(self) -> None:
        if self._import_writer is not None:
            return
        from nexus.catalog.factory import make_catalog_reader, make_catalog_writer  # noqa: PLC0415 — deferred to avoid import cycle
        from nexus.catalog.multi_document_write import MultiDocumentImportWriter  # noqa: PLC0415 — deferred to avoid import cycle
        self._reader = make_catalog_reader()
        self._writer = make_catalog_writer(priority="interactive")
        self._import_writer = MultiDocumentImportWriter(
            self._writer, collection=self.collection_name, content_hash=self.file_hash,
            embedding_model=self.embedding_model, force_re_embed=True, metadata_merge=True,
            # The stamp comes after the post-store chains of the page that finishes a document
            # (RDR-223 decision of 2026-09-30, nexus-z0o2p.34).
            defer_completion=True,
        )

    def close(self) -> None:
        _close = getattr(self._writer, "close", None)
        if callable(_close):
            _close()

    def abort(self, error: str) -> None:
        if self._import_writer is not None:
            self._import_writer.abort(error)

    def progress(self) -> tuple[int, int]:
        """``(documents stamped complete, documents begun)``."""
        return self._import_writer.progress() if self._import_writer is not None else (0, 0)

    def compensate_minted(self) -> int:
        """Remove the documents :meth:`plan` registered that nothing was written to, once the run
        has ended without a request in flight.

        :meth:`plan` registers every owner group's document before the first page is written. A run
        that ends before a document's first request lands (the first request refused: an engine that
        predates the writer, a 4xx, a connection never made, a client-side refusal; or a failure
        between the prepass and the first page) would leave a zero-chunk registration, the ghost that
        the note and PDF writers already remove. A document is removed only when ALL hold: this call
        registered it, no row of it landed, its manifest reads empty (another writer's version means
        the row is no longer this run's to delete), and the last failed request, if there was one, did
        not leave an outcome open (:func:`~nexus.catalog.write_outcome.may_have_written`: an
        in-flight request may have committed, and a rerun resumes it). Returns the number removed.
        Never raises: it runs inside a failure path whose own exception must propagate.
        """
        if not self._minted or self._import_writer is None:
            return 0
        if self._import_writer.request_may_have_written():
            return 0
        from nexus.catalog.store_hook import rollback_minted_catalog_entry  # noqa: PLC0415 — deferred: store_hook imports the indexers' helpers

        removed = 0
        for doc in self._minted:
            try:
                if self._import_writer.landed(doc) or self._reader.get_manifest(doc):
                    continue
                if rollback_minted_catalog_entry(doc, original_error="nxexp import ended before its first write"):
                    self._import_writer.discard(doc)
                    removed += 1
            except Exception as exc:  # noqa: BLE001 — compensation must never mask the import's own failure
                _log.warning("import_minted_compensation_failed", doc_id=doc, error=str(exc))
        if removed:
            _log.warning(
                "import_minted_documents_removed", collection=self.collection_name, removed=removed,
                minted=len(self._minted))
        return removed

    # ── prepass ───────────────────────────────────────────────────────────────

    def plan(self, pre: dict[str, dict]) -> None:
        """Resolve every owner group of the prepass to its catalog document (found or registered) and
        fix each DOCUMENT's record count and highest position. Several groups can resolve to one
        document (a live document holding both owner-tagged and legacy ``doc_id`` records; two legacy
        ids aliased to one document; a literal ``nxexp://<target>/<uri>`` identity beside the original
        that was copied to it): the document's figures are the sums, so it is finished once, when ALL
        of its records have arrived. Resolving here, before anything is written or stamped, is what
        lets the totals be complete; the writing pass reads the answers back by group key."""
        self._ensure()
        legacy_ids = [g["legacy_doc_id"] for g in pre.values() if g.get("legacy_doc_id")]
        if legacy_ids:
            self._live_legacy = {
                doc_id: entry for doc_id, entry in self._reader.resolve_many(legacy_ids).items()
                if entry.physical_collection == self.collection_name
            }
        if pre and self._owner_tumbler is None:
            self._owner_tumbler = _resolve_import_owner_tumbler(
                self.collection_name, self._reader, self._writer)
        for key, g in pre.items():
            # One group's failure must not strand every later group: record it, carry on, report all
            # at the end.
            try:
                doc = _resolve_owner_document(
                    g, self.collection_name, self._owner_tumbler, self._reader, self._writer,
                    self._live_legacy, minted_out=self._minted,
                )
            except Exception as exc:  # noqa: BLE001 — collected and re-raised at the end as one NexusError
                _log.warning(
                    "import_owner_group_failed",
                    collection=self.collection_name, source_uri=g["source_uri"], error=str(exc),
                )
                self._group_failed.add(key)
                self.failures.append((f"source {g['source_uri']}", str(exc)))
                continue
            self._doc_of[key] = doc
            total, top = self._doc_figures.get(doc, (0, -1))
            self._doc_figures[doc] = (total + g["seen"], max(top, g.get("maxpos", -1)))

    # ── one page ──────────────────────────────────────────────────────────────

    def flush(self, page: list[_PageRec]) -> None:
        """Resolve, decide and write one page of records."""
        if not page:
            return
        self._ensure()
        self._resolve_groups(page)
        first_group: dict[str, dict] = {}
        for r in page:
            doc = r.group.get("doc_id")
            if doc and doc not in self._state:
                first_group.setdefault(doc, r.group)
        self._decide(first_group)

        writer = self._import_writer
        rows_by_doc: dict[str, list[dict]] = {}
        recs_by_doc: dict[str, list[_PageRec]] = {}
        for r in page:
            doc = r.group.get("doc_id")
            if not doc:
                continue                      # its group failed to resolve; already reported
            state = self._state.get(doc)
            if state == "kept":
                k = self._kept[doc]
                k["file"].add(r.rec_id)
                k["seen"] += 1
                self.skipped_count += 1
                if k["seen"] >= k["total"]:
                    self._settle_kept(doc)
                continue
            if state not in ("write", "resume") or writer.failure(doc) is not None:
                continue
            rows_by_doc.setdefault(doc, []).append(
                {"chash": r.rec_id, "position": writer.claim_position(doc, r.position)})
            recs_by_doc.setdefault(doc, []).append(r)
        if not rows_by_doc:
            return

        already = self._existing_ids([r.rec_id for recs in recs_by_doc.values() for r in recs])
        chunks: dict[str, dict] = {}
        for recs in recs_by_doc.values():
            for r in recs:
                if r.rec_id not in already and r.rec_id not in chunks:
                    chunks[r.rec_id] = {
                        "chash": r.rec_id, "text": r.doc, "metadata": r.meta, "embedding": r.emb,
                    }
        result = writer.write_page(rows_by_doc, chunks)
        self.vector_mismatches += result.vector_mismatches
        sent_ids: list[str] = []
        sent_docs: list[str] = []
        sent_embs: list[list[float]] = []
        sent_metas: list[dict] = []
        for doc in result.written:
            for r in recs_by_doc[doc]:
                if r.rec_id in already:
                    self.skipped_count += 1
                    continue
                self.imported_count += 1
                sent_ids.append(r.rec_id)
                sent_docs.append(r.doc)
                sent_embs.append(r.emb)
                sent_metas.append(r.meta)
        if sent_ids:
            _fire_store_chains_grouped_by_doc(
                sent_ids, self.collection_name, sent_docs, sent_embs, sent_metas, self.hooks,
            )
        # The stamp, LAST (nexus-z0o2p.34): a document whose last request landed in this page is
        # stamped only after the page's chains fired, so a kill in a chain leaves it ``indexing``
        # and the next run of the same file resumes it and fires the chains again. A refusal is
        # recorded by the writer and reported in finish(); it does not stop the import.
        if result.landed:
            stamped = writer.complete_documents(result.landed)
            result.finished.extend(stamped.finished)
            result.failed.update(stamped.failed)
        _log.debug(
            "import_page_written", documents=len(result.written), finished=len(result.finished),
            failed=len(result.failed), chunks=len(sent_ids), total_so_far=self.imported_count,
        )

    def _existing_ids(self, ids: list[str]) -> set[str]:
        """Records whose chunk the target already holds (``--skip-existing``): their rows are still
        written (a stored but ownerless chunk needs its owner) but their payload is not sent."""
        if not self.skip_existing or not ids:
            return set()
        # nexus-ou4tb: existing_ids raises rather than reading as "nothing exists". Isolate to THIS
        # page: an unreadable probe means we cannot prove these ids are duplicates, so send them
        # (the write is idempotent) rather than losing the whole import's progress.
        try:
            return set(self.db.existing_ids(self.collection_name, ids))
        except Exception:  # noqa: BLE001 — per-page isolation; import continues, duplicates are idempotent
            _log.warning(
                "skip_existing_probe_failed_importing_batch",
                collection=self.collection_name, batch=len(ids), exc_info=True,
            )
            return set()

    def _resolve_groups(self, page: list[_PageRec]) -> None:
        """Attach each record's group to the document :meth:`plan` resolved it to."""
        for r in page:
            g = r.group
            if g.get("doc_id") or g.get("failed"):
                continue
            key = g["key"]
            if key in self._group_failed:
                g["failed"] = True                # reported by plan()
            elif key in self._doc_of:
                g["doc_id"] = self._doc_of[key]
            else:
                # The prepass and the writing pass read the same file with the same grouping, so this
                # is unreachable; refusing beats writing a document whose total nobody counted.
                raise NexusError(
                    f"Import of {self.collection_name!r}: owner group {g.get('source_uri')!r} appeared "
                    "in the file after the counting pass did not see it; the file changed while it "
                    "was being imported. Nothing further was written; run the import again.")

    def _decide(self, first_group: dict[str, dict]) -> None:
        """Keep, resume or write each document met for the first time (one batched manifest read)."""
        if not first_group:
            return
        new_docs = list(first_group)
        try:
            manifests = self._reader.get_manifests(new_docs)
        except Exception as exc:  # noqa: BLE001 — cannot prove the documents are empty: do not write them
            _log.warning(
                "import_owner_manifest_read_failed",
                collection=self.collection_name, docs=len(new_docs), error=str(exc),
            )
            for d in new_docs:
                self._state[d] = "failed"
                self.failures.append((f"document {d}", f"its manifest could not be read: {exc}"))
            return
        existing = {
            d: {
                # Rows stamped with another collection (None only from a pre-field engine) do not
                # make this collection's manifest non-empty.
                r.chash for r in manifests.get(d, [])
                if r.collection in (None, self.collection_name)
            }
            for d in new_docs
        }
        occupied = [d for d in new_docs if existing[d]]
        resumable: set[str] = set()
        entries: dict[str, Any] = {}
        if occupied:
            try:
                entries = self._reader.resolve_many(occupied)
            except Exception as exc:  # noqa: BLE001 — cannot tell an interrupted run of this import from a current document
                _log.warning(
                    "import_owner_state_read_failed",
                    collection=self.collection_name, docs=len(occupied), error=str(exc),
                )
                for d in occupied:
                    self._state[d] = "failed"
                    self.failures.append((f"document {d}", f"its index state could not be read: {exc}"))
            for d in occupied:
                e = entries.get(d)
                if (d not in self._state and e is not None and e.index_state in ("indexing", "failed")
                        and e.index_content_hash == self.file_hash):
                    resumable.add(d)
        for d in new_docs:
            if d in self._state:
                continue
            total, max_position = self._doc_figures[d]
            if existing[d] and d not in resumable:
                # nexus-wbfpw.40 (Sam, 2026-09-29: keep existing): a live document that already
                # owns chunks is current truth. A replace would delete every row it has (an older
                # export imported over a re-put note hid the correction), an append would
                # resurrect a superseded version. Leave its manifest alone AND leave the file's
                # chunks for it out: since RDR-223 a chunk is only ever written with an owner row.
                self._state[d] = "kept"
                entry = entries.get(d)
                self._kept[d] = {
                    "existing": existing[d], "file": set(), "seen": 0, "total": total,
                    "index_state": getattr(entry, "index_state", None),
                }
                continue
            self._import_writer.register_document(
                d, total_rows=total, max_position=max_position, resume=d in resumable)
            self._state[d] = "resume" if d in resumable else "write"

    # ── end of stream ─────────────────────────────────────────────────────────

    def _settle_kept(self, doc: str) -> None:
        """A kept document's records have all been seen: count what it already owns and what the
        file's chunks for it leave out, then drop the two chash sets (they are needed for that count
        and for nothing else)."""
        k = self._kept[doc]
        if k.get("settled"):
            return
        file_chashes = k["file"]
        kept = len(file_chashes & k["existing"])
        left_out = len(file_chashes) - kept
        self._kept_owned += kept
        self.unowned_count += left_out
        if left_out:
            self._left_out[doc] = (left_out, k["index_state"])
            _log.warning(
                "import_owner_kept_existing_manifest",
                collection=self.collection_name, doc=doc, index_state=k["index_state"],
                file_chunks=len(file_chashes), left_out=left_out,
            )
        file_chashes.clear()
        k["existing"].clear()
        k["settled"] = True

    def finish(self) -> dict[str, Any]:
        """Collect the run's verdict and summarise. Returns ``owned_count``, ``unowned_count``,
        ``unowned_documents`` (each ``{tumbler, title, index_state, left_out}``) and
        ``sweep_skipped``; failures are in :attr:`failures`. Every document was swept on its own last
        page and stamped right after that page's chains, so nothing is sent here."""
        sweep_skipped = 0
        owned_count = 0
        if self._import_writer is not None:
            done = self._import_writer.finish()
            for d, reason in done.failed.items():
                self.failures.append((f"document {d}", reason))
            owned_count += self._import_writer.rows_landed
            sweep_skipped = self._import_writer.sweep_skipped
        for doc in self._kept:
            self._settle_kept(doc)            # a document whose records did not all arrive
        owned_count += self._kept_owned
        unowned_tumblers = list(self._left_out)
        unowned_documents: list[dict[str, Any]] = []
        if unowned_tumblers:
            # The remedy nx store import prints is `nx store delete --title`, so name each
            # document by its CURRENT title. title None: the lookup failed; "": the document has none.
            try:
                found = self._reader.resolve_many(unowned_tumblers)
                titles = {t: getattr(found.get(t), "title", "") or "" for t in unowned_tumblers}
            except Exception:  # noqa: BLE001 — naming is best-effort; the counts above stand
                titles = {t: None for t in unowned_tumblers}
            unowned_documents = [
                {"tumbler": t, "title": titles[t], "index_state": self._left_out[t][1],
                 "left_out": self._left_out[t][0]}
                for t in unowned_tumblers
            ]
        return {
            "owned_count": owned_count,
            "unowned_count": self.unowned_count,
            "unowned_documents": unowned_documents,
            "sweep_skipped": sweep_skipped,
        }


def import_collection(
    db: "T3Database | HttpVectorClient",
    input_path: Path,
    target_collection: str | None = None,
    remaps: list[tuple[str, str]] | None = None,
    *,
    hooks: "HookRegistry | None" = None,
    assume_model: str | None = None,
    skip_existing: bool = False,
) -> dict:
    """Import a ``.nxexp`` file into T3.

    Parameters
    ----------
    db:
        A connected T3Database or HttpVectorClient instance.
    input_path:
        Path to the ``.nxexp`` file to import.
    target_collection:
        Override the collection name from the export header.  Useful for
        renaming on import (e.g. ``code__newname``).
    remaps:
        List of ``(old_prefix, new_prefix)`` pairs applied to the
        ``source_path`` metadata field during import.
    assume_model:
        Override the export header's declared ``embedding_model`` for both
        the model-mismatch gate and the dimension sanity check (GH #1370
        D2). Pre-migration exports can carry a wrong header label; this
        lets the caller supply the true model instead of trusting it.
    skip_existing:
        If True, no text or vector is sent for a record whose chunk the
        target collection already holds (GH #1370 D3): the stored chunk and
        vector stay, and the record still gets its owner row. Without it every
        record is written with the file's vector, which replaces a stored one.

    Returns
    -------
    dict with keys: collection_name, imported_count, skipped_count,
    rehashed_count, owned_count, unowned_count, unowned_documents,
    vector_mismatches, elapsed_seconds.
    ``imported_count`` is the number of records written with their chunk;
    ``skipped_count`` those not written: already stored (``skip_existing``:
    their owner row is still written), or belonging to a document that keeps
    its current manifest (below). ``owned_count`` (nexus-wbfpw.31) is the
    number of the file's chunks that end the import owned by their document.
    ``unowned_count`` (nexus-wbfpw.40, meaning changed by RDR-223) is the
    number of the file's chunks LEFT OUT of the import because their document
    already existed with a manifest that does not name them: an existing
    document's manifest is never replaced or extended by an import, and a
    chunk is never written without an owner row, so these are not stored.
    ``unowned_documents`` lists those documents as ``{"tumbler", "title",
    "index_state", "left_out"}`` (``index_state`` tells a document another run
    left unfinished from a finished one). ``sweep_skipped`` counts documents
    whose replaced chunks the engine's fail-open sweep did not delete.
    ``vector_mismatches`` is the number of stored vectors that differed from
    the file's and were replaced by it (0 when the target held none).

    Against the engine (RDR-223, nexus-z0o2p.19) the file is read three times:
    hashed (the fence's content hash), then a counting pass that keeps the
    records and the highest position of each owner group and nothing else
    (:func:`_prepass_groups`), then the writing pass, page by page. Every owner
    group is resolved to its document BEFORE the first page, so a document
    several groups resolve to is counted once, with the sum. Each page's records are grouped by owner identity -- a legacy
    record carrying ``meta.doc_id`` by that doc_id (:func:`_locate_legacy_group`),
    every other record by its export-time ``owner`` or the file fallback
    (:func:`_locate_owner_group`) -- and each group is resolved to a catalog
    document, found or registered (:func:`_resolve_owner_document`;
    :func:`_resolve_import_owner_tumbler` picks the owner). A document met for
    the first time is KEPT (it already owns chunks in the collection: never
    replaced or extended), RESUMED (this same file left it ``indexing`` or
    ``failed``: finished with the append form) or WRITTEN: a first page that
    replaces its manifest and later pages that append. Every request carries
    the page's chunks with their exported vectors, written with
    ``force_re_embed`` so the file's vector replaces a stored one
    (``vector_mismatches`` counts the ones that differed) and with metadata
    merged, plus the export's explicit positions, so the embedder is not called.
    A document's last page, known from the prepass, also carries its deferred
    sweep and its completion stamp, so each document reads ``complete`` as
    soon as it is whole. This is deliberately NOT routed through
    ``manifest_write_batch_hook`` (the per-batch hook every OTHER T3 write
    path uses): its position numbering is local to one ``fire_store_chains``
    call and restarts at 0 per batch. The import fires its store chains
    without that hook at all (nexus-wbfpw.40). See :class:`_OwnerImport` and
    ``nexus.catalog.multi_document_write``. A non-service handle keeps the
    plain upsert; ``taxonomy__*`` and ``quarantine-*`` targets on the service
    path are refused with :class:`NexusError` before anything is written.

    Raises
    ------
    NexusError:
        For a ``taxonomy__*`` or ``quarantine-*`` target (service path), and at the
        end if any document could not be finished (every chunk stored has its
        owner row; the failure names ``source <URI>`` for a group that could not
        be registered and ``document <tumbler>`` for one that failed writing or
        stamping).
    FormatVersionError:
        If the export file's format_version exceeds MAX_SUPPORTED_FORMAT_VERSION.
    EmbeddingModelMismatch:
        If the export's embedding_model does not match the target collection's
        expected index model.
    EmbeddingDimensionMismatch:
        If the declared model's expected dimensionality doesn't match the
        actual vectors found in the file (a mislabeled pre-migration export).
    """
    t0 = time.monotonic()
    remaps = remaps or []
    if hooks is None:
        from nexus.hook_registry import HookRegistry, install_default_hooks  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
        hooks = HookRegistry()
        install_default_hooks(hooks)
    # nexus-wbfpw.40: the explicit end-of-import write below is the only
    # manifest writer an import has. The per-batch hook replaced a live
    # document's manifest for every legacy doc_id batch before the
    # keep-existing check could run, and then looked like that document's
    # existing manifest to it.
    from nexus.mcp_infra import manifest_write_batch_hook  # noqa: PLC0415 — deferred to avoid import cycle
    hooks = hooks.without_batch(manifest_write_batch_hook)

    # Phase 1: read and validate header.
    with open(input_path, "rb") as f:
        header_line = f.readline()

    header: dict = json.loads(header_line.decode())

    file_format_version: int = header.get("format_version", 0)
    if file_format_version > MAX_SUPPORTED_FORMAT_VERSION:
        raise FormatVersionError(
            f"Export file format_version={file_format_version} exceeds "
            f"MAX_SUPPORTED_FORMAT_VERSION={MAX_SUPPORTED_FORMAT_VERSION}. "
            "Upgrade Nexus to import this file."
        )

    try:
        source_collection: str = header["collection_name"]
    except KeyError:
        raise FormatVersionError(
            f"Export file {input_path!r} is missing required header key 'collection_name'. "
            "The file may be corrupt or was produced by an incompatible version."
        )
    collection_name: str = target_collection or source_collection
    bypass_schema = collection_name.startswith(_BYPASS_SCHEMA_PREFIXES)
    if _owners_apply(db) and bypass_schema:
        # Their ids are not chunk hashes and they have no catalog documents, so no chunk of theirs
        # can be written with an owner row, and the engine refuses an ownerless write. Say so now,
        # before anything is read or written, rather than fail halfway (or embed the text and drop
        # the exported vectors, which is all the plain upsert could do).
        raise NexusError(
            f"Importing into {collection_name!r} is not supported: its ids are not chunk hashes and "
            "it has no catalog documents, and the engine writes a chunk only together with an owner "
            "row. Nothing was written."
        )
    try:
        export_model: str = header["embedding_model"]
    except KeyError:
        raise FormatVersionError(
            f"Export file {input_path!r} is missing required header key 'embedding_model'. "
            "The file may be corrupt or was produced by an incompatible version."
        )
    expected_model: str = index_model_for_collection(collection_name)

    # GH #1370 D2: --assume-model overrides the header's (possibly wrong)
    # declared model for this gate. It corrects a mislabeled export; it does
    # NOT bypass the safety check for a genuinely incompatible collection.
    effective_model: str = assume_model if assume_model is not None else export_model

    if effective_model != expected_model:
        label = "assumed model" if assume_model is not None else "export uses"
        raise EmbeddingModelMismatch(
            f"Embedding model mismatch — {label} '{effective_model}' but "
            f"target collection '{collection_name}' requires '{expected_model}'. "
            "Import aborted. Re-index from source or export to a compatible "
            "collection prefix."
        )

    _log.info(
        "import_start",
        source_collection=source_collection,
        target_collection=collection_name,
        embedding_model=export_model,
        assume_model=assume_model,
        skip_existing=skip_existing,
        input=str(input_path),
    )

    # GH #1370 D2: the dims sanity check only applies when the declared
    # model is authoritative -- either the target name is RDR-103
    # conformant (four-segment names embed the model token directly,
    # per ``embedding_model_for_collection_name``) or the caller
    # explicitly opted in via ``--assume-model`` (which must still be
    # validated, per spec, even for a legacy target). For a legacy
    # two-segment target, ``expected_model`` is only ever a prefix-based
    # *guess* (``voyage_model_for_collection``) -- local-mode installs
    # legitimately write bge/minilm vectors under 2-segment names, and
    # the guess has never been reliable there. Enforcing the dims check
    # unconditionally would block that pre-existing, unaffected
    # workflow; scope it to the cases where GH #1370 D2 actually bites
    # (migrating into a properly self-declaring conformant collection).
    name_carries_model: bool = embedding_model_for_collection_name(collection_name) is not None
    enforce_dims_check: bool = assume_model is not None or name_carries_model

    # Stream records from gzip-compressed msgpack body and upsert in batches.
    # Single file open: read header, then gzip body from the same handle
    # (eliminates the TOCTOU window of opening the file twice — 031-S8).
    # Records are upserted as each batch fills, avoiding accumulating all
    # records in memory (031-I1).
    page_size = QUOTAS.MAX_RECORDS_PER_WRITE
    imported_count = 0
    skipped_count = 0
    rehashed_count = 0
    ids: list[str] = []
    documents: list[str] = []
    embeddings: list[list[float]] = []
    metadatas: list[dict] = []

    # RDR-223 (nexus-z0o2p.19): against the engine, every chunk is written together
    # with its owner row through the catalog manifest routes, carrying the exported
    # vector, so nothing is ever stored ownerless and the embedder is not called.
    # A handle that keeps its chunks outside the engine (the InMemoryVectorClient
    # unit-test substrate: the manifest's chunk FK cannot reference them) and the
    # bypass-schema collections (``taxonomy__*``: ids that are not chashes, no
    # catalog documents) keep the plain upsert -- production never takes either.
    owner_import: _OwnerImport | None = None
    if _owners_apply(db):
        # No per-collection embed cap here: that cap (64 for a CCE collection) bounds the engine's
        # EMBEDDING latency inside one request, and an import carries every vector, so it embeds
        # nothing. The bound is the combined-write request's own (300 chunks).
        from nexus.catalog.http_catalog_client import MANIFEST_APPEND_MANY_MAX_CHUNKS  # noqa: PLC0415 — deferred to avoid import cycle
        page_size = min(page_size, MANIFEST_APPEND_MANY_MAX_CHUNKS)
        owner_import = _OwnerImport(
            db=db, collection_name=collection_name, hooks=hooks,
            embedding_model=_wire_embedding_model(
                db, collection_name, effective_model, name_carries_model=name_carries_model),
            file_hash=_file_sha256(input_path), skip_existing=skip_existing,
        )
    else:
        # A non-service handle (the InMemoryVectorClient unit-test substrate).
        _log.info("import_owners_skipped_non_service_handle", collection=collection_name)
    page: list[_PageRec] = []

    # nexus-wbfpw.31: owner identity, resolved across the WHOLE file (see
    # _locate_owner_group / this function's own docstring for why a per-batch
    # write cannot be trusted). Identity for a record with no ``owner`` field
    # and no legacy ``meta.doc_id`` is one document per IMPORT FILE, keyed by a
    # source_uri derived from the target collection and the input file's name
    # -- stable across repeated imports of the same file.
    file_fallback_source_uri = f"nxexp://{collection_name}/{input_path.name}"
    file_fallback_title = input_path.name
    default_content_type: str = header.get("database_type") or "knowledge"

    # GH #1370 D1: legacy (pre-migration) exports carry non-conformant
    # chunk ids that fail the Postgres ``chash`` length constraint on
    # import. Bypass-schema collections (``taxonomy__*``) use their own
    # programmatic id scheme (not content-derived) and must NOT be
    # rehashed -- that would break their intentional stable identifiers.
    rehash_ids = not bypass_schema

    # CLI review: infer the expected embedding byte-size from the first
    # record and reject any subsequent record whose embedding doesn't
    # match. This catches truncation/corruption mid-file without
    # hard-coding a model-specific dim (tests use 384-dim MiniLM;
    # production uses 1024-dim Voyage). Also sanity-checks that the
    # byte-size is a multiple of 4 (float32).
    expected_emb_bytes: int | None = None

    def _filter_existing(
        batch_ids: list[str],
        batch_docs: list[str],
        batch_embs: list[list[float]],
        batch_metas: list[dict],
    ) -> tuple[list[str], list[str], list[list[float]], list[dict], int]:
        """Drop records whose id already exists in the target collection
        (GH #1370 D3, ``--skip-existing``). No-op unless requested."""
        if not skip_existing:
            return batch_ids, batch_docs, batch_embs, batch_metas, 0
        # nexus-ou4tb: existing_ids raises now rather than reading as "nothing
        # exists" — which previously made --skip-existing silently skip
        # nothing. Isolate to THIS batch: an unreadable probe means we cannot
        # prove these ids are duplicates, so import them (the upsert is
        # idempotent) rather than losing the whole import's progress.
        try:
            existing = db.existing_ids(collection_name, batch_ids)
        except Exception:  # noqa: BLE001 — per-batch isolation; import continues, duplicates are idempotent
            _log.warning(
                "skip_existing_probe_failed_importing_batch",
                collection=collection_name, batch=len(batch_ids), exc_info=True,
            )
            return batch_ids, batch_docs, batch_embs, batch_metas, 0
        if not existing:
            return batch_ids, batch_docs, batch_embs, batch_metas, 0
        keep = [i for i, rid in enumerate(batch_ids) if rid not in existing]
        return (
            [batch_ids[i] for i in keep],
            [batch_docs[i] for i in keep],
            [batch_embs[i] for i in keep],
            [batch_metas[i] for i in keep],
            len(batch_ids) - len(keep),
        )

    owned_count = 0
    unowned_count = 0
    vector_mismatches = 0
    sweep_skipped = 0
    failures: list[tuple[str, str]] = []
    unowned_documents: list[dict[str, Any]] = []
    try:
        if owner_import is not None:
            # A first read of the file, for counts only: each document's record count says which
            # page is its last (when it is swept and stamped), and its highest position says where
            # a colliding row goes. See _prepass_groups.
            owner_import.plan(_prepass_groups(
                input_path, fallback_source_uri=file_fallback_source_uri,
                fallback_title=file_fallback_title, fallback_content_type=default_content_type,
                target_collection=collection_name))
        with open(input_path, "rb") as f:
            f.readline()  # skip header (already parsed above)
            with gzip.GzipFile(fileobj=f, mode="rb") as gz:
                unpacker = msgpack.Unpacker(gz, raw=False, max_buffer_size=10 * 1024 * 1024)
                for record in unpacker:
                    rec_id: str = record["id"]
                    # Vector-only entries (e.g. ``taxonomy__centroids``) round-trip
                    # ``document=None``. Coerce to empty string so the downstream
                    # write path's byte-length checks don't trip on ``None.encode()``
                    # (nexus-fxc1).
                    doc: str = record["document"] or ""
                    meta: dict = dict(record["metadata"])
                    emb_bytes: bytes = record["embedding"]

                    if expected_emb_bytes is None:
                        if len(emb_bytes) == 0 or len(emb_bytes) % 4 != 0:
                            raise FormatVersionError(
                                f"Export file {input_path!r} has a malformed "
                                f"embedding for record {rec_id!r}: "
                                f"{len(emb_bytes)} bytes is not a multiple of 4 "
                                "(float32). File may be corrupt."
                            )
                        expected_emb_bytes = len(emb_bytes)

                        # GH #1370 D2: sanity-check the declared model's dims
                        # against the actual first-record vector (scope: see
                        # ``enforce_dims_check`` above). Unknown models (not
                        # in _MODEL_DIMENSIONS) skip silently -- can't
                        # validate what we don't have a table entry for.
                        if enforce_dims_check:
                            actual_dims = expected_emb_bytes // 4
                            declared_dims = _MODEL_DIMENSIONS.get(effective_model)
                            if declared_dims is not None and declared_dims != actual_dims:
                                raise EmbeddingDimensionMismatch(
                                    declared_model=effective_model,
                                    declared_dims=declared_dims,
                                    actual_dims=actual_dims,
                                    collection=collection_name,
                                    assumed=assume_model is not None,
                                )
                    elif len(emb_bytes) != expected_emb_bytes:
                        raise FormatVersionError(
                            f"Export file {input_path!r} contains an embedding "
                            f"of {len(emb_bytes)} bytes for record {rec_id!r}, "
                            f"expected {expected_emb_bytes} bytes (same as the "
                            "first record). File may be truncated or corrupt."
                        )

                    if rehash_ids and len(rec_id) != _CHASH_LEN:
                        new_id, full_hash = _rehash_nonconformant_id(rec_id, doc)
                        if "chunk_text_hash" in meta:
                            meta["chunk_text_hash"] = full_hash
                        rec_id = new_id
                        rehashed_count += 1

                    if remaps and "source_path" in meta:
                        meta["source_path"] = _apply_remap(meta["source_path"], remaps)

                    emb: list[float] = np.frombuffer(emb_bytes, dtype=np.float32).tolist()

                    if owner_import is not None:
                        # nexus-wbfpw.31: locate the record's owner group and position now,
                        # across the WHOLE file. Uses the FINAL (possibly rehashed) rec_id --
                        # the id that will actually be written to T3. An export-time ``owner``
                        # is the chunk's current owner; a ``meta.doc_id`` beside it is stale
                        # pre-RDR-108 metadata.
                        if meta.get("doc_id") and not record.get("owner"):
                            group, position = _locate_legacy_group(
                                owner_import.groups, meta,
                                file_source_uri=file_fallback_source_uri,
                                fallback_content_type=default_content_type,
                            )
                        else:
                            group, position = _locate_owner_group(
                                owner_import.groups, record.get("owner"),
                                fallback_source_uri=file_fallback_source_uri,
                                fallback_title=file_fallback_title,
                                fallback_content_type=default_content_type,
                                target_collection=collection_name,
                            )
                        page.append(_PageRec(rec_id, doc, meta, emb, group, position))
                        if len(page) >= page_size:
                            owner_import.flush(page)
                            page = []
                        continue

                    ids.append(rec_id)
                    documents.append(doc)
                    embeddings.append(emb)
                    metadatas.append(meta)

                    # Flush batch when page_size reached.
                    if len(ids) >= page_size:
                        f_ids, f_docs, f_embs, f_metas, skipped = _filter_existing(
                            ids, documents, embeddings, metadatas,
                        )
                        skipped_count += skipped
                        if f_ids:
                            _upsert_with_hint(
                                db, collection_name, f_ids, f_docs, f_embs, f_metas, hooks,
                            )
                        imported_count += len(f_ids)
                        _log.debug("import_batch_written", count=len(f_ids), total_so_far=imported_count)
                        ids, documents, embeddings, metadatas = [], [], [], []

        # Flush remaining records.
        if owner_import is not None:
            owner_import.flush(page)
            summary = owner_import.finish()
            imported_count = owner_import.imported_count
            skipped_count = owner_import.skipped_count
            owned_count = summary["owned_count"]
            unowned_count = summary["unowned_count"]
            unowned_documents = summary["unowned_documents"]
            sweep_skipped = summary["sweep_skipped"]
            vector_mismatches = owner_import.vector_mismatches
            failures = owner_import.failures
            _log.info(
                "import_owners_reconciled",
                collection=collection_name,
                document_groups=len(owner_import.groups),
                owned_count=owned_count,
                unowned_count=unowned_count,
                vector_mismatches=vector_mismatches,
                sweep_skipped=sweep_skipped,
                failures=len(failures),
            )
        elif ids:
            f_ids, f_docs, f_embs, f_metas, skipped = _filter_existing(
                ids, documents, embeddings, metadatas,
            )
            skipped_count += skipped
            if f_ids:
                _upsert_with_hint(db, collection_name, f_ids, f_docs, f_embs, f_metas, hooks)
            imported_count += len(f_ids)
    except BaseException as exc:
        if owner_import is not None:
            stamped, begun = owner_import.progress()
            _log.warning(
                "import_aborted", collection=collection_name, documents_complete=stamped,
                documents_begun=begun, error=f"{type(exc).__name__}: {exc}")
            owner_import.compensate_minted()      # before the fence marks: a removed row has no fence left
            owner_import.abort(f"{type(exc).__name__}: {exc}")
        raise
    finally:
        if owner_import is not None:
            owner_import.close()

    if failures:
        assert owner_import is not None          # only the owner leg collects failures
        shown = "; ".join(f"{label}: {err}" for label, err in failures[:5])
        more = f" (and {len(failures) - 5} more)" if len(failures) > 5 else ""
        raise NexusError(
            f"Import of {collection_name!r} could not finish {len(failures)} of "
            f"{len(owner_import.groups)} owner documents: {shown}{more}. Every chunk that was stored "
            "has its owner row (none is stored ownerless). A document left unfinished stays "
            "indexing, and running the same import again finishes it; a document whose completion "
            "stamp was refused needs a look at the engine log."
        )

    elapsed = time.monotonic() - t0

    if rehashed_count:
        _log.info(
            "import_rehashed_nonconformant_ids",
            collection=collection_name,
            rehashed_count=rehashed_count,
        )

    _log.info(
        "import_complete",
        collection=collection_name,
        imported_count=imported_count,
        skipped_count=skipped_count,
        rehashed_count=rehashed_count,
        elapsed_seconds=round(elapsed, 2),
    )

    return {
        "collection_name": collection_name,
        "imported_count": imported_count,
        "skipped_count": skipped_count,
        "rehashed_count": rehashed_count,
        "owned_count": owned_count,
        "unowned_count": unowned_count,
        "unowned_documents": unowned_documents,
        "vector_mismatches": vector_mismatches,
        "sweep_skipped": sweep_skipped,
        "elapsed_seconds": round(elapsed, 2),
    }
