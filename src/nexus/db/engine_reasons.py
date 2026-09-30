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


def error_reason(body: Any) -> str | None:
    """The ``reason`` of a decoded error body, or None when it has none.

    None includes every engine that predates the field and every typed body
    that has no reason (the 409 bodies key on ``status`` instead)."""
    if not isinstance(body, dict):
        return None
    reason = body.get("reason")
    return reason if isinstance(reason, str) and reason else None
