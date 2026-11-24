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

#: ``upsert-chunks`` (or the since-retired ``store-put``) asked to write a chash with no live
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

#: A write (a chunk write: ``upsert-chunks``, or the combined ``manifest/write_many`` /
#: ``append`` / ``append_many``; or, since nexus-i15oc, a taxonomy, aspect, highlight
#: or aspect-queue write) named a collection whose catalog row carries
#: ``superseded_by`` (HTTP 409, nexus-mp8ys). The body repeats the value in
#: ``error`` and also carries ``message``, ``collection`` and ``superseded_by``
#: (the successor). Never retryable, and NOT the ``unregistered_collection`` 422
#: on purpose: a register-and-retry would revive the retired name. Only an
#: explicit registration revives a name.
COLLECTION_SUPERSEDED_REASON: str = "collection_superseded"

#: ``POST /v1/vectors/gc/restore-rereferenced`` (bounded or not) or ``POST /v1/vectors/gc/quarantine-restore``, from the collection-id
#: latch (gate 3), named quarantined chunks tagged with the origin's label whose ``origin_collection_id`` is the JSON null marker: an
#: origin that had no registry row, or no longer has one, when they were quarantined (HTTP 409, RDR-229 P3.7). Also carries ``label``
#: (the origin) and ``rows`` (how many chunks carry the marker). The whole call is refused before anything moves, id-matched chunks
#: of the same call included, and the chunks stay quarantined. Never retryable as sent: restore the other chunks by naming only
#: them, and re-register the origin or let the quarantine expire for the marked ones.
RESTORE_ORIGIN_UNREGISTERED_REASON: str = "restore_origin_unregistered"

#: A register or revive (upsert), a supersede successor, a rename target or a single-form per-collection import named a label that is RESERVED: a
#: collection renamed away from it by the one-row rename still has rows in the name-keyed tables that carry it, until the P4.1
#: cleanup drops the name columns (HTTP 409, RDR-229 P3.7; the engine's ``HttpUtil.LABEL_RESERVED_REASON``). Also carries ``error`` and ``label``.
#: Nothing was written. Never retryable: pick another label, or rename the collection that was renamed away back to it.
LABEL_RESERVED_REASON: str = "label_reserved"

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

#: ``POST /v1/catalog/collections/delete`` named a collection a re-embed job in a non-terminal state (planned, running, paused,
#: cutover or failed) holds as its source or its target (HTTP 409, RDR-229 P3.5a row 14). Also carries ``error``, ``collection``
#: and ``job_id``. Nothing was removed. Retry once the job is done, or after ``nx collection re-embed --abort JOB``.
COLLECTION_BUSY_REASON: str = "collection_busy"

#: A collection rename (``POST /v1/catalog/collections/rename`` and the per-store rename routes) while the engine cannot yet
#: run the one-row rename (HTTP 409, RDR-229 P3.5a). Also carries ``error`` and ``missing``: ``restart`` when the engine must
#: restart to apply a changeset (renames open after the next restart), else ``name_key_drop_move``, ``p35b_list_missing``,
#: ``readiness_unreadable``, ``p35c_readers_absent`` or ``latch_row_missing``. Nothing was renamed.
RENAME_REFUSED_REASON: str = "rename_refused"


def error_reason(body: Any) -> str | None:
    """The ``reason`` of a decoded error body, or None when it has none.

    None includes every engine that predates the field and every typed body
    that has no reason (the 409 bodies key on ``status`` instead)."""
    if not isinstance(body, dict):
        return None
    reason = body.get("reason")
    return reason if isinstance(reason, str) and reason else None
