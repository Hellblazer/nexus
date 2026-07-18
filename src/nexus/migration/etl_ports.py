# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-185 P2.1: the batched-ETL source/target Protocol seam (RQ4 lift).

The RDR-176/178 batch machinery (quota-paged reads, bounded-retry +
circuit-breaker writes, count verification) is proven but welded to
concrete Chroma-source / pgvector-target parameters. This module lifts the
SIGNATURE into ports while REUSING the primitives — ``iter_collection_chunks``
paging, ``_etl_batch_with_breaker``, the GH #1390 nonconformant-id guard —
never reimplementing them. The live ``_migrate_one`` path is deliberately
untouched: the P2 substrate rung drives THIS seam; the legacy path is
demoted in P4.

Design pins:

- **The id guard runs POST-transform.** GH #1390 stands (destination
  constraints never weakened), but the wire transform is where .15's re-id
  COMPUTES the correct chash — so a re-id run passes the guard because its
  ids are genuinely correct, while an identity run over legacy ids still
  fails loudly with the re-index diagnostic.
- **Identical-text collapse is deduped post-transform** (first occurrence
  kept — the ChashRepository.upsertMany precedent) and verification
  compares the target count against DISTINCT post-transform ids, not the
  raw source count (RDR-108 collapse makes them legitimately differ).
- **Immutable source (RDR-176)**: the source port exposes ONLY
  ``iter_batches``/``count``; the adapter holds its client privately, so
  no write verb is reachable through the seam.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import structlog

from nexus.retry import EtlCircuitBreaker, _etl_batch_with_breaker

_log = structlog.get_logger(__name__)


@runtime_checkable
class EtlSource(Protocol):
    """Read-only chunk source. NO write members, by design (RDR-176)."""

    def iter_batches(
        self, collection: str, *, page: int, include_embeddings: bool = False
    ) -> Iterator[list[dict[str, Any]]]:
        """Yield page-aligned batches of ``{id, document, metadata[, embedding]}``.

        Implementations MAY additionally accept ``start_offset`` (keyword)
        to begin mid-stream — the resume path passes it only when nonzero,
        so offset-less fixtures stay conformant. Offsets are stable because
        the source is frozen post-cutover (RDR-176 immutability)."""
        ...

    def count(self, collection: str) -> int: ...


@runtime_checkable
class EtlTarget(Protocol):
    """Write side of the seam — the pgvector service shape."""

    def upsert_chunks(
        self,
        collection: str,
        ids: list[str],
        documents: list[Any],
        metadatas: list[Any],
        *,
        embeddings: list[Any] | None = None,
    ) -> Any: ...

    def count(self, collection: str) -> int: ...


@dataclass(frozen=True)
class EtlRunResult:
    """Outcome of one :func:`run_batched_etl` run."""

    ok: bool
    source_count: int
    written: int
    reason: str = ""


class ChromaReadSource:
    """RDR-176 read leg as an :class:`EtlSource`. Client held privately;
    only the read surface is exposed."""

    def __init__(self, read_client: Any) -> None:
        self._client = read_client

    def iter_batches(
        self, collection: str, *, page: int, include_embeddings: bool = False,
        start_offset: int = 0,
    ) -> Iterator[list[dict[str, Any]]]:
        from nexus.migration.chroma_read import iter_collection_chunks  # noqa: PLC0415 — deferred, keeps module import cheap

        batch: list[dict[str, Any]] = []
        for chunk in iter_collection_chunks(
            self._client, collection, page_size=page,
            include_embeddings=include_embeddings, start_offset=start_offset,
        ):
            batch.append(chunk)
            if len(batch) == page:
                yield batch
                batch = []
        if batch:
            yield batch

    def count(self, collection: str) -> int:
        return int(self._client.get_collection(collection).count())


class VectorServiceTarget:
    """The service vector client as an :class:`EtlTarget` (thin delegation)."""

    def __init__(self, vector_client: Any) -> None:
        self._client = vector_client

    def upsert_chunks(
        self,
        collection: str,
        ids: list[str],
        documents: list[Any],
        metadatas: list[Any],
        *,
        embeddings: list[Any] | None = None,
    ) -> Any:
        return self._client.upsert_chunks(
            collection, ids, documents, metadatas, embeddings=embeddings
        )

    def count(self, collection: str) -> int:
        return int(self._client.count(collection))


