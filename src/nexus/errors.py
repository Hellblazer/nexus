# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Nexus exception hierarchy."""
from __future__ import annotations


class NexusError(Exception):
    """Base exception for all Nexus errors."""


class T3ConnectionError(NexusError):
    """Failed to connect to or use the T3 ChromaDB cloud backend."""


class IndexingError(NexusError):
    """Error during document indexing pipeline."""


class ExtractionQualityError(NexusError):
    """Post-extraction text-quality gate failed (nexus-wi1uv).

    Raised by :func:`nexus.pdf_extractor._enforce_extraction_quality` when
    extracted PDF text trips the calibrated whitespace/token-length
    signals that flag the space-stripped-garbage failure mode (MinerU or
    Docling completing but producing unsearchable output, e.g.
    "istheasetofthe"). The documented recovery is to retry with a
    different extractor, or pass ``--allow-degraded-extraction`` to accept
    the degraded output deliberately.

    A per-record-raisable member of :data:`PER_RECORD_SURVIVABLE_
    EXCEPTIONS` below (round-2 review). ``__init__`` accepts *message* as
    a plain keyword so ``tests/test_commands_dt.py``'s registry-driven
    ``_MEMBER_KWARGS``-style construction (``member_cls(**kwargs)``) works
    without special-casing — the base ``Exception.__init__`` accepts only
    positional args, which that construction pattern cannot supply.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)


class CredentialsMissingError(NexusError):
    """A required API key or credential is absent."""


class CollectionNotFoundError(NexusError):
    """The requested vector collection does not exist.

    RDR-155 P4b (P0c, nexus-g37fr plan v3): the substrate-neutral successor
    to ``chromadb.errors.NotFoundError`` as the missing-collection contract
    between :class:`~nexus.db.http_vector_client.HttpVectorClient` (raiser)
    and the indexer/purge/reidentify/backfill catchers.
    """


def collection_not_found_errors() -> tuple[type[BaseException], ...]:
    """The exception types that mean "collection does not exist".

    P3 (2026-07-25): the transition window is CLOSED. This returned
    ``(CollectionNotFoundError, chromadb.errors.NotFoundError)`` while the
    chroma-backed test substrate could still raise the latter natively; the
    chroma member dropped out when the dependency left the tree, and the
    promise held — not one catcher needed an edit at removal time.

    The function survives deliberately rather than collapsing into a bare
    ``except CollectionNotFoundError``: it is the single place a second
    missing-collection type would be added if another substrate ever raises
    its own, and ~10 call sites already spell the catch this way.
    """
    return (CollectionNotFoundError,)


def vector_argument_errors() -> tuple[type[BaseException], ...]:
    """The exception types that mean "the vector store rejected the call".

    Sibling of :func:`collection_not_found_errors`. Chroma raised
    ``InvalidArgumentError``; the substrate-neutral stores raise
    ``ValueError`` (see ``InMemoryCollection._check_dimension``). The chroma
    member dropped out with the dependency at P3.

    Deliberately broad on the nexus side: the only caller
    (``T3Database.search``) classifies on ``"dimension" in str(exc)`` and
    re-raises everything else, so a wider catch cannot swallow an unrelated
    failure -- it can only route a dimension-mismatch skip correctly.
    """
    return (ValueError,)


class EmbeddingModelMismatch(NexusError):
    """Export embedding model is incompatible with the target collection's model.

    Importing an export produced with one embedding model into a collection
    that uses a different model would silently corrupt search quality
    (cross-model cosine similarity ≈ 0.05, i.e. random noise).
    """


class FormatVersionError(NexusError):
    """Export file format version is newer than this version of Nexus supports.

    Upgrade Nexus to import this file.
    """


class EmbeddingDimensionMismatch(NexusError):
    """The export's declared embedding model doesn't match the actual
    vector dimensionality found in the file (GH #1370 D2).

    Pre-migration ``.nxexp`` exports can carry a WRONG ``embedding_model``
    header label: legacy two-segment collection names route through
    ``voyage_model_for_collection``'s prefix-based guess, which silently
    mislabels local-mode (bge/minilm) exports as Voyage models. This
    check catches the resulting dimension contradiction before it
    reaches the vector store, instead of failing with an opaque
    downstream error.

    Attributes:
        declared_model: The model name that was checked (the export
            header's value, or the ``--assume-model`` override).
        declared_dims: Expected dimensionality for ``declared_model``.
        actual_dims: Dimensionality actually found in the export's vectors.
        collection: Target collection name.
        assumed: True if ``declared_model`` came from ``--assume-model``.
    """

    def __init__(
        self,
        *,
        declared_model: str,
        declared_dims: int,
        actual_dims: int,
        collection: str,
        assumed: bool = False,
    ) -> None:
        self.declared_model = declared_model
        self.declared_dims = declared_dims
        self.actual_dims = actual_dims
        self.collection = collection
        self.assumed = assumed
        if assumed:
            label = f"assumed model {declared_model!r}"
            hint = "the --assume-model override is wrong -- pick a different model"
        else:
            label = f"header claims {declared_model!r}"
            hint = (
                "the header label is wrong (a known defect of pre-migration "
                "exports, GH #1370); re-run with --assume-model <model>"
            )
        super().__init__(
            f"{label} ({declared_dims}-dim) but vectors are {actual_dims}-dim "
            f"for collection {collection!r} -- {hint}."
        )


class SourceUriNotFoundError(NexusError):
    """``--source-uri`` was given but resolves to no LIVE catalog document.

    nexus-y8qtj: falling back to registering a brand-new Document here is
    the exact defect this type exists to prevent — a path-based re-index
    silently forking a second catalog row for a source whose real identity
    is an out-of-band URI (e.g. DEVONthink's ``x-devonthink-item://<UUID>``),
    leaving the original's chunks live, searchable, and never revisited by
    any orphan sweep (they remain part of the original document's own
    current manifest). No fallback registration; the caller must fix the
    URI or index without ``--source-uri`` for a genuinely new document.
    """


class SourceUriCollectionMismatchError(NexusError):
    """``--source-uri`` resolves to a document living in a DIFFERENT
    physical collection than the one this index run targets.

    Naming both a ``--source-uri`` and a ``--collection``/``--corpus`` that
    disagree with the document's current home is a MOVE, not a re-index;
    refusing rather than silently repointing either field keeps collection
    membership an explicit, deliberate operation (``nx catalog update`` /
    ``nx collection`` tooling), not a side effect of an index invocation.
    """


class PutOversizedError(NexusError):
    """A ``put``-path write was refused because the document exceeds the
    ChromaDB Cloud per-document byte cap.

    The indexer pipeline tolerates oversized inputs via defense-in-depth
    drop-and-warn (a chunker that produced an oversized record is the
    real bug; the pipeline keeps running). The ``put`` path has no
    chunker upstream, so dropping silently would leave the caller
    believing the write succeeded while producing a catalog ghost
    (no row in ChromaDB despite a registered ``doc_id``). See
    GitHub #244 and bead ``nexus-akof``.

    Attributes:
        doc_id: The computed doc_id that did not make it to ChromaDB.
        doc_bytes: Actual size of the serialized document in bytes.
        max_bytes: The ChromaDB Cloud cap (``QUOTAS.MAX_DOCUMENT_BYTES``).
        collection: Target collection name for clearer diagnostics.
    """

    def __init__(
        self,
        *,
        doc_id: str,
        doc_bytes: int,
        max_bytes: int,
        collection: str,
    ) -> None:
        self.doc_id = doc_id
        self.doc_bytes = doc_bytes
        self.max_bytes = max_bytes
        self.collection = collection
        super().__init__(
            f"document {doc_id!r} is {doc_bytes} bytes, exceeds "
            f"{max_bytes}-byte ChromaDB cap for collection {collection!r}. "
            f"Shrink the content or chunk it before calling put()."
        )


class ChunkLandingUnverifiedError(NexusError):
    """A metadata-only chunk update returned ``missing=None`` — the engine's
    response omitted the "missing" field, so the client cannot tell whether
    any of the updated ids were a stale-positive probe miss (nexus-tp8yk D1,
    design memo §1 P1: ``_upsert_skip_reembed`` used to treat this as "no
    reroute" and proceed silently, letting the caller's manifest hook write
    rows for chunks that were never confirmed present in T3).

    ``None`` means "cannot tell", never "zero misses" — ``REQUIRED_ENGINE_
    VERSION`` pins one engine identity per release (CLAUDE.md § Releases),
    so this should be unreachable against a correctly-deployed fleet; when
    it fires anyway (a pre-nexus-5xn3k engine, or a mixed-version fleet
    mid-rolling-deploy) the caller must refuse to proceed rather than
    silently commit manifest rows for an unconfirmed batch. The caller
    (``doc_indexer``'s fence-bracketed call sites) converts this into a
    failed index run — the fence stays ``'indexing'``, over-work on the
    next pass, never silent under-work.

    Attributes:
        collection: T3 collection the update targeted.
        count: number of ids whose landing could not be confirmed.
    """

    def __init__(self, *, collection: str, count: int) -> None:
        self.collection = collection
        self.count = count
        super().__init__(
            f"cannot confirm {count} chunk(s) landed in T3 collection "
            f"{collection!r} — the engine's update-chunks response omitted "
            f"the 'missing' field, so a stale-positive probe result cannot "
            f"be distinguished from a genuine landing. Refusing to proceed: "
            f"committing a manifest for these chunks would risk rows that "
            f"reference content never confirmed present. Re-run once the "
            f"engine fleet is on a consistent version (see REQUIRED_ENGINE_"
            f"VERSION), or check 'nx doctor' for a version mismatch."
        )


class IndexRunVerifyRefused(NexusError):
    """``HttpCatalogClient.complete_index_run`` was refused by the engine's
    fail-closed verify-then-stamp gate (RUNFENCE, nexus-5xn3k, design memo
    §3.3, mirrors ``CatalogRepository.IndexRunVerifyRefused`` on the engine
    side).

    The engine ran ``manifest_verify(doc_id)`` inside the SAME transaction
    as the completion stamp and found either ``missing > 0`` or
    ``referenced != chunk_count`` (the caller's own claimed count) — the
    document is NOT actually whole in T3, so ``index_state`` stays whatever
    it was (``'indexing'``, most commonly) rather than being stamped
    ``'complete'``. Surfacing this as a typed exception (never a silently
    absorbed 500, never a bare ``ValueError``) is the whole point of the
    fence: a run whose manifest writes ALL failed while its chunks landed
    in T3 would otherwise report success (memo §1's empty-manifest case,
    recreated one layer up).

    Attributes:
        doc_id: The document the completion call targeted.
        referenced: Chunks the manifest names.
        present: Of those, how many exist in T3.
        missing: ``referenced - present`` (the refusal trigger when > 0).
        chunk_count: The caller's own claimed chunk count (the OTHER
            refusal trigger when it disagrees with ``referenced``).

    The message ALWAYS carries the counts summary (``doc_id``/``referenced``/
    ``present``/``missing``/``chunk_count``) — a caller reading the exception
    text (a log line, a CLI error) must never lose the numbers to a
    server-supplied string. *server_detail* (the engine's own ``error``
    field, when present) is APPENDED as supplementary context, never a
    replacement for the counts summary.
    """

    def __init__(
        self,
        *,
        doc_id: str,
        referenced: int,
        present: int,
        missing: int,
        chunk_count: int,
        server_detail: str = "",
    ) -> None:
        self.doc_id = doc_id
        self.referenced = referenced
        self.present = present
        self.missing = missing
        self.chunk_count = chunk_count
        message = (
            f"completeIndexRun refused for doc_id={doc_id!r}: "
            f"referenced={referenced} present={present} missing={missing} "
            f"claimed_chunk_count={chunk_count}"
        )
        if server_detail:
            message = f"{message} (engine: {server_detail})"
        super().__init__(message)


class CombinedWriteEmbedTimeoutError(NexusError):
    """A ``httpx.ReadTimeout`` on the combined write's chunk-carrying POST
    (``HttpCatalogClient.write_manifest_many``'s ``chunks=`` branch,
    nexus-y9t08 CRITICAL fix).

    That POST makes the engine run a SYNCHRONOUS server-side embed inside
    the request; the engine has no cancellation path for it, so a retried
    timeout starts a second (third, ...) uncancelled embed on top of one
    that may still be running. The predecessor path
    (``HttpVectorClient.upsert_chunks``) deliberately excludes ALL timeout
    types from its own retry classifier for exactly this shape
    (``http_vector_client.py:779-784`` — "TimeoutError is intentionally
    NOT in this retry classifier ... it propagates straight to the
    _get/_post handler").

    ``write_manifest_many`` converts the raw ``httpx.ReadTimeout`` into
    THIS type, raised with no ``__cause__``/``__context__`` chain back to
    it (see the raise site), specifically so
    ``nexus.retry._is_connectivity_error`` — the classifier
    ``nexus.retry._manifest_write_with_retry`` uses to decide whether to
    retry the WHOLE ``write_manifest_many`` call from
    ``indexer.py``/``mcp_infra.py`` — sees a non-connectivity error and
    does not retry. A bare re-raised ``httpx.ReadTimeout`` would still be
    classified as connectivity (it IS-A ``httpx.TransportError``) and
    retried at that OUTER layer even after the inner self-heal layer
    (``RefreshableHttpStoreMixin._send``'s ``retry_read_timeout=False``
    opt-out passed for this one call) stopped retrying it — this type
    conversion is what closes that outer amplification path too, with no
    change to ``nexus.retry``'s general classifier (every other catalog
    call keeps its existing connectivity-retry behavior unchanged).

    Every OTHER exception this call can raise (``ConnectionResetError``,
    ``httpx.ConnectError``, etc. — genuine connectivity blips, GH #1371)
    is NOT converted and propagates unchanged, so the ordinary
    connectivity retry stays intact for this call.
    """

    def __init__(self, *, collection: str, chunk_count: int, original: str) -> None:
        self.collection = collection
        self.chunk_count = chunk_count
        super().__init__(
            f"combined write to collection {collection!r} ({chunk_count} "
            f"chunk(s)) timed out waiting for the server-side embed and "
            f"was NOT retried (nexus-y9t08 — the engine has no "
            f"cancellation path for an in-flight embed, so a retry would "
            f"start an uncancelled duplicate on top of a possibly-still-"
            f"running one): {original}"
        )


#: NexusError subclasses that a per-record/per-file BATCH loop (``nx dt
#: index``, ``nx index pdf --dir``) must catch, log, and continue past —
#: never let escape and abort the whole batch (nexus-rlkgu; recurrence-
#: prevention for the nexus-2fyb / nexus-qo84l / nexus-9800y / nexus-hb10j
#: class of bug: a per-record ingest loop's narrow except tuple let a
#: newly-introduced NexusError subclass propagate uncaught, killing the
#: rest of the batch instead of just the offending record).
#:
#: This is the ONE place a per-record-raisable NexusError subclass is
#: added; every per-record loop catch site references this tuple by name
#: (or uses a broad ``except Exception``, safe by construction) instead of
#: repeating a per-exception except clause. tests/test_rlkgu_per_record_
#: catch_tripwire.py is the mechanical enforcement:
#:
#:  1. every per-record/per-file loop in src/nexus/commands/dt.py and
#:     src/nexus/commands/index.py that calls an index_*-family function
#:     must catch this tuple (by name), a broad Exception, or be in that
#:     test's explicit allowlist with a reason;
#:  2. EVERY NexusError subclass defined in this module must be
#:     classified — either a member of this tuple, or listed in that
#:     test's command-level allowlist with a reason explaining why it is
#:     NOT per-record-raisable. A newly-added, unclassified NexusError
#:     subclass fails that test at introduction time — the tripwire's
#:     whole point is to make that classification decision mandatory
#:     rather than something the next per-record loop author has to
#:     remember to go check four call sites for.
#:
#: doc_indexer's per-record ingest paths (index_pdf / index_markdown) are
#: the origin of the first two members: ChunkLandingUnverifiedError fires
#: from ``_upsert_skip_reembed`` before any manifest row is committed;
#: IndexRunVerifyRefused fires from the RUNFENCE completion-verify gate.
#: ExtractionQualityError (nexus-wi1uv occurrence 5 of this exact class,
#: caught by this tripwire before it shipped) fires from
#: ``PDFExtractor.extract()``, deep inside ``index_pdf`` -> ``_pdf_chunks``
#: -- reached by dt.py's ``nx dt index`` per-record loop for any
#: ``.pdf``-suffixed record. One PDF tripping the post-extraction quality
#: gate must fail THAT record, never abort the rest of the batch.
PER_RECORD_SURVIVABLE_EXCEPTIONS: tuple[type[NexusError], ...] = (
    ChunkLandingUnverifiedError,
    IndexRunVerifyRefused,
    ExtractionQualityError,
)
