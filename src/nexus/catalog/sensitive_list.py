# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The engine's bounded sensitive list (``GET /v1/catalog/sensitive``, engine 0.1.188): the answer's shape check and
the error a strict caller gets, split out of ``http_catalog_client`` (over its size ceiling)."""
from __future__ import annotations


class SensitiveListUnavailable(Exception):
    """``HttpCatalogClient.sensitive_documents(strict=True)`` got no answer; :attr:`reason` says why.

    A caller that must not scan the catalog instead (``nx catalog list --sensitive``) tells the three apart: an
    engine without the route is a message about the engine, a timeout is a retry, anything else is neither.
    """

    #: The engine does not advertise ``catalog_sensitive_list_supported``, or the route answers 404.
    UNSUPPORTED = "unsupported"
    #: The route answered 503: the engine's own statement timeout, or the service unavailable.
    TIMEOUT = "timeout"
    #: Any other status, a transport error, or a body that is not the route's shape.
    FAILED = "failed"

    def __init__(self, reason: str) -> None:
        super().__init__(f"the engine's sensitive list is unavailable ({reason})")
        self.reason = reason


def sensitive_answer(result: object) -> dict | None:
    """The route's body as ``{"count", "capped", "documents": [{"tumbler", "physical_collection"}]}``, or ``None`` when
    it is malformed or inconsistent (an uncapped list whose length is not ``count``)."""
    if not isinstance(result, dict):
        return None
    count, capped, documents = result.get("count"), result.get("capped"), result.get("documents")
    well_formed = (
        not isinstance(count, bool) and isinstance(count, int) and count >= 0 and isinstance(capped, bool)
        and isinstance(documents, list)
        # A document flagged before it was indexed has no collection: the engine sends null, read as "",
        # as _to_entry reads it for the walk.
        and all(isinstance(d, dict) and isinstance(d.get("tumbler"), str) and "physical_collection" in d
                and isinstance(d["physical_collection"], (str, type(None))) for d in documents)
        and (capped or len(documents) == count)
    )
    if not well_formed:
        return None
    return {
        "count": count, "capped": capped,
        "documents": [{"tumbler": d["tumbler"], "physical_collection": d.get("physical_collection") or ""}
                      for d in documents],
    }