def run_batched_etl(
    source: EtlSource,
    target: EtlTarget,
    *,
    source_collection: str,
    target_collection: str,
    page: int,
    include_embeddings: bool = False,
    transform: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
    breaker: EtlCircuitBreaker | None = None,
    on_batch: Callable[[int, int], None] | None = None,
    skip_rows: int = 0,
) -> EtlRunResult:
    """Drive one source→target batched ETL run through the seam.

    Per batch: read → *transform* (the wire seam — identity when ``None``)
    → post-transform id guard (GH #1390) → within-batch dedupe by id →
    breaker-wrapped upsert. Ends with a count verification against the
    DISTINCT post-transform id count. Failures are REPORTED in the result,
    never raised (the per-collection contract ``_migrate_one`` established).

    ``on_batch(written_so_far, source_count_so_far)`` is the progress seam
    (feeds the rung's progress reporter AND the rung-keyed watermark
    advance — see ``substrate_etl.execute_leg``). ``skip_rows`` resumes
    mid-stream from a watermark floor (RDR-178 resumability): counts are
    THIS-run-relative; the caller owns floor arithmetic. When ``skip_rows``
    is nonzero the source must accept ``start_offset``. NOTE: the final
    count verification compares the FULL target against distinct ids seen
    THIS run, so a resumed run's verification is delegated to the caller
    (partial-run visibility) — verified=False result fields still hold.
    """
    from nexus.migration.vector_etl import (  # noqa: PLC0415 — deferred to avoid import cycle (vector_etl is heavy)
        _legacy_id_failure_reason,
        _nonconformant_id,
    )

    breaker = breaker if breaker is not None else EtlCircuitBreaker()
    source_count = 0
    written = 0
    distinct_ids: set[str] = set()
    try:
        if skip_rows:
            batches = source.iter_batches(
                source_collection, page=page,
                include_embeddings=include_embeddings, start_offset=skip_rows,
            )
        else:
            batches = source.iter_batches(
                source_collection, page=page, include_embeddings=include_embeddings
            )
        for batch in batches:
            source_count += len(batch)
            if transform is not None:
                batch = transform(batch)
            batch_ids = [c["id"] for c in batch]
            bad_id = _nonconformant_id(batch_ids)
            if bad_id is not None:
                reason = _legacy_id_failure_reason(source_collection, bad_id)
                _log.error(
                    "etl_seam_nonconformant_post_transform",
                    source=source_collection,
                    target=target_collection,
                    example_id=bad_id,
                    written=written,
                )
                return EtlRunResult(False, source_count, written, reason)
            # Identical-text collapse: dedupe by post-transform id, first kept.
            deduped: list[dict[str, Any]] = []
            seen: set[str] = set()
            for chunk in batch:
                if chunk["id"] in seen:
                    continue
                seen.add(chunk["id"])
                deduped.append(chunk)
            distinct_ids.update(seen)
            embeddings = None
            if include_embeddings and all(c.get("embedding") is not None for c in deduped):
                embeddings = [c["embedding"] for c in deduped]
            _etl_batch_with_breaker(
                target.upsert_chunks,
                target_collection,
                [c["id"] for c in deduped],
                [c["document"] for c in deduped],
                [c["metadata"] for c in deduped],
                breaker=breaker,
                embeddings=embeddings,
            )
            written += len(deduped)
            if on_batch is not None:
                on_batch(written, source_count)
    except Exception as exc:  # noqa: BLE001 — report, never raise: the per-collection contract
        reason = f"upsert failed after {written} chunks: {exc}"
        _log.error(
            "etl_seam_upsert_failed",
            source=source_collection,
            target=target_collection,
            written=written,
            error=str(exc),
        )
        return EtlRunResult(False, source_count, written, reason)

    expected = len(distinct_ids)
    target_count = int(target.count(target_collection))
    if skip_rows:
        # Resumed run: the target legitimately holds rows from the earlier
        # partial pass — this run can only assert its OWN rows landed. The
        # authoritative full-collection check is the rung's verify() (the
        # RDR-142 gate), which sees the whole source.
        if target_count < expected:
            reason = (
                f"post-write count mismatch on resume: target={target_count} "
                f"< this-run distinct={expected}"
            )
            _log.error(
                "etl_seam_resume_count_mismatch",
                source=source_collection,
                target=target_collection,
                expected=expected,
                target_count=target_count,
            )
            return EtlRunResult(False, source_count, written, reason)
        return EtlRunResult(True, source_count, written)
    # `<`, not `!=` (nexus-tidtd): a co-resident target legitimately holds
    # rows this leg never wrote — independently indexed data or another
    # era's legs. Equality assumed the leg exclusively owns the target and
    # turned a fully-landed leg into a forever-failing upgrade. Same
    # semantics as the resume branch above. ACCEPTED, UNMITIGATED residual:
    # pre-existing rows can mask a partial this-run loss here, and nothing
    # downstream catches it today — drop_converged_legs still tests count
    # equality (the nexus-tidtd root cause), so it will re-plan, not
    # verify membership. The full-membership convergence check is the
    # deferred nexus-tidtd design (PG-side per the NO-SQLITE directive).
    if target_count < expected:
        reason = (
            f"post-write count mismatch: distinct-transformed={expected} "
            f"target={target_count} (raw source={source_count})"
        )
        _log.error(
            "etl_seam_count_mismatch",
            source=source_collection,
            target=target_collection,
            expected=expected,
            target_count=target_count,
        )
        return EtlRunResult(False, source_count, written, reason)

    _log.info(
        "etl_seam_run_complete",
        source=source_collection,
        target=target_collection,
        source_count=source_count,
        written=written,
    )
    return EtlRunResult(True, source_count, written)
