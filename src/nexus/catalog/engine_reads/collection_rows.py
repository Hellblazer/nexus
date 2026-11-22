# SPDX-License-Identifier: AGPL-3.0-or-later
"""Client reader for ``GET /v1/catalog/collections/list`` that keeps the rows as the engine sent them (RDR-229 P3.12,
nexus-p2m1x.32).

``HttpCatalogClient.list_collections`` passes every row through ``_coerce_collection_id``, which fills a missing
``collection_id`` with ``None``. That hides whether the engine lists ids at all: an engine before RDR-229 P3.5a's read
half sends no key, one after it sends the key on every row (an int, or null). ``nx doctor``'s registry null-id check
needs that distinction, so it reads the rows through this module instead."""
from __future__ import annotations

from typing import Any

ROUTE: str = "/collections/list"


def read_collection_rows(client: Any) -> list[dict]:
    """Every registry row (every lifecycle state), uncoerced: the ``collection_id`` key is absent on an engine that does
    not list ids."""
    result = client._get(ROUTE, lifecycle_state=None, content_type=None)
    return [dict(c) for c in (result.get("collections", []) if isinstance(result, dict) else []) if isinstance(c, dict)]
