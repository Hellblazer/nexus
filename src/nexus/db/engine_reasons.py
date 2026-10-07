# SPDX-License-Identifier: AGPL-3.0-or-later
"""The engine's typed-error ``reason`` vocabulary, as the client reads it.

A 4xx typed body from the engine carries a top-level ``reason``: a stable,
machine-readable discriminator, so a client never has to match the prose in
``error`` (nexus-bgvnx). The vocabulary and its rule live engine-side in
``HttpUtil.sendTypedDbError``'s javadoc; this module is the client's single
copy of the values, so a caller in ``corpus`` and one in ``db`` cannot drift.

A leaf module on purpose: ``db.http_vector_client`` and ``corpus`` each
import the other lazily to avoid a cycle, so a shared constant cannot live
in either.
"""
from __future__ import annotations

from typing import Any

#: A write or read naming a ``(tenant, collection)`` with no catalog row
#: (HTTP 422, ``UnregisteredCollectionException``). Also carries ``tenant``,
#: ``collection`` and ``remedy``.
UNREGISTERED_COLLECTION_REASON: str = "unregistered_collection"

#: ``upsert-chunks`` or ``store-put`` asked to write a chash with no live
#: manifest row in the collection (HTTP 422, RDR-223 Phase 3 Step 2). Also
#: carries ``unowned_count``, ``requested_count`` and ``unowned_chashes`` (a
#: sample of at most eight). The remedy is the combined write, never a retry.
OWNERLESS_CHUNK_WRITE_REASON: str = "ownerless_chunk_write"

#: ``POST /v1/vectors/gc/quarantine-restore`` could not take the collection's sweep
#: gate (or an owning document's index-run lock) inside its 2 s bound, or ran past its
#: statement bound (HTTP 503, nexus-wbfpw.49); the statement that tripped rolled back.
#: Also carries ``retry_after_seconds``, ``nothing_moved`` (False when an earlier
#: quarantine sibling of the call had already committed: each sibling is its own
#: transaction), ``audit_ids`` and ``moved_chashes`` (both empty when nothing
#: moved; nexus-wbfpw.55). Retryable: the same call may be sent
#: again. The route is a non-idempotent sweep route, so the client's gateway ladder
#: does not retry it on its own; the CLI reads this and says so.
QUARANTINE_RESTORE_BUSY_REASON: str = "quarantine_restore_busy"

#: A re-home or rename across embedding models: the collection's chunks would
#: move to a collection of another model (HTTP 409, RDR-225). Re-modelling a
#: collection is a cross-model migration, never a rename.
COLLECTION_MODEL_MISMATCH_REASON: str = "collection_model_mismatch"

#: A write naming an embedding model registered nowhere (HTTP 422, RDR-225).
UNREGISTERED_EMBEDDING_MODEL_REASON: str = "unregistered_embedding_model"

#: A registered model whose partition was never created (HTTP 500, RDR-225):
#: an engine invariant, not a caller mistake.
MODEL_PARTITION_MISSING_REASON: str = "model_partition_missing"

#: PostgreSQL found no tenant partition for the row (HTTP 500, RDR-225): an
#: engine invariant, not a caller mistake.
TENANT_PARTITION_MISSING_REASON: str = "tenant_partition_missing"

#: A first token for a tenant waited on partition-creation locks past its
#: bound and issued nothing (HTTP 503 with ``Retry-After``, RDR-225).
#: Retryable: the same call may be sent again.
TENANT_CREATION_BUSY_REASON: str = "tenant_creation_busy"


def error_reason(body: Any) -> str | None:
    """The ``reason`` of a decoded error body, or None when it has none.

    None includes every engine that predates the field and every typed body
    that has no reason (the 409 bodies key on ``status`` instead)."""
    if not isinstance(body, dict):
        return None
    reason = body.get("reason")
    return reason if isinstance(reason, str) and reason else None
