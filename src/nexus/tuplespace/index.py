# SPDX-License-Identifier: Apache-2.0
"""Chroma collection layout for the semantic tuple space (RDR-110 P1.3).

Project-tier storage: one persistent local ChromaDB collection per registered
template, named ``tuples__<template_slug>``.  The slug strips ``<`` and ``>``
from parameterised segments and replaces ``/`` with ``_`` so that collection
names are valid ChromaDB identifiers (no colons; ``__`` separator is the
project convention from CLAUDE.md).

**Client choice**: production code passes a ``chromadb.PersistentClient``
pointed at the project data directory (``~/.config/nexus/``).  Tests inject
a ``chromadb.EphemeralClient`` so no filesystem side-effects occur.  The
``TupleIndex`` class accepts any ``chromadb.ClientAPI``-compatible object
(constructor injection; no singletons).

**Embedding**: collections are created without an explicit embedding function,
so ChromaDB uses its bundled all-MiniLM-L6-v2 embedder when documents are
passed.  Explicit embedding-function injection is deferred to P1.4/Phase 2.

**API consumers (P1.4 api.py) do not touch ChromaDB directly**, they call
``out()`` and ``read()`` on this class only.
"""

from __future__ import annotations

import re
from typing import Any

import structlog

from nexus.db.chroma_quotas import QUOTAS, QuotaValidator

_log = structlog.get_logger(__name__)

# Matches < and > individually so both brackets are removed.
_ANGLE_RE = re.compile(r"[<>]")

_VALIDATOR = QuotaValidator(QUOTAS)


def _template_slug(name: str) -> str:
    """Convert a template name to a ChromaDB-safe slug.

    Strips ``<`` and ``>`` from parameterised segments, then replaces
    ``/`` with ``_``.  Examples::

        "tasks/<project>"  -> "tasks_project"
        "locks/<resource>" -> "locks_resource"
        "plans"            -> "plans"
        "a/<b>/c/<d>"      -> "a_b_c_d"
    """
    return _ANGLE_RE.sub("", name).replace("/", "_")


def collection_name(template_name: str) -> str:
    """Return the ChromaDB collection name for *template_name*.

    Format: ``tuples__<slug>`` where slug is produced by
    :func:`_template_slug`.

    Examples::

        collection_name("tasks/<project>") == "tuples__tasks_project"
        collection_name("plans")           == "tuples__plans"
    """
    return f"tuples__{_template_slug(template_name)}"


