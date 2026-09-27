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
deletes it outright. ``import_collection`` registers (or reconciles onto
an existing) catalog document per distinct owner identity in the file and
writes its manifest explicitly, once, after every one of its chunks has
been upserted -- see that function's docstring for why a per-upsert-batch
manifest write cannot be trusted for a document spanning more than one
300-chunk batch.
"""
from __future__ import annotations

import fnmatch
import gzip
import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import msgpack
import numpy as np
import structlog

from nexus.aspect_readers import uri_for
from nexus.catalog.collection_name import CollectionName
from nexus.catalog.tumbler import Tumbler
from nexus.corpus import (
    embedding_model_for_collection_name,
    index_model_for_collection,
    is_conformant_collection_name,
)
from nexus.db.limits import QUOTAS
from nexus.db.local_ef import _MODEL_DIMS as _LOCAL_RAW_MODEL_DIMS
from nexus.db.local_ef import _MODEL_TOKENS as _LOCAL_MODEL_TOKENS
from nexus.db.t3 import _BYPASS_SCHEMA_PREFIXES  # noqa: PLC0415 — same cross-module reuse pattern as commands/catalog_cmds/doctor.py
from nexus.errors import (
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
    (nexus-wbfpw.31) -- mirrors the SAME choice the live write path
    already makes for a document going into *collection_name*, never a
    fresh convention. The split is by CONTENT_TYPE, not by name
    conformance -- a ``knowledge`` collection's four-segment name is
    routinely conformant too (``knowledge__<subject>__<model>__v<n>``),
    but its owner_id segment is an arbitrary subject slug, never a
    tumbler-derived one, because every knowledge document is owned by
    the ONE ``knowledge`` curator regardless of which subject collection
    it lives in (``catalog_store_hook_tracked``'s own owner lookup):

    * A conformant, NON-knowledge collection name (code/docs/rdr) embeds
      its owner segment in the name itself (``CollectionName.owner_id``)
      -- the identical field the indexer's own catalog hook registers
      those documents under (``owner_segment_for_tumbler``'s forward
      direction). This reverses it: hyphens back to dots reconstruct the
      owner's own tumbler prefix directly, no catalog round trip needed.
    * Every other case -- a ``knowledge`` collection (conformant or not),
      or a legacy / non-conformant name (2-segment, or simply
      unregistered, the same fallback ``export_collection`` already
      applies when no catalog row backs it) -- is owned by the
      ``knowledge`` curator, the identical owner
      ``catalog_store_hook_tracked`` registers every note under.

    Raises :class:`NexusError` naming *collection_name* when a conformant
    non-knowledge name's owner segment does not parse to a tumbler -- a
    malformed collection name is a data-correctness problem, not
    something to paper over with a guessed owner.
    """
    if is_conformant_collection_name(collection_name):
        cn = CollectionName.parse(collection_name)
        if cn.content_type != "knowledge":
            owner_str = cn.owner_id.replace("-", ".")
            try:
                return Tumbler.parse(owner_str)
            except Exception as exc:
                raise NexusError(
                    f"Import into {collection_name!r} cannot resolve an "
                    f"owner tumbler from the collection's owner segment "
                    f"{cn.owner_id!r}: {exc}"
                ) from exc
    owner_t = reader.curator_owner_tumbler_by_name("knowledge")
    if owner_t is not None:
        return owner_t
    return writer.register_owner("knowledge", "curator")