class TupleIndex:
    """Thin ChromaDB wrapper for tuple-space per-template collections.

    One collection per registered template; created at instantiation via
    ``get_or_create_collection`` (idempotent).  The collection stores tuple
    documents with metadata containing ``subspace`` plus the validated
    dimensions declared in the template's YAML schema.

    Args:
        collections: Mapping from template name to ChromaDB collection.
    """

    def __init__(
        self,
        collections: dict[str, Any],  # dict[str, chromadb.Collection]
    ) -> None:
        self._collections = collections

    @classmethod
    def from_registry(cls, registry: Any, client: Any) -> "TupleIndex":
        """Create a ``TupleIndex`` from a loaded :class:`Registry`.

        Iterates ``registry.schemas()`` and calls
        ``client.get_or_create_collection`` for each template, using
        :func:`collection_name` to derive the name.  Safe to call multiple
        times on the same client (idempotent).

        Args:
            registry: A :class:`nexus.tuplespace.registry.Registry` instance.
            client: Any ``chromadb.ClientAPI``-compatible object
                (``PersistentClient`` in production, ``EphemeralClient`` in
                tests).

        Returns:
            A fully initialised ``TupleIndex``.
        """
        collections: dict[str, Any] = {}
        for schema in registry.schemas():
            coll_name = collection_name(schema.name)
            coll = client.get_or_create_collection(coll_name)
            collections[schema.name] = coll
            _log.debug(
                "tuplespace_collection_ready",
                template=schema.name,
                collection=coll_name,
            )
        _log.info(
            "tuplespace_index_created",
            count=len(collections),
        )
        return cls(collections)

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def out(
        self,
        *,
        template_name: str,
        subspace: str,
        tuple_id: str,
        payload: str,
        metadata: dict[str, Any],
    ) -> None:
        """Write (upsert) one tuple into the template's collection.

        Args:
            template_name: The template key as registered (e.g. ``"tasks/<project>"``).
            subspace: Concrete subspace string (e.g. ``"tasks/nexus"``).
            tuple_id: Stable identifier for this tuple (used as the ChromaDB
                record ID).  Upsert semantics: calling ``out`` twice with the
                same ``tuple_id`` is a no-op.
            payload: Document text indexed for semantic retrieval.
            metadata: Record metadata dict.  Must include ``subspace`` and any
                validated dimensions.  Validated against ChromaDB quota limits
                before the write.

        Raises:
            KeyError: *template_name* is not in this index.
            RecordTooLarge: *payload* exceeds ``MAX_DOCUMENT_BYTES`` (16 384 bytes).
            NameTooLong: *tuple_id* exceeds ``MAX_ID_BYTES`` (128 bytes).
        """
        _VALIDATOR.validate_record(
            id=tuple_id,
            document=payload,
            embedding=None,
            metadata=metadata,
        )
        coll = self._collections[template_name]
        coll.upsert(
            ids=[tuple_id],
            documents=[payload],
            metadatas=[metadata],
        )
        _log.debug(
            "tuplespace_out",
            template=template_name,
            subspace=subspace,
            tuple_id=tuple_id,
        )

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    def delete(
        self,
        *,
        template_name: str,
        tuple_ids: list[str],
    ) -> None:
        """Delete tuples from the template's collection by id.

        Chunks by ``QUOTAS.MAX_RECORDS_PER_WRITE`` so callers can pass
        large id lists (e.g. from the retention sweeper, nexus-kk9h) without
        hitting the Chroma quota.

        Args:
            template_name: The template key (e.g. ``"tasks/<project>"``).
            tuple_ids: List of tuple IDs to remove from Chroma.

        Raises:
            KeyError: *template_name* is not in this index.
        """
        if not tuple_ids:
            return
        coll = self._collections[template_name]
        batch = QUOTAS.MAX_RECORDS_PER_WRITE
        for i in range(0, len(tuple_ids), batch):
            coll.delete(ids=tuple_ids[i : i + batch])
        _log.debug(
            "tuplespace_delete",
            template=template_name,
            count=len(tuple_ids),
        )

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def read(
        self,
        *,
        template_name: str,
        subspace: str,
        query: str,
        where: dict[str, Any] | None = None,
        n_results: int = 1,
    ) -> list[dict[str, Any]]:
        """Query tuples semantically from the template's collection.

        The *subspace* filter is always merged into the ``where`` predicate
        so that only tuples from the requested concrete subspace are returned.

        Args:
            template_name: The template key (e.g. ``"tasks/<project>"``).
            subspace: Concrete subspace to filter on.
            query: Semantic query text.  Must be <= 256 chars.
            where: Optional caller-supplied ChromaDB metadata filter dict.
                Must have <= 8 top-level keys (quota limit).
            n_results: Maximum results to return.  Must be <= 300.

        Returns:
            List of dicts with keys ``id``, ``document``, ``metadata``,
            ``distance``.  Empty list if no matches.

        Raises:
            KeyError: *template_name* is not in this index.
            ResultsExceedLimit: *n_results* > 300.
            QueryStringTooLong: *query* > 256 chars.
            TooManyPredicates: caller *where* has > 8 top-level keys.
        """
        # Validate quota limits before touching ChromaDB.
        _VALIDATOR.validate_query(
            query_text=query,
            where=where,
            n_results=n_results,
        )

        # nexus-26b7 (notable, dim-15 N-1): the previous untracked
        # TODO covered an empirical ChromaDB quota question on
        # compound $and-wrapped filters. Caller_where with 8
        # predicates passes the pre-merge validation; the post-merge
        # form is wrapped under $and. Lift if/when a real workload
        # surfaces a quota-trip; no production bead yet.
        merged_where = _merge_where(subspace, where)

        coll = self._collections[template_name]
        raw = coll.query(
            query_texts=[query],
            where=merged_where,
            n_results=n_results,
        )

        # ChromaDB returns nested lists (one per query text).  Unwrap the
        # single-query outer list.
        ids = raw["ids"][0] if raw["ids"] else []
        documents = raw["documents"][0] if raw["documents"] else []
        metadatas = raw["metadatas"][0] if raw["metadatas"] else []
        distances = raw["distances"][0] if raw["distances"] else []

        results = [
            {
                "id": id_,
                "document": doc,
                "metadata": meta,
                "distance": dist,
            }
            for id_, doc, meta, dist in zip(ids, documents, metadatas, distances)
        ]
        _log.debug(
            "tuplespace_read",
            template=template_name,
            subspace=subspace,
            n_results=n_results,
            hits=len(results),
        )
        return results


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _merge_where(
    subspace: str,
    caller_where: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build the merged ChromaDB ``where`` filter.

    Always injects a ``subspace`` equality filter.  If the caller supplied
    additional predicates, wraps both in a ``$and`` compound filter.

    Args:
        subspace: The concrete subspace string to filter on.
        caller_where: Optional caller-supplied filter dict (may be ``None``).

    Returns:
        A single ChromaDB-compatible ``where`` dict.
    """
    subspace_filter: dict[str, Any] = {"subspace": {"$eq": subspace}}

    if not caller_where:
        return subspace_filter

    # Wrap subspace filter and caller filter in $and.
    return {"$and": [subspace_filter, caller_where]}