def _accumulate_owner_group(
    owner_groups: dict[str, dict],
    owner_meta: Any,
    chash: str,
    *,
    fallback_source_uri: str,
    fallback_title: str,
    fallback_content_type: str,
    target_collection: str,
) -> None:
    """Assign *chash* to the owner-manifest group it belongs to
    (nexus-wbfpw.31), accumulated across the WHOLE import file rather
    than per upsert-batch.

    A document's chunks can span several 300-record upsert batches, and
    ``manifest_write_batch_hook``'s own per-batch position enumeration
    (``int(m.get("chunk_index", i))`` where ``i`` is the LOCAL index
    within that one hook call) restarts at 0 on every batch/group -- see
    that function's docstring. Grouping every chash for one owner
    identity here, across every batch, and writing ONE
    ``write_manifest`` (a replace, not an append) after the whole file
    has streamed is the only way to get correct positions for a
    multi-batch document; see ``import_collection``'s own docstring.

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
    to this group's own running count -- stable file-order enumeration,
    exactly like :func:`_fire_store_chains_grouped_by_doc`'s legacy path.
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
        {"source_uri": source_uri, "title": title, "content_type": content_type, "rows": []},
    )
    if position is None:
        position = len(group["rows"])
    group["rows"].append((position, chash))


def _manifest_rows(rows: list[tuple[int, str]]) -> list[dict]:
    """Manifest rows for one owner group, ordered by recorded position
    (nexus-wbfpw.31). Positions are the manifest's primary key per
    document, so if a hand-made or mixed-vintage file gives two chunks
    the same position, keep the order and renumber from 0 rather than
    let ``write_manifest`` fail on the key.
    """
    ordered = sorted(enumerate(rows), key=lambda ir: (ir[1][0], ir[0]))
    positions = [pos for _, (pos, _) in ordered]
    if len(set(positions)) != len(positions):
        return [{"chash": chash, "position": i} for i, (_, (_, chash)) in enumerate(ordered)]
    return [{"chash": chash, "position": pos} for _, (pos, chash) in ordered]


def _write_owner_group(
    group: dict, collection_name: str, owner_tumbler: Tumbler, reader: Any, writer: Any,
) -> int:
    """Register (or find) the document for one owner group and write its
    manifest in *collection_name* (nexus-wbfpw.31). Returns the number of
    manifest rows written.

    ``source_uri`` is unique across the tenant, and ``write_manifest``
    replaces a document's rows in EVERY collection, so a document can own
    live chunks in only one collection. When the export's document still
    lives in another collection (``--collection`` naming a different
    target), import COPIES rather than moves (Sam, 2026-09-27): the
    existing document is left untouched, so its own collection stays
    live, and the target gets a separate document under the
    target-qualified identity ``nxexp://<target>/<original source_uri>``.
    Re-importing finds that qualified document again, so it is idempotent.
    """
    source_uri = group["source_uri"]
    existing = reader.by_source_uri(source_uri) if source_uri else None
    if existing is not None and existing.physical_collection != collection_name:
        source_uri = f"nxexp://{collection_name}/{source_uri}"
        existing = reader.by_source_uri(source_uri)
    if existing is not None:
        doc_tumbler = existing.tumbler
    else:
        doc_tumbler = writer.register(
            owner=owner_tumbler,
            title=group["title"] or group["source_uri"],
            content_type=group["content_type"] or "knowledge",
            physical_collection=collection_name,
            source_uri=source_uri,
        )
    rows = _manifest_rows(group["rows"])
    writer.write_manifest(str(doc_tumbler), rows, collection=collection_name)
    return len(rows)


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
        If True, records whose id already exists in the target collection
        are skipped rather than overwritten (GH #1370 D3). Useful for
        resuming a partially-completed import.

    Returns
    -------
    dict with keys: collection_name, imported_count, skipped_count,
    rehashed_count, owned_count, elapsed_seconds. ``owned_count``
    (nexus-wbfpw.31) is the number of chunks that got an explicit
    catalog-manifest row written by THIS call (owner-grouped or
    file-fallback records only -- a legacy record carrying ``meta.doc_id``
    is manifested by the existing per-batch hook path and is not counted
    here).

    Every chunk this function upserts that is NOT keyed by a legacy
    ``meta.doc_id`` (see :func:`_fire_store_chains_grouped_by_doc`) is
    grouped by owner identity (:func:`_accumulate_owner_group`) as it
    streams, and — once every batch has been flushed — each group's
    document is registered (or reconciled onto an existing one) and its
    manifest is written EXPLICITLY, once, with the group's full,
    correctly-ordered row list (:func:`_resolve_import_owner_tumbler`
    picks the owner). This is deliberately NOT routed through
    ``manifest_write_batch_hook`` (the per-batch hook every OTHER T3 write
    path uses): that hook's position numbering is local to one
    ``fire_store_chains`` call and restarts at 0 per batch, which is
    wrong the moment a document's chunks span more than one 300-record
    upsert batch (exactly the shape RDR-192 Step 5 needs this fix to
    close for a large import). The hook already naturally NO-OPS for
    these records regardless -- ``_fire_store_chains_grouped_by_doc``
    groups by ``meta.get("doc_id", "")``, and every non-legacy record's
    key is the empty string, which the hook's own ``if not by_doc:
    return`` guard skips -- so no hook-side change was needed to keep the
    two write paths from producing conflicting manifest rows for the same
    chunk.

    Raises
    ------
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
    enforce_dims_check: bool = (
        assume_model is not None
        or embedding_model_for_collection_name(collection_name) is not None
    )

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

    # nexus-wbfpw.31: owner-manifest grouping, accumulated across the
    # WHOLE file (see _accumulate_owner_group / this function's own
    # docstring for why a per-batch write cannot be trusted). Identity
    # for a record with no ``owner`` field and no legacy ``meta.doc_id``
    # is one document per IMPORT FILE, keyed by a source_uri derived from
    # the target collection and the input file's name -- stable across
    # repeated imports of the same file.
    owner_groups: dict[str, dict] = {}
    file_fallback_source_uri = f"nxexp://{collection_name}/{input_path.name}"
    file_fallback_title = input_path.name
    default_content_type: str = header.get("database_type") or "knowledge"

    # GH #1370 D1: legacy (pre-migration) exports carry non-conformant
    # chunk ids that fail the Postgres ``chash`` length constraint on
    # import. Bypass-schema collections (``taxonomy__*``) use their own
    # programmatic id scheme (not content-derived) and must NOT be
    # rehashed -- that would break their intentional stable identifiers.
    rehash_ids = not collection_name.startswith(_BYPASS_SCHEMA_PREFIXES)

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

                ids.append(rec_id)
                documents.append(doc)
                embeddings.append(emb)
                metadatas.append(meta)

                # nexus-wbfpw.31: group every non-legacy record for the
                # explicit end-of-import manifest write. Uses the FINAL
                # (possibly rehashed) rec_id -- the id that will actually
                # be written to T3. Unconditional (before --skip-existing
                # filtering below): a record dropped as a duplicate at
                # flush time was already written by a prior run and must
                # still end up owned by this one.
                if not meta.get("doc_id"):
                    _accumulate_owner_group(
                        owner_groups, record.get("owner"), rec_id,
                        fallback_source_uri=file_fallback_source_uri,
                        fallback_title=file_fallback_title,
                        fallback_content_type=default_content_type,
                        target_collection=collection_name,
                    )

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
    if ids:
        f_ids, f_docs, f_embs, f_metas, skipped = _filter_existing(
            ids, documents, embeddings, metadatas,
        )
        skipped_count += skipped
        if f_ids:
            _upsert_with_hint(db, collection_name, f_ids, f_docs, f_embs, f_metas, hooks)
        imported_count += len(f_ids)

    # nexus-wbfpw.31: register (or reconcile onto) one document per owner
    # group and write its manifest EXPLICITLY, once, now that every batch
    # has been upserted -- see this function's docstring for why this
    # cannot be the per-batch manifest_write_batch_hook.
    owned_count = 0
    if owner_groups and not _owners_apply(db):
        # A non-service handle (the InMemoryVectorClient unit-test
        # substrate) holds its chunks outside the engine, so the catalog
        # manifest cannot reference them (the manifest's chunk FK refuses
        # it). Capability, not configuration: production never takes this.
        _log.info(
            "import_owners_skipped_non_service_handle",
            collection=collection_name, document_groups=len(owner_groups),
        )
        owner_groups = {}
    if owner_groups:
        from nexus.catalog.factory import make_catalog_reader, make_catalog_writer  # noqa: PLC0415 — deferred to avoid import cycle
        reader = make_catalog_reader()
        writer = make_catalog_writer(priority="interactive")
        failures: list[tuple[str, str]] = []
        try:
            owner_tumbler = _resolve_import_owner_tumbler(collection_name, reader, writer)
            for group in owner_groups.values():
                # One group's failure must not strand every later group
                # manifest-less: record it, carry on, report all at the end.
                try:
                    owned_count += _write_owner_group(
                        group, collection_name, owner_tumbler, reader, writer,
                    )
                except Exception as exc:  # noqa: BLE001 — collected and re-raised below as one NexusError
                    _log.warning(
                        "import_owner_group_failed",
                        collection=collection_name,
                        source_uri=group["source_uri"],
                        error=str(exc),
                    )
                    failures.append((group["source_uri"], str(exc)))
        finally:
            _close = getattr(writer, "close", None)
            if callable(_close):
                _close()
        _log.info(
            "import_owners_reconciled",
            collection=collection_name,
            document_groups=len(owner_groups),
            owned_count=owned_count,
            failed_groups=len(failures),
        )
        if failures:
            shown = "; ".join(f"{uri}: {err}" for uri, err in failures[:5])
            more = f" (and {len(failures) - 5} more)" if len(failures) > 5 else ""
            raise NexusError(
                f"Import stored every chunk in {collection_name!r}, but "
                f"{len(failures)} of {len(owner_groups)} owner documents could "
                f"not be registered, so their chunks have no catalog owner and "
                f"are not searchable: {shown}{more}. Re-running the same import "
                f"is safe (document lookup and manifest writes are idempotent)."
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
        "elapsed_seconds": round(elapsed, 2),
    }
